"""#8: admin alerts and customer notices for held / reconcile / recovered orders are durable,
retried with backoff, re-sent after a restart, and always built from the order's current state."""

from datetime import datetime, timedelta, timezone

import bot
from conftest import ADMIN_ID, CUSTOMER_ID, FakeBot, new_order
from storage import ALERT_BACKOFF_MAX_SECONDS, MAX_NOTICE_ATTEMPTS, backoff_seconds

ADMIN = str(ADMIN_ID)
_n = 0


async def claimed(storage, order_id="ord1", **kwargs):
    global _n
    _n += 1
    await storage.insert_order(new_order(order_id, reference=f"{_n:064x}", **kwargs))
    order = await storage.claim_order(order_id)
    assert order
    return order


async def make_due(storage, order_id, *fields):
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await storage._orders.update_one({"_id": order_id}, {"$set": {f: past for f in fields}})


def aware(moment):
    """The in-memory client returns naive datetimes; the real one is tz-aware."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def admin_messages(fake_bot):
    return [(text, kw) for cid, text, kw in fake_bot.sent if str(cid) == ADMIN]


def callbacks(kw):
    markup = kw.get("reply_markup")
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


# ---- held ----------------------------------------------------------------------------------

async def test_hold_sends_the_alert_and_the_customer_notice_and_clears_the_flags(any_storage):
    order = await claimed(any_storage)
    fake = FakeBot()

    await bot.hold_order(fake, order, "supplier said no", attempt=1)

    ((text, kw),) = admin_messages(fake)
    assert "on hold" in text and "supplier said no" in text
    assert [c.split(":")[0] for c in callbacks(kw)] == ["approve", "reject"]
    assert fake.texts_to(CUSTOMER_ID) == [bot.CUSTOMER_DELAY_NOTICE]
    stored = await any_storage.get_order("ord1")
    assert stored["status"] == "held" and stored["attempt"] == 1
    assert stored["alert_pending"] is False and stored["notice_pending"] is False


async def test_an_unsent_alert_is_persisted_and_retried_with_backoff(any_storage):
    order = await claimed(any_storage)
    down = FakeBot(fail_for=[ADMIN])

    await bot.hold_order(down, order, "supplier said no")

    stored = await any_storage.get_order("ord1")
    assert stored["alert_pending"] is True and stored["alert_attempts"] == 1
    assert aware(stored["alert_next_at"]) > datetime.now(timezone.utc) + timedelta(seconds=30)
    assert down.texts_to(CUSTOMER_ID) == [bot.CUSTOMER_DELAY_NOTICE]  # the customer still heard

    back_up = FakeBot()
    await bot.flush_outbox(back_up)
    assert back_up.sent == []  # not due yet: the backoff is respected

    await make_due(any_storage, "ord1", "alert_next_at")
    await bot.flush_outbox(back_up)

    ((text, kw),) = admin_messages(back_up)
    assert "on hold" in text and "approve:ord1" in callbacks(kw)
    assert (await any_storage.get_order("ord1"))["alert_pending"] is False
    await bot.flush_outbox(back_up)
    assert len(admin_messages(back_up)) == 1  # sent once


async def test_the_owed_alert_survives_a_restart(any_storage):
    order = await claimed(any_storage)
    await bot.hold_order(FakeBot(fail_for=[ADMIN]), order, "supplier said no")
    await make_due(any_storage, "ord1", "alert_next_at")

    # "restart": nothing in memory, only the database. A fresh pass sends what is owed.
    after_restart = FakeBot()
    await bot.flush_outbox(after_restart)

    assert len(admin_messages(after_restart)) == 1


async def test_repeated_failures_back_off_exponentially(any_storage):
    order = await claimed(any_storage)
    down = FakeBot(fail_for=[ADMIN])
    await bot.hold_order(down, order, "x")

    for expected_attempts in (2, 3, 4):
        await make_due(any_storage, "ord1", "alert_next_at")
        await bot.flush_outbox(down)
        stored = await any_storage.get_order("ord1")
        assert stored["alert_attempts"] == expected_attempts
        wait = (aware(stored["alert_next_at"]) - datetime.now(timezone.utc)).total_seconds()
        assert abs(wait - backoff_seconds(expected_attempts)) < 5


def test_the_backoff_schedule_doubles_and_is_capped():
    assert [backoff_seconds(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert backoff_seconds(0) == 60 and backoff_seconds(50) == ALERT_BACKOFF_MAX_SECONDS


# ---- reconcile -----------------------------------------------------------------------------

async def test_reconcile_alert_is_durable_and_offers_the_unlock_flow(any_storage):
    order = await claimed(any_storage)
    await bot.reconcile_order(FakeBot(fail_for=[ADMIN]), order, "Purchase outcome unknown: timeout")
    await make_due(any_storage, "ord1", "alert_next_at")
    fake = FakeBot()

    await bot.flush_outbox(fake)

    ((text, kw),) = admin_messages(fake)
    assert "MANUAL RECONCILIATION" in text and "ord1-0" in text  # the supplier client reference
    assert callbacks(kw) == ["unlock:ord1"]
    assert (await any_storage.get_order("ord1"))["status"] == "reconcile"


# ---- recovered -----------------------------------------------------------------------------------

async def test_recovered_orders_owe_an_alert_and_a_customer_notice(any_storage):
    unknown = await claimed(any_storage, "u1")
    purchased = await claimed(any_storage, "p1")
    await any_storage.update_order("p1", {"outcome": "purchased"}, only_if_status="fulfilling")

    recovered = await any_storage.recover_interrupted_orders()

    assert sorted(o["id"] for o in recovered) == ["p1", "u1"]
    stored = await any_storage.get_order("u1")
    assert stored["alert_pending"] is True and stored["alert_kind"] == "recovered"
    assert stored["notice_pending"] is True

    fake = FakeBot()
    await bot.flush_outbox(fake)

    by_order = {text.split("#")[1].split("\n")[0]: (text, kw) for text, kw in admin_messages(fake)}
    text, kw = by_order["u1"]
    assert "interrupted by a restart" in text and callbacks(kw) == ["unlock:u1"]
    text, kw = by_order["p1"]
    assert "PURCHASED" in text and callbacks(kw) == []  # never an unlock for a purchased order
    assert fake.texts_to(CUSTOMER_ID) == [bot.CUSTOMER_DELAY_NOTICE] * 2
    again = FakeBot()
    await bot.flush_outbox(again)
    assert again.sent == []


# ---- built from the current state ------------------------------------------------------------------

async def test_an_alert_is_dropped_if_the_order_moved_on_before_it_was_sent(any_storage):
    order = await claimed(any_storage)
    await bot.reconcile_order(FakeBot(fail_for=[ADMIN]), order, "unknown")
    await any_storage.unlock_order("ord1", ADMIN_ID, "Pandora confirmed none")  # the admin already acted
    await make_due(any_storage, "ord1", "alert_next_at")
    fake = FakeBot()

    await bot.flush_outbox(fake)

    assert admin_messages(fake) == []
    assert (await any_storage.get_order("ord1"))["alert_pending"] is False


async def test_a_delay_notice_is_skipped_if_the_order_was_rejected_meanwhile(any_storage):
    order = await claimed(any_storage)
    await bot.hold_order(FakeBot(fail_for=[CUSTOMER_ID]), order, "x")
    assert (await any_storage.get_order("ord1"))["notice_pending"] is True
    await any_storage.reject_order("ord1")
    await make_due(any_storage, "ord1", "notice_next_at")
    fake = FakeBot()

    await bot.flush_outbox(fake)

    assert fake.texts_to(CUSTOMER_ID) == []
    assert (await any_storage.get_order("ord1"))["notice_pending"] is False


async def test_the_customer_notice_gives_up_after_repeated_failures_without_affecting_the_admin_alert(any_storage):
    order = await claimed(any_storage)
    blocked = FakeBot(fail_for=[CUSTOMER_ID])
    await bot.hold_order(blocked, order, "x")

    for _ in range(MAX_NOTICE_ATTEMPTS + 3):
        await make_due(any_storage, "ord1", "notice_next_at")
        await bot.flush_outbox(blocked)

    stored = await any_storage.get_order("ord1")
    assert stored["notice_pending"] is False and stored["notice_gave_up"] is True
    assert blocked.attempts[str(CUSTOMER_ID)] == MAX_NOTICE_ATTEMPTS  # then it stops trying
    assert len(admin_messages(blocked)) == 1 and stored["alert_pending"] is False


async def test_outbox_alerts_never_contain_credentials(any_storage, credential_box):
    order = await claimed(any_storage)
    await any_storage.update_order(
        "ord1", {"outcome": "purchased", "credentials_enc": credential_box.encrypt_list(["user:pass"])},
        only_if_status="fulfilling",
    )
    await any_storage.recover_interrupted_orders()
    fake = FakeBot()

    await bot.flush_outbox(fake)

    for text, _ in admin_messages(fake):
        assert "user:pass" not in text and "credentials" not in text.lower()
    assert "user:pass" not in str(fake.sent)


async def test_an_order_with_nothing_owed_sends_nothing(any_storage):
    await claimed(any_storage)
    fake = FakeBot()

    await bot.flush_outbox(fake)

    assert fake.sent == []

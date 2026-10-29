"""#10: a short stock reservation while the buyer pays; released on cancel, expiry, rejection
or failure; a clear message if the stock is gone before payment."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
from conftest import CUSTOMER_ID, TXID, FakeBot, FakeState, make_callback, new_order
from storage import (
    ORDER_RESERVATION_TTL_SECONDS,
    RESERVATION_TTL_SECONDS,
    RESERVE_MAX_TRACKED_STOCK,
    reservation_key,
)

KEY = reservation_key("pandora", "p1", None)


def aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def expire(storage, token):
    await storage._db["reservations"].update_one(
        {"token": token}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=5)}}
    )


# ---- the storage primitive ----------------------------------------------------------------------

async def test_only_as_many_buyers_as_there_are_units_can_reserve(any_storage):
    tokens = [await any_storage.reserve_stock(KEY, user, 2) for user in (1, 2, 3)]

    assert tokens[0] and tokens[1] and tokens[2] is None
    assert tokens[0] != tokens[1]


async def test_the_same_buyer_gets_the_same_unit_back_not_a_second_one(any_storage):
    first = await any_storage.reserve_stock(KEY, 1, 2)
    again = await any_storage.reserve_stock(KEY, 1, 2)
    other = await any_storage.reserve_stock(KEY, 2, 2)
    third = await any_storage.reserve_stock(KEY, 3, 2)

    assert again == first
    assert other and third is None  # one unit for user 1, one for user 2: none left


async def test_releasing_frees_the_unit_for_the_next_buyer(any_storage):
    token = await any_storage.reserve_stock(KEY, 1, 1)
    assert await any_storage.reserve_stock(KEY, 2, 1) is None

    assert await any_storage.release_reservation(token=token) == 1

    assert await any_storage.reserve_stock(KEY, 2, 1)


async def test_an_expired_reservation_frees_the_unit(any_storage):
    token = await any_storage.reserve_stock(KEY, 1, 1)
    await expire(any_storage, token)

    taken_over = await any_storage.reserve_stock(KEY, 2, 1)

    assert taken_over and taken_over != token
    assert await any_storage.attach_reservation(token, "ord1") is False  # the old holder lost it


async def test_zero_stock_and_a_missing_release_target_are_handled(any_storage):
    assert await any_storage.reserve_stock(KEY, 1, 0) is None
    assert await any_storage.release_reservation(token="nope") == 0


async def test_attaching_to_an_order_extends_the_reservation_and_records_the_order(any_storage):
    token = await any_storage.reserve_stock(KEY, 1, 1)

    assert await any_storage.attach_reservation(token, "ord1", ORDER_RESERVATION_TTL_SECONDS) is True

    doc = await any_storage._db["reservations"].find_one({"token": token})
    assert doc["order_id"] == "ord1"
    assert aware(doc["expires_at"]) > datetime.now(timezone.utc) + timedelta(hours=23)
    assert await any_storage.reserve_stock(KEY, 2, 1) is None  # still held
    assert await any_storage.release_reservation(order_id="ord1") == 1
    assert await any_storage.reserve_stock(KEY, 2, 1)


async def test_concurrent_buyers_never_share_a_unit(any_storage):
    results = await asyncio.gather(*[any_storage.reserve_stock(KEY, user, 3) for user in range(1, 11)])

    winners = [r for r in results if r]
    assert len(winners) == 3 and len(set(winners)) == 3


async def test_the_reservation_ttl_is_short_and_the_order_hold_is_longer():
    assert RESERVATION_TTL_SECONDS == 15 * 60
    assert ORDER_RESERVATION_TTL_SECONDS > RESERVATION_TTL_SECONDS


# ---- the checkout flow ------------------------------------------------------------------------------

def listing_with_stock(stock):
    offer = bot.Offer("pandora", "p1", None, 3.0, stock)
    return bot.Listing("k1", "Netflix 1M", "", [offer])


async def start_checkout(monkeypatch, user_id, stock, state=None):
    async def fake_catalog(force=False):
        return {"k1": listing_with_stock(stock)}

    monkeypatch.setattr(bot, "get_catalog", fake_catalog)
    state = state or FakeState()
    callback = make_callback("pay:trc20:k1", user_id=user_id)
    await bot.choose_payment(callback, state)
    return callback, state


async def test_the_first_buyer_gets_payment_details_and_a_reservation(storage, monkeypatch):
    callback, state = await start_checkout(monkeypatch, 501, stock=1)

    (text,) = callback.message.answer.await_args.args
    assert "TTestTronAddress" in text and "reserved for you for 15 minutes" in text
    assert state.state == bot.Checkout.waiting_reference
    assert state.data["reservation_token"] and state.data["reservation_key"] == KEY


async def test_the_next_buyer_is_told_clearly_that_the_stock_is_gone(storage, monkeypatch):
    await start_checkout(monkeypatch, 501, stock=1)

    callback, state = await start_checkout(monkeypatch, 502, stock=1)

    callback.message.answer.assert_awaited_once_with(bot.STOCK_GONE_MESSAGE)
    assert state.state is None and state.data == {}  # no checkout was started


async def test_cancelling_the_checkout_releases_the_unit_at_once(storage, monkeypatch):
    _, first_state = await start_checkout(monkeypatch, 501, stock=1)

    await bot.cancel_order(make_callback("cancel_order", user_id=501), first_state)
    callback, second_state = await start_checkout(monkeypatch, 502, stock=1)

    assert second_state.state == bot.Checkout.waiting_reference  # the second buyer can now proceed


async def test_the_cancel_command_also_releases_the_unit(storage, monkeypatch):
    _, first_state = await start_checkout(monkeypatch, 501, stock=1)
    message = SimpleNamespace(from_user=SimpleNamespace(id=501), answer=AsyncMock())

    await bot.cmd_cancel(message, first_state)
    _, second_state = await start_checkout(monkeypatch, 502, stock=1)

    assert second_state.state == bot.Checkout.waiting_reference


async def test_an_abandoned_checkout_frees_the_unit_when_it_expires(storage, monkeypatch):
    _, state = await start_checkout(monkeypatch, 501, stock=1)
    await expire(storage, state.data["reservation_token"])

    _, second_state = await start_checkout(monkeypatch, 502, stock=1)

    assert second_state.state == bot.Checkout.waiting_reference


async def test_unknown_or_plentiful_stock_is_never_reserved(storage, monkeypatch):
    for stock in (None, RESERVE_MAX_TRACKED_STOCK + 1, 5000):
        _, state = await start_checkout(monkeypatch, 501, stock=stock)
        assert state.state == bot.Checkout.waiting_reference
        assert "reservation_token" not in state.data

    assert await storage._db["reservations"].find_one({}) is None


# ---- from payment proof to order ---------------------------------------------------------------------

def proof_message(user_id):
    return SimpleNamespace(
        text=TXID,
        from_user=SimpleNamespace(id=user_id, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=FakeBot(),
    )


async def submit_proof(state, user_id):
    message = proof_message(user_id)
    await bot.receive_reference(message, state)
    return message


async def test_the_proof_attaches_the_reservation_to_the_order(storage, monkeypatch):
    _, state = await start_checkout(monkeypatch, 501, stock=1)
    message = await submit_proof(state, 501)

    order = await storage._orders.find_one({})
    assert order["stock_reservation"] == "held"
    reservation = await storage._db["reservations"].find_one({"order_id": order["id"]})
    assert reservation is not None
    ((admin, text, _),) = [m for m in message.bot.sent if str(m[0]) == "111"]
    assert "a unit is reserved for this order" in text


async def test_a_proof_that_arrives_after_the_reservation_lapsed_still_creates_the_order(storage, monkeypatch):
    _, state = await start_checkout(monkeypatch, 501, stock=1)
    await expire(storage, state.data["reservation_token"])
    await start_checkout(monkeypatch, 502, stock=1)  # someone else takes the unit

    message = await submit_proof(state, 501)  # the first buyer has already paid

    order = await storage._orders.find_one({})
    assert order is not None and order["stock_reservation"] == "lapsed"
    ((_, text, _),) = [m for m in message.bot.sent if str(m[0]) == "111"]
    assert "reservation expired" in text and "purchase can fail" in text


async def test_a_lapsed_reservation_is_taken_again_if_a_unit_is_free(storage, monkeypatch):
    _, state = await start_checkout(monkeypatch, 501, stock=1)
    await expire(storage, state.data["reservation_token"])  # expired, but nobody took it

    await submit_proof(state, 501)

    order = await storage._orders.find_one({})
    assert order["stock_reservation"] == "held"


# ---- release on rejection, failure and completion ------------------------------------------------------

async def create_reserved_order(storage, monkeypatch, order_id="ord1"):
    token = await storage.reserve_stock(KEY, CUSTOMER_ID, 1)
    await storage.insert_order(new_order(order_id, reference="ab" * 31 + "01"))
    await storage.attach_reservation(token, order_id)
    return token


async def holds_unit(storage):
    return await storage.reserve_stock(KEY, 999, 1) is None


async def test_rejecting_the_order_releases_the_unit(storage, monkeypatch):
    await create_reserved_order(storage, monkeypatch)
    assert await holds_unit(storage)

    await bot.reject_order(make_callback("reject:ord1"))

    assert not await holds_unit(storage)


async def test_a_failed_order_releases_the_unit(storage, monkeypatch):
    await create_reserved_order(storage, monkeypatch)
    claimed = await storage.claim_order("ord1")

    await bot.hold_order(FakeBot(), claimed, "supplier said no")

    assert not await holds_unit(storage)


async def test_an_order_parked_for_reconciliation_releases_the_unit(storage, monkeypatch):
    await create_reserved_order(storage, monkeypatch)
    claimed = await storage.claim_order("ord1")

    await bot.reconcile_order(FakeBot(), claimed, "unknown outcome")

    assert not await holds_unit(storage)


async def test_a_completed_purchase_releases_the_unit(storage, pandora, monkeypatch):
    await create_reserved_order(storage, monkeypatch)

    await bot.approve_order(make_callback("approve:ord1"))

    assert (await storage.get_order("ord1"))["status"] == "delivered"
    assert not await holds_unit(storage)


async def test_a_pending_order_keeps_its_unit_until_the_admin_decides(storage, monkeypatch):
    await create_reserved_order(storage, monkeypatch)

    assert await holds_unit(storage)  # nothing has released it yet

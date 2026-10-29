"""#11: when the live cost has risen above the order's price, the order is not auto-failed:
the admin sees the new cost, margin and difference and chooses Approve at new cost / Hold."""

from datetime import datetime, timedelta, timezone

import bot
from conftest import CUSTOMER_ID, FakeBot, make_callback, new_order

ADMIN = "111"


def quote_at(pandora, price):
    pandora.quote = {"can_purchase": True, "unit_price": price, "price_version": "v1"}


def admin_texts(fake_bot):
    return [(text, kw) for cid, text, kw in fake_bot.sent if str(cid) == ADMIN]


def buttons(kw):
    markup = kw.get("reply_markup")
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


async def approve(storage, pandora, price, order_id="ord1"):
    """Insert a fresh order (priced on cost 3.00) and approve it while the supplier asks `price`."""
    quote_at(pandora, price)
    await storage.insert_order(new_order(order_id))
    callback = make_callback(f"approve:{order_id}")
    await bot.approve_order(callback)
    return callback


# ---- a rise does not fail the order ---------------------------------------------------------------

async def test_a_price_rise_parks_the_order_for_the_admin_instead_of_failing_it(storage, pandora):
    callback = await approve(storage, pandora, "3.50")

    order = await storage.get_order("ord1")
    assert order["status"] == "price_review"  # not held, not failed
    assert order["price_review"]["old_cost"] == 3.0 and order["price_review"]["new_cost"] == 3.5
    assert pandora.purchase_posts() == []  # nothing was bought
    assert await storage.claim_order("ord1") is None  # the normal Approve path does not apply
    assert callback.bot.texts_to(CUSTOMER_ID) and bot.CUSTOMER_DELAY_NOTICE in callback.bot.texts_to(CUSTOMER_ID)


async def test_the_admin_sees_new_cost_margin_and_difference_with_two_choices(storage, pandora):
    callback = await approve(storage, pandora, "3.50")

    ((text, kw),) = admin_texts(callback.bot)
    assert "PRICE CHANGED" in text
    assert "Customer paid: 4.20 USDT" in text
    assert "Cost when ordered: 3.00" in text and "Cost now (pandora): 3.50" in text
    assert "Difference: +0.50 (+16.7%)" in text
    assert "Margin if you buy now: 0.70 USDT (16.7% of the price)" in text
    assert "Margin at order time: 1.20 USDT (28.6%)" in text
    assert "Nothing has been bought" in text
    assert buttons(kw) == ["pricego:ord1", "pricehold:ord1"]


async def test_selling_at_a_loss_is_called_out(storage, pandora):
    callback = await approve(storage, pandora, "5.00")

    ((text, _),) = admin_texts(callback.bot)
    assert "LOSS of 0.80 USDT" in text and "Margin if you buy now: -0.80" in text


async def test_a_lower_price_is_bought_straight_away_without_asking(storage, pandora):
    await approve(storage, pandora, "2.50")

    order = await storage.get_order("ord1")
    assert order["status"] == "delivered" and order["actual_cost"] == 2.5
    assert len(pandora.purchase_posts()) == 1


# ---- the admin's choices ---------------------------------------------------------------------------

async def test_approving_at_the_new_cost_buys_at_that_cost(storage, pandora):
    await approve(storage, pandora, "3.50")
    decision = make_callback("pricego:ord1")

    await bot.price_approve(decision)

    order = await storage.get_order("ord1")
    assert order["status"] == "delivered"
    assert order["actual_cost"] == 3.5 and order["approved_cost"] == 3.5
    (post,) = pandora.purchase_posts()
    assert post[2]["json"]["expected_unit_price"] == "3.50"
    assert any("user:pass" in t for t in decision.bot.texts_to(CUSTOMER_ID))


async def test_a_second_tap_cannot_buy_twice(storage, pandora):
    await approve(storage, pandora, "3.50")
    await bot.price_approve(make_callback("pricego:ord1"))
    second = make_callback("pricego:ord1")

    await bot.price_approve(second)

    assert len(pandora.purchase_posts()) == 1
    assert "not waiting for a price decision" in second.answer.await_args.args[0]


async def test_holding_leaves_the_order_for_retry_or_reject(storage, pandora):
    await approve(storage, pandora, "3.50")
    decision = make_callback("pricehold:ord1")

    await bot.price_hold(decision)

    order = await storage.get_order("ord1")
    assert order["status"] == "held" and "price change" in order["error"]
    assert pandora.purchase_posts() == []
    kw = decision.message.edit_text.await_args.kwargs
    assert [c.split(":")[0] for c in buttons(kw)] == ["approve", "reject"]


async def test_retrying_a_held_order_looks_at_the_price_again(storage, pandora):
    await approve(storage, pandora, "3.50")
    await bot.price_hold(make_callback("pricehold:ord1"))

    again = make_callback("approve:ord1")  # Retry, price still high
    await bot.approve_order(again)
    assert (await storage.get_order("ord1"))["status"] == "price_review"

    await bot.price_hold(make_callback("pricehold:ord1"))
    quote_at(pandora, "3.00")  # the supplier's price fell back
    await bot.approve_order(make_callback("approve:ord1"))
    assert (await storage.get_order("ord1"))["status"] == "delivered"


async def test_a_further_rise_after_approval_asks_again(storage, pandora):
    await approve(storage, pandora, "3.50")
    quote_at(pandora, "3.80")  # rose again before the admin's approval was acted on

    await bot.price_approve(make_callback("pricego:ord1"))

    order = await storage.get_order("ord1")
    assert order["status"] == "price_review" and order["price_review"]["new_cost"] == 3.8
    assert pandora.purchase_posts() == []


async def test_only_the_admin_can_decide(storage, pandora):
    await approve(storage, pandora, "3.50")
    for handler, data in ((bot.price_approve, "pricego:ord1"), (bot.price_hold, "pricehold:ord1")):
        outsider = make_callback(data, user_id=999)
        await handler(outsider)
        outsider.answer.assert_awaited_once_with("Not allowed", show_alert=True)

    assert (await storage.get_order("ord1"))["status"] == "price_review"
    assert pandora.purchase_posts() == []


# ---- durable alert ------------------------------------------------------------------------------------

async def test_the_price_decision_alert_is_retried_if_telegram_was_down(storage, pandora):
    quote_at(pandora, "3.50")
    await storage.insert_order(new_order("ord1"))
    await bot.approve_order(make_callback("approve:ord1", fake_bot=FakeBot(fail_for=[ADMIN])))
    stored = await storage.get_order("ord1")
    assert stored["status"] == "price_review" and stored["alert_pending"] is True
    assert stored["alert_kind"] == "price"

    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await storage._orders.update_one({"_id": "ord1"}, {"$set": {"alert_next_at": past}})
    later = FakeBot()
    await bot.flush_outbox(later)

    ((text, kw),) = admin_texts(later)
    assert "Cost now (pandora): 3.50" in text and buttons(kw) == ["pricego:ord1", "pricehold:ord1"]
    assert (await storage.get_order("ord1"))["alert_pending"] is False


async def test_an_alert_for_a_decision_already_taken_is_not_sent(storage, pandora):
    quote_at(pandora, "3.50")
    await storage.insert_order(new_order("ord1"))
    await bot.approve_order(make_callback("approve:ord1", fake_bot=FakeBot(fail_for=[ADMIN])))
    await bot.price_hold(make_callback("pricehold:ord1"))  # the admin found the order another way
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await storage._orders.update_one({"_id": "ord1"}, {"$set": {"alert_next_at": past, "alert_pending": True}})
    later = FakeBot()

    await bot.flush_outbox(later)

    assert admin_texts(later) == []

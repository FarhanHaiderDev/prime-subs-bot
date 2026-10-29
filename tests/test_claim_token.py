"""Fix 2: every write made for a claimed purchase filters on status AND claim_token,
so a stale attempt can never write onto a newer attempt's order."""

import asyncio
from dataclasses import asdict

import pytest

import bot
from conftest import ADMIN_ID, FakeBot, make_callback, new_order


async def _claim(storage, order_id="ord1", **order_kwargs):
    await storage.insert_order(new_order(order_id, **order_kwargs))
    claimed = await storage.claim_order(order_id)
    assert claimed and claimed["claim_token"]
    return claimed


async def _newer_attempt(storage, order_id):
    """Simulate: recovery -> admin unlock -> a second claim (a newer attempt)."""
    assert await storage.update_order(
        order_id, {"status": "reconcile", "outcome": "unknown"}, only_if_status="fulfilling"
    )
    assert await storage.unlock_order(order_id, ADMIN_ID, "Pandora confirmed none")
    return await storage.claim_order(order_id)


# ---- storage level -----------------------------------------------------------------

async def test_update_order_requires_the_matching_claim_token(storage):
    claimed = await _claim(storage)

    wrong = await storage.update_order(
        "ord1", {"error": "x"}, only_if_status="fulfilling", claim_token="not-the-token"
    )
    right = await storage.update_order(
        "ord1", {"error": "y"}, only_if_status="fulfilling", claim_token=claimed["claim_token"]
    )

    assert wrong is False
    assert right is True
    assert (await storage.get_order("ord1"))["error"] == "y"


async def test_claim_token_cannot_be_overwritten_through_fields(storage):
    claimed = await _claim(storage)
    with pytest.raises(ValueError):
        await storage.update_order("ord1", {"claim_token": "forged"}, claim_token=claimed["claim_token"])
    with pytest.raises(ValueError):
        await storage.update_order("ord1", {"error": "x"}, claim_token="")


async def test_a_new_claim_gets_a_different_token(storage):
    first = await _claim(storage)
    second = await _newer_attempt(storage, "ord1")

    assert second["claim_token"] != first["claim_token"]
    assert second["attempt"] == 1


# ---- persist_order: the stale writer ----------------------------------------------------

async def test_stale_attempt_cannot_write_onto_a_newer_attempts_order(storage):
    first = await _claim(storage)
    second = await _newer_attempt(storage, "ord1")
    delivered = {"status": "delivered", "outcome": "purchased", "credentials_x": ["stale"]}

    stale_result = await bot.persist_order("ord1", delivered, "fulfilling", first["claim_token"])

    assert stale_result == bot.STALE
    order = await storage.get_order("ord1")
    assert order["status"] == "fulfilling"  # the newer attempt's state is untouched
    assert order["claim_token"] == second["claim_token"]
    assert "credentials_x" not in order

    fresh_result = await bot.persist_order("ord1", delivered, "fulfilling", second["claim_token"])
    assert fresh_result == bot.SAVED


async def test_stale_writer_also_fails_from_the_reconcile_status(storage):
    first = await _claim(storage)
    assert await storage.update_order(
        "ord1", {"status": "reconcile", "outcome": "unknown"}, only_if_status="fulfilling"
    )
    # same attempt, recovered to reconcile: its own token still works...
    ok = await bot.persist_order("ord1", {"error": "same attempt"}, "reconcile", first["claim_token"])
    # ...another attempt's token does not
    other = await bot.persist_order("ord1", {"error": "other attempt"}, "reconcile", "some-other-token")

    assert ok == bot.SAVED
    assert other == bot.STALE


async def test_stale_hold_does_not_park_a_newer_attempt(storage):
    first = await _claim(storage)
    second = await _newer_attempt(storage, "ord1")
    fake_bot = FakeBot()

    await bot.hold_order(fake_bot, first, "late failure from the old attempt", attempt=5)

    order = await storage.get_order("ord1")
    assert order["status"] == "fulfilling"
    assert order["claim_token"] == second["claim_token"]
    assert order.get("attempt") == 1  # not bumped to 5
    admin_messages = [kw for cid, text, kw in fake_bot.sent if str(cid) == "111"]
    assert admin_messages and all(kw.get("reply_markup") is None for kw in admin_messages)


async def test_stale_reconcile_does_not_park_a_newer_attempt(storage):
    first = await _claim(storage)
    second = await _newer_attempt(storage, "ord1")

    await bot.reconcile_order(FakeBot(), first, "late ambiguity from the old attempt")

    order = await storage.get_order("ord1")
    assert order["status"] == "fulfilling"
    assert order["claim_token"] == second["claim_token"]


# ---- full approve flow with a stale handler ------------------------------------------------

async def test_approve_flow_whose_claim_was_superseded_does_not_overwrite(storage, monkeypatch):
    await storage.insert_order(new_order("ord9"))
    loop = asyncio.get_running_loop()

    def purchase_then_lose_the_claim(order, fence):
        # Supplier purchase succeeds, but meanwhile a newer attempt claimed the order.
        asyncio.run_coroutine_threadsafe(
            storage._orders.update_one({"_id": "ord9"}, {"$set": {"claim_token": "newer-token"}}), loop
        ).result(timeout=5)
        return ["user:pass"], bot.Offer("pandora", "p1", None, 3.0), []

    monkeypatch.setattr(bot, "fulfil", purchase_then_lose_the_claim)
    callback = make_callback("approve:ord9")

    await bot.approve_order(callback)

    order = await storage.get_order("ord9")
    assert order["status"] == "fulfilling"  # nothing was written by the stale handler
    assert order["claim_token"] == "newer-token"
    assert "credentials" not in order and "credentials_enc" not in order
    admin_alerts = [
        (text, kw) for cid, text, kw in callback.bot.sent if str(cid) == "111" and "user:pass" in text
    ]
    assert admin_alerts, "the purchased credentials must still reach the admin"
    text, kwargs = admin_alerts[0]
    assert "changed by something else" in text
    assert kwargs.get("protect_content") is True

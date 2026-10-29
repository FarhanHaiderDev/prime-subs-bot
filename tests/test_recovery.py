"""Startup recovery of interrupted orders, in every order state.

Runs on the in-memory database always, and on a real MongoDB too when MONGODB_TEST_URI is
set (see tests/README.md). Recovery uses an aggregation-pipeline update, which is exactly
the kind of operation worth checking against the real server.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import bot
from conftest import ADMIN_ID, CUSTOMER_ID, FakeBot, new_order
from storage import INTERRUPTED_REASON

SECRET = "user:pass"
_counter = 0


async def put(storage, order_id, status, **extra):
    """Insert an order in a given state, with a unique payment proof."""
    global _counter
    _counter += 1
    order = new_order(order_id, status=status, reference=f"{_counter:064x}", **extra)
    await storage.insert_order(order)
    return await storage.get_order(order_id)


async def snapshot(storage, order_id):
    doc = await storage.get_order(order_id)
    return {k: v for k, v in doc.items()}


# ---- interrupted (fulfilling) orders ------------------------------------------------------------

async def test_interrupted_order_becomes_reconcile_unknown(any_storage):
    await put(any_storage, "f1", "fulfilling", claim_token="t1")

    recovered = await any_storage.recover_interrupted_orders()

    assert [o["id"] for o in recovered] == ["f1"]
    order = await any_storage.get_order("f1")
    assert (order["status"], order["outcome"]) == ("reconcile", "unknown")
    assert order["error"] == INTERRUPTED_REASON
    assert order["claim_token"] == "t1"  # kept, so the fence/stale-writer checks still work
    assert order.get("attempt", 0) == 0  # recovery never changes the attempt


async def test_interrupted_order_already_known_to_be_purchased_stays_purchased(any_storage):
    await put(any_storage, "f2", "fulfilling", outcome="purchased", claim_token="t2")

    await any_storage.recover_interrupted_orders()

    order = await any_storage.get_order("f2")
    assert (order["status"], order["outcome"]) == ("reconcile", "purchased")


async def test_interrupted_order_from_an_unlocked_retry_is_unknown_again(any_storage):
    await put(any_storage, "f3", "fulfilling", outcome="verified_none", claim_token="t3", attempt=1)

    await any_storage.recover_interrupted_orders()

    order = await any_storage.get_order("f3")
    assert (order["status"], order["outcome"]) == ("reconcile", "unknown")
    assert order["attempt"] == 1


# ---- every other state is left alone ------------------------------------------------------------

UNTOUCHED = {
    "pending": {},
    "held": {"outcome": "definitive_failure", "attempt": 1},
    "reconcile": {"outcome": "unknown", "error": "earlier"},
    "delivered": {"outcome": "purchased"},
    "rejected": {},
    "delivery_failed": {"outcome": "purchased"},
    "purchased_undelivered": {"outcome": "purchased"},
}


async def test_orders_in_every_other_state_are_untouched_and_not_reported(any_storage):
    for status, extra in UNTOUCHED.items():
        await put(any_storage, f"o_{status}", status, **extra)
    before = {s: await snapshot(any_storage, f"o_{s}") for s in UNTOUCHED}

    recovered = await any_storage.recover_interrupted_orders()

    assert recovered == []
    for status in UNTOUCHED:
        assert await snapshot(any_storage, f"o_{status}") == before[status], status


async def test_mixed_batch_recovers_exactly_the_interrupted_orders(any_storage):
    for status, extra in UNTOUCHED.items():
        await put(any_storage, f"o_{status}", status, **extra)
    await put(any_storage, "fa", "fulfilling")
    await put(any_storage, "fb", "fulfilling", outcome="purchased")

    recovered = await any_storage.recover_interrupted_orders()

    assert sorted(o["id"] for o in recovered) == ["fa", "fb"]
    for status in UNTOUCHED:
        assert (await any_storage.get_order(f"o_{status}"))["status"] == status


async def test_recovery_is_idempotent(any_storage):
    await put(any_storage, "f1", "fulfilling")
    first = await any_storage.recover_interrupted_orders()
    after_first = await snapshot(any_storage, "f1")

    second = await any_storage.recover_interrupted_orders()

    assert len(first) == 1 and second == []
    assert await snapshot(any_storage, "f1") == after_first


# ---- what a recovered order may do next ------------------------------------------------------------

async def test_recovered_orders_cannot_be_claimed_rejected_or_written_by_the_old_attempt(any_storage):
    await put(any_storage, "f1", "pending")
    claimed = await any_storage.claim_order("f1")  # a real claim: status fulfilling + token
    await any_storage.recover_interrupted_orders()

    assert await any_storage.claim_order("f1") is None
    assert await any_storage.reject_order("f1") is None
    assert await any_storage.owns_claim("f1", claimed["claim_token"]) is False  # the fence fails
    stale = await bot.persist_order("f1", {"status": "delivered"}, "fulfilling", claimed["claim_token"])
    assert stale == bot.STALE
    assert (await any_storage.get_order("f1"))["status"] == "reconcile"


async def test_recovered_unknown_order_can_be_unlocked_but_a_purchased_one_cannot(any_storage):
    await put(any_storage, "fa", "fulfilling")
    await put(any_storage, "fb", "fulfilling", outcome="purchased")
    await any_storage.recover_interrupted_orders()

    unlocked = await any_storage.unlock_order("fa", ADMIN_ID, "Pandora confirmed no purchase")
    refused = await any_storage.unlock_order("fb", ADMIN_ID, "Pandora confirmed no purchase")

    assert unlocked and unlocked["status"] == "held" and unlocked["attempt"] == 1
    assert refused is None
    assert (await any_storage.get_order("fb"))["status"] == "reconcile"


async def test_purchased_undelivered_survives_a_restart_and_is_redelivered(any_storage, credential_box):
    """The full restart sequence: recovery first, then the background redelivery pass."""
    await put(
        any_storage,
        "u1",
        "purchased_undelivered",
        outcome="purchased",
        credentials_enc=credential_box.encrypt_list([SECRET]),
        purchased_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )

    recovered = await any_storage.recover_interrupted_orders()
    restart_bot = FakeBot()
    await bot.redeliver_undelivered(restart_bot)

    assert recovered == []
    order = await any_storage.get_order("u1")
    assert order["status"] == "delivered"
    assert order["delivered_at"]
    assert any(SECRET in text for text in restart_bot.texts_to(CUSTOMER_ID))


async def test_fulfilling_and_undelivered_orders_are_each_handled_by_their_own_path(any_storage, credential_box):
    await put(any_storage, "fa", "fulfilling")
    await put(
        any_storage,
        "u1",
        "purchased_undelivered",
        outcome="purchased",
        credentials_enc=credential_box.encrypt_list([SECRET]),
        purchased_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )

    recovered = await any_storage.recover_interrupted_orders()
    await bot.redeliver_undelivered(FakeBot())

    assert [o["id"] for o in recovered] == ["fa"]
    assert (await any_storage.get_order("fa"))["status"] == "reconcile"
    assert (await any_storage.get_order("u1"))["status"] == "delivered"


# ---- behaviours the in-memory database cannot fully prove, worth checking on a real server ------------

async def test_only_one_of_many_concurrent_claims_wins(any_storage):
    await put(any_storage, "race", "pending")

    results = await asyncio.gather(*[any_storage.claim_order("race") for _ in range(10)])

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert (await any_storage.get_order("race"))["claim_token"] == winners[0]["claim_token"]


async def test_unique_proof_index_is_enforced(any_storage):
    from pymongo.errors import DuplicateKeyError

    await put(any_storage, "p1", "pending")
    existing = await any_storage.get_order("p1")

    try:
        await any_storage.insert_order(new_order("p2", reference="0x" + existing["reference"]))
    except DuplicateKeyError:
        pass
    else:
        raise AssertionError("a second order was accepted for the same payment proof")

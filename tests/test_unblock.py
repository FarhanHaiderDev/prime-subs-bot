"""#16: /unblock frees the payment proof of a rejected order, for the admin only, with an audit log."""

import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
from conftest import ADMIN_ID, CUSTOMER_ID, TXID, FakeBot, FakeState, new_order

REASON = "Customer proved the payment to support"


def command_message(args, user_id=ADMIN_ID):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="A", username="a"), answer=AsyncMock(), bot=FakeBot()
    )
    return message, SimpleNamespace(args=args)


async def run(args, user_id=ADMIN_ID):
    message, command = command_message(args, user_id)
    await bot.cmd_unblock(message, command)
    return message


def reply(message):
    return message.answer.await_args.args[0] if message.answer.await_args else None


async def rejected_order(storage, order_id="ord1", reference=TXID):
    await storage.insert_order(new_order(order_id, status="rejected", reference=reference))


async def audit_entries(storage):
    return [doc async for doc in storage._db["audit_log"].find({})]


# ---- who may use it -------------------------------------------------------------------------

async def test_a_non_admin_is_silently_ignored_and_nothing_changes(any_storage):
    await rejected_order(any_storage)

    message = await run(f"{TXID} {REASON}", user_id=999)

    message.answer.assert_not_awaited()
    assert await any_storage.reference_exists(TXID)
    assert await audit_entries(any_storage) == []


async def test_a_reason_is_required(any_storage):
    await rejected_order(any_storage)
    for args in (None, "", TXID, f"{TXID} ab", f"{TXID} " + "x" * 201):
        message = await run(args)
        assert reply(message) == bot.UNBLOCK_USAGE, args

    assert await any_storage.reference_exists(TXID)
    assert await audit_entries(any_storage) == []


# ---- what it does -------------------------------------------------------------------------------

async def test_unblocking_a_rejected_proof_frees_it_and_records_who_when_and_why(any_storage):
    await rejected_order(any_storage)

    message = await run(f"{TXID} {REASON}")

    assert "can be used again" in reply(message) and "#ord1" in reply(message)
    order = await any_storage.get_order("ord1")
    assert "reference_lower" not in order and order["status"] == "rejected"
    assert order["reference"] == TXID  # the record of what was submitted is kept
    assert order["unblocked_by"] == str(ADMIN_ID) and order["unblock_reason"] == REASON and order["unblocked_at"]
    assert not await any_storage.reference_exists(TXID)


async def test_the_audit_log_entry_has_the_admin_order_reason_and_only_a_hash_of_the_proof(any_storage):
    await rejected_order(any_storage)

    await run(f"{TXID} {REASON}")

    (entry,) = await audit_entries(any_storage)
    assert entry["action"] == "unblock_reference" and entry["order_id"] == "ord1"
    assert entry["admin_id"] == str(ADMIN_ID) and entry["reason"] == REASON and entry["at"]
    assert entry["result"] == "done"
    assert entry["reference_sha256"] == hashlib.sha256(TXID.encode()).hexdigest()
    assert TXID not in str(entry)  # the proof itself is not in the log


async def test_the_proof_can_be_used_again_in_a_new_order(any_storage):
    await rejected_order(any_storage)
    await run(f"{TXID} {REASON}")

    await any_storage.insert_order(new_order("ord2", reference=TXID))  # no duplicate-key error

    assert (await any_storage.get_order("ord2"))["reference_lower"] == TXID
    assert await any_storage.reference_exists(TXID)


async def test_the_customer_can_resubmit_the_proof_through_the_normal_flow(storage):
    await rejected_order(storage)
    await run(f"{TXID} {REASON}")
    state = FakeState(
        {
            "method": "trc20",
            "name": "Netflix 1M",
            "price": 4.2,
            "offers": [{"supplier": "pandora", "product_id": "p1", "variant_id": None, "cost": 3.0}],
        }
    )
    message = SimpleNamespace(
        text=TXID,
        from_user=SimpleNamespace(id=CUSTOMER_ID, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=FakeBot(),
    )

    await bot.receive_reference(message, state)

    pending = [o async for o in storage._orders.find({"status": "pending"})]
    assert len(pending) == 1 and pending[0]["reference"] == TXID


async def test_other_spellings_of_the_same_proof_match(any_storage):
    await rejected_order(any_storage)

    message = await run(f"0x{TXID.upper()} {REASON}")

    assert "can be used again" in reply(message)


# ---- what it refuses ---------------------------------------------------------------------------------

async def test_only_rejected_orders_can_be_unblocked(any_storage):
    for status in ("pending", "held", "fulfilling", "reconcile", "delivered", "purchased_undelivered"):
        order_id = f"o_{status}"
        proof = f"{hash(status) & 0xFFFFFFFF:064x}"
        await any_storage.insert_order(new_order(order_id, status=status, reference=proof))

        message = await run(f"{proof} {REASON}")

        assert "not rejected" in reply(message) and status in reply(message), status
        assert (await any_storage.get_order(order_id))["reference_lower"] == proof

    assert await audit_entries(any_storage) == []  # nothing was attempted, so nothing is logged


async def test_an_unknown_proof_is_reported(any_storage):
    message = await run(f"{TXID} {REASON}")

    assert "No order uses that payment proof." in reply(message)
    assert await audit_entries(any_storage) == []


async def test_unblocking_twice_does_nothing_the_second_time(any_storage):
    await rejected_order(any_storage)
    await run(f"{TXID} {REASON}")

    second = await run(f"{TXID} {REASON}")

    assert "No order uses that payment proof." in reply(second)
    assert len(await audit_entries(any_storage)) == 1


async def test_once_the_proof_backs_a_new_order_it_cannot_be_unblocked_through_the_old_one(any_storage):
    await rejected_order(any_storage)
    await run(f"{TXID} {REASON}")
    await any_storage.insert_order(new_order("ord2", reference=TXID))

    message = await run(f"{TXID} {REASON}")

    assert "#ord2" in reply(message) and "not rejected" in reply(message)
    assert (await any_storage.get_order("ord2"))["reference_lower"] == TXID


# ---- consistency -------------------------------------------------------------------------------------------

async def test_the_startup_audit_does_not_flag_an_unblocked_order(any_storage):
    await rejected_order(any_storage)
    await run(f"{TXID} {REASON}")

    for full in (False, True):
        report = await any_storage.audit_references(full=full)
        assert not report.has_problems, full


async def test_the_proof_is_not_written_to_the_logs(any_storage, caplog):
    await rejected_order(any_storage)

    with caplog.at_level(logging.INFO):
        await run(f"{TXID} {REASON}")

    assert any("unblocked the payment proof" in r.getMessage() for r in caplog.records)
    assert TXID not in " ".join(r.getMessage() for r in caplog.records)

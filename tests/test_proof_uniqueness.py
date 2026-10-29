"""Fix 4: one payment proof (TXID / Binance order ID) can back only one order."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import DuplicateKeyError

import bot
from conftest import FakeBot, TXID, new_order
from storage import REFERENCE_INDEX, normalize_reference


# ---- canonical form -------------------------------------------------------------------

def test_txid_variants_share_one_canonical_form():
    assert (
        normalize_reference(TXID)
        == normalize_reference(TXID.upper())
        == normalize_reference("0x" + TXID)
        == normalize_reference("0X" + TXID.upper())
        == normalize_reference("  " + TXID + "  ")
        == TXID
    )


def test_binance_order_ids_are_trimmed_and_case_folded():
    assert normalize_reference(" ABC123def ") == "abc123def"


def test_0x_is_only_dropped_from_a_full_64_hex_hash():
    assert normalize_reference("0x1234") == "0x1234"
    assert normalize_reference("0x" + "g" * 64) == "0x" + "g" * 64


# ---- the unique index -------------------------------------------------------------------

async def test_unique_index_is_created(storage):
    report = await storage.ensure_indexes()
    assert report.has_problems is False


async def test_ensure_indexes_reports_the_reference_index(storage):
    # a fresh Storage on empty data creates both indexes
    from mongomock_motor import AsyncMongoMockClient
    from storage import Storage

    fresh = Storage("mongodb://unused.invalid", "fresh_db")
    fresh._client = AsyncMongoMockClient()
    report = await fresh.ensure_indexes()
    assert REFERENCE_INDEX in report.created


@pytest.mark.parametrize(
    "variant",
    [TXID.upper(), "0x" + TXID, "0X" + TXID.upper(), "  " + TXID + " "],
)
async def test_same_proof_cannot_back_a_second_order(storage, variant):
    await storage.insert_order(new_order("first", reference=TXID))

    with pytest.raises(DuplicateKeyError):
        await storage.insert_order(new_order("second", reference=variant))

    assert await storage.get_order("second") is None


async def test_a_different_proof_is_accepted(storage):
    await storage.insert_order(new_order("first", reference=TXID))
    await storage.insert_order(new_order("second", reference="cd" * 32))

    assert await storage.get_order("second") is not None


async def test_reference_exists_sees_every_variant(storage):
    await storage.insert_order(new_order("first", reference=TXID))

    assert await storage.reference_exists("0x" + TXID)
    assert await storage.reference_exists(TXID.upper())
    assert not await storage.reference_exists("cd" * 32)


async def test_startup_audit_refuses_existing_duplicates(storage):
    """Data that already holds two orders for one proof must not start the bot silently."""
    from mongomock_motor import AsyncMongoMockClient
    from storage import Storage

    dirty = Storage("mongodb://unused.invalid", "dirty_db")
    dirty._client = AsyncMongoMockClient()
    for order_id, ref in (("a", TXID), ("b", "0x" + TXID)):
        await dirty._orders.insert_one(
            {"_id": order_id, "id": order_id, "reference": ref, "reference_lower": ref.lower(), "user_id": 1}
        )

    report = await dirty.ensure_indexes()

    assert report.has_problems
    assert REFERENCE_INDEX not in report.created
    assert report.duplicate_references == {TXID: ["a", "b"]}


# ---- the user-facing handler -------------------------------------------------------------------

def _message(text, user_id=777):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=FakeBot(),
    )


def _state():
    data = {
        "method": "trc20",
        "name": "Netflix 1M",
        "price": 4.2,
        "offers": [{"supplier": "pandora", "product_id": "p1", "variant_id": None, "cost": 3.0}],
    }
    return SimpleNamespace(get_data=AsyncMock(return_value=data), clear=AsyncMock())


async def test_second_submission_of_the_same_proof_gets_a_clear_message(storage):
    first = _message(TXID, user_id=777)
    await bot.receive_reference(first, _state())
    assert [o async for o in storage._orders.find({})].__len__() == 1

    second, state = _message("0x" + TXID, user_id=888), _state()
    await bot.receive_reference(second, state)

    assert len([o async for o in storage._orders.find({})]) == 1  # no second order
    (answer,), _ = second.answer.call_args
    assert answer == bot.DUPLICATE_REFERENCE
    assert "already been used" in answer
    state.clear.assert_not_called()  # the user can still send a different proof


async def test_duplicate_key_race_is_reported_as_a_duplicate_proof(storage, monkeypatch):
    await storage.insert_order(new_order("first", reference=TXID))
    # simulate the race: the pre-check passes, the unique index then rejects the insert
    monkeypatch.setattr(storage, "reference_exists", AsyncMock(return_value=False))
    message, state = _message(TXID, user_id=888), _state()

    await bot.receive_reference(message, state)

    (answer,), _ = message.answer.call_args
    assert answer == bot.DUPLICATE_REFERENCE
    state.clear.assert_not_called()
    assert len([o async for o in storage._orders.find({})]) == 1

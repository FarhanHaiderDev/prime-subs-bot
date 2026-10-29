"""#21: indexes for the status-based scans, and a startup audit that reads only the orders
that are not finished instead of the whole collection."""

import logging

import pytest
from mongomock_motor import AsyncMongoMockClient

from conftest import TXID
from storage import ORDER_INDEXES, REFERENCE_INDEX, TERMINAL_STATUSES

OTHER = "cd" * 32


async def drop_unique_index(storage):
    """Simulate a database that predates the unique index, so duplicates can be planted."""
    await storage._orders.drop_index(REFERENCE_INDEX)


async def raw_order(storage, order_id, status, reference, reference_lower="same-as-reference", **extra):
    """Insert an order document directly, bypassing insert_order (to plant bad data)."""
    if reference_lower == "same-as-reference":
        reference_lower = reference.lower() if reference else None
    doc = {"_id": order_id, "id": order_id, "status": status, "user_id": 1, "reference": reference, **extra}
    if reference_lower is not None:
        doc["reference_lower"] = reference_lower
    await storage._orders.insert_one(doc)


# ---- the indexes ----------------------------------------------------------------------------------

async def test_all_status_scan_indexes_and_the_unique_index_exist(any_storage):
    report = await any_storage.ensure_indexes()

    names = set((await any_storage._orders.index_information()))
    assert {name for name, _, _ in ORDER_INDEXES} <= names and REFERENCE_INDEX in names
    assert report.has_problems is False and REFERENCE_INDEX in report.created
    for needed in ("status_created", "status_notified", "status_purchased", "alert_outbox", "notice_outbox"):
        assert needed in names


async def test_ensure_indexes_is_idempotent_on_a_healthy_database(any_storage):
    await raw_order(any_storage, "a", "pending", TXID)
    await raw_order(any_storage, "b", "delivered", OTHER)

    first = await any_storage.ensure_indexes()
    second = await any_storage.ensure_indexes()

    assert not first.has_problems and not second.has_problems
    assert second.scanned == "active"  # no slow path on a normal restart


# ---- the audit reads only unfinished orders ------------------------------------------------------------

async def test_finished_orders_are_not_part_of_the_startup_audit(any_storage):
    await raw_order(any_storage, "live", "pending", TXID)
    # bad data in FINISHED orders: a missing key, a stale key, no reference at all
    await raw_order(any_storage, "d1", "delivered", OTHER, reference_lower=None)
    await raw_order(any_storage, "d2", "rejected", "ef" * 32, reference_lower="stale-value")
    await raw_order(any_storage, "d3", "delivery_failed", "", reference_lower=None)

    quick = await any_storage.audit_references()
    thorough = await any_storage.audit_references(full=True)

    assert quick.scanned == "active" and not quick.has_problems
    assert thorough.scanned == "full"
    assert thorough.missing_reference_lower == ["d1"]
    assert thorough.stale_reference_lower == ["d2"]
    assert thorough.invalid_reference == ["d3"]


async def test_problems_in_unfinished_orders_are_still_caught_quickly(any_storage):
    await raw_order(any_storage, "p1", "pending", TXID, reference_lower=None)
    await raw_order(any_storage, "h1", "held", OTHER, reference_lower="stale-value")
    await raw_order(any_storage, "odd", "some-future-status", "ef" * 32, reference_lower=None)

    report = await any_storage.ensure_indexes()

    assert report.has_problems and report.scanned == "active"
    assert report.missing_reference_lower == ["odd", "p1"] and report.stale_reference_lower == ["h1"]
    assert REFERENCE_INDEX not in report.created  # not built over bad data


async def test_every_terminal_status_is_skipped_and_every_other_is_audited(any_storage):
    assert set(TERMINAL_STATUSES) == {"delivered", "rejected", "delivery_failed"}
    for i, status in enumerate(["pending", "held", "fulfilling", "reconcile", "price_review", "purchased_undelivered"]):
        await raw_order(any_storage, f"x{i}", status, f"{i:064x}", reference_lower=None)

    report = await any_storage.audit_references()

    assert len(report.missing_reference_lower) == 6


# ---- the slow path: only when the unique index cannot be built ----------------------------------------------

async def test_duplicates_among_finished_orders_trigger_one_full_scan_and_stop_startup(any_storage, caplog):
    await drop_unique_index(any_storage)
    await raw_order(any_storage, "d1", "delivered", TXID)
    await raw_order(any_storage, "d2", "delivered", TXID)  # two finished orders, one payment proof

    with caplog.at_level(logging.ERROR):
        report = await any_storage.ensure_indexes()

    assert report.index_blocked and report.has_problems and report.scanned == "full"
    assert report.duplicate_references == {TXID: ["d1", "d2"]}
    assert REFERENCE_INDEX not in report.created
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "d1" in logged and "d2" in logged
    assert TXID not in logged  # order ids and counts only, never the payment proof


async def test_a_blocked_index_is_reported_even_if_the_scan_cannot_explain_it(any_storage):
    """Same stored key on two orders whose references differ: the index fails, the audit by
    canonical reference finds no duplicate, and startup must still stop."""
    await drop_unique_index(any_storage)
    await raw_order(any_storage, "a", "delivered", TXID, reference_lower="shared-key")
    await raw_order(any_storage, "b", "delivered", OTHER, reference_lower="shared-key")

    report = await any_storage.ensure_indexes()

    assert report.index_blocked and report.has_problems
    assert sorted(report.stale_reference_lower) == ["a", "b"]


# ---- on a real server only ---------------------------------------------------------------------------------

async def test_the_audit_query_is_served_by_the_status_index(any_storage):
    if isinstance(any_storage._client, AsyncMongoMockClient):
        pytest.skip("query plans only exist on a real MongoDB")
    await raw_order(any_storage, "a", "pending", TXID)

    plan = await any_storage._orders.find({"status": {"$nin": TERMINAL_STATUSES}}).hint("status_created").explain()

    assert "IXSCAN" in str(plan) and "COLLSCAN" not in str(plan)

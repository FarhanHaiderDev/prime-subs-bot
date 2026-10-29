"""MongoDB storage for orders and language preferences.

Database errors (pymongo.errors.PyMongoError and subclasses) propagate to the
caller on purpose, so each call site can decide how to degrade.
"""

import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument

SERVER_SELECTION_TIMEOUT_MS = 10_000
DEFAULT_DB = "prime_subs_bot"

SUPPORTED_LANGS = frozenset({"en", "hi", "es", "zh"})
DEFAULT_LANG = "en"

# Fields the caller may not overwrite through update_order().
_PROTECTED_FIELDS = frozenset({"_id", "id", "created_at", "reference_lower"})

INTERRUPTED_REASON = (
    "Interrupted: the bot stopped while this order was being fulfilled. "
    "The supplier purchase may or may not have completed - check the supplier "
    "and reconcile manually."
)

# Supplier-outcome states stored in an order's `outcome` field:
#   (absent)            no purchase attempted yet
#   "definitive_failure" the code proved no purchase happened (normal Retry allowed)
#   "unknown"           the purchase may or may not have happened (manual reconciliation)
#   "purchased"         the purchase completed (never retry; resolve manually if unresolved)
#   "verified_none"     an admin confirmed no purchase exists (Retry allowed again)
# Orders with these outcomes can't be claimed for fulfilment or rejected.
BLOCKING_OUTCOMES = ["unknown", "purchased"]

REFERENCE_INDEX = "reference_lower_unique"
USER_ORDERS_INDEX = "user_id_created_at"


def resolve_lang(value: object) -> str:
    """Unsupported or legacy values (e.g. the removed 'ur') resolve to English."""
    return value if isinstance(value, str) and value in SUPPORTED_LANGS else DEFAULT_LANG


def normalize_reference(reference: object) -> str:
    """The one canonical form of a payment reference: trimmed and case-folded."""
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("reference must be a non-empty string")
    return reference.strip().casefold()


def coerce_created_at(value: object) -> datetime:
    """Return a timezone-aware UTC datetime.

    None -> now. datetime -> as is. str -> ISO 8601 (a trailing 'Z' is accepted).
    Naive values are taken to be UTC; aware values are converted to UTC.
    Anything else, or an unparseable string, raises ValueError.
    """
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"created_at is not a valid ISO 8601 timestamp: {value!r}") from None
    if not isinstance(value, datetime):
        raise ValueError(f"created_at must be a datetime or ISO 8601 string, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class IndexReport:
    created: list[str] = field(default_factory=list)
    # canonical reference -> ids of the orders sharing it (2 or more).
    duplicate_references: dict[str, list[str]] = field(default_factory=dict)
    # Order ids whose reference_lower is absent or not a string.
    missing_reference_lower: list[str] = field(default_factory=list)
    # Order ids whose reference_lower differs from the canonical form of `reference`.
    stale_reference_lower: list[str] = field(default_factory=list)
    # Order ids whose `reference` itself is absent, blank or not a string.
    invalid_reference: list[str] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(
            self.duplicate_references
            or self.missing_reference_lower
            or self.stale_reference_lower
            or self.invalid_reference
        )


class Storage:
    def __init__(self, uri: str, db_name: str = DEFAULT_DB):
        self._uri = uri
        self._db_name = db_name
        self._client: AsyncMongoClient | None = None

    @classmethod
    def from_env(cls) -> "Storage":
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI environment variable is not set.")
        return cls(uri, os.getenv("MONGODB_DB") or DEFAULT_DB)

    async def connect(self) -> None:
        """Create the client and ping the server (10 s server-selection timeout)."""
        if self._client is not None:
            return
        client = AsyncMongoClient(
            self._uri,
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,  # datetimes read back are timezone-aware UTC
        )
        try:
            await client.admin.command("ping")
        except BaseException:
            await client.close()
            raise
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.close()

    @property
    def _db(self):
        if self._client is None:
            raise RuntimeError("Storage is not connected; call connect() first.")
        return self._client[self._db_name]

    @property
    def _orders(self):
        return self._db["orders"]

    @property
    def _prefs(self):
        return self._db["prefs"]

    # ---- Indexes (separate from connect so callers decide when to run it) ----

    async def audit_references(self) -> IndexReport:
        """Read-only check of every order's reference fields.

        The canonical reference is always derived from `reference`, so duplicates
        are found even when `reference_lower` is missing or stale.
        """
        report = IndexReport()
        by_canonical: dict[str, list[str]] = {}

        cursor = self._orders.find({}, {"reference": 1, "reference_lower": 1})
        async for doc in cursor:
            order_id = doc["_id"]
            try:
                canonical = normalize_reference(doc.get("reference"))
            except ValueError:
                report.invalid_reference.append(order_id)
                continue
            by_canonical.setdefault(canonical, []).append(order_id)

            stored = doc.get("reference_lower")
            if not isinstance(stored, str):
                report.missing_reference_lower.append(order_id)
            elif stored != canonical:
                report.stale_reference_lower.append(order_id)

        report.duplicate_references = {
            ref: sorted(ids) for ref, ids in by_canonical.items() if len(ids) > 1
        }
        report.invalid_reference.sort()
        report.missing_reference_lower.sort()
        report.stale_reference_lower.sort()
        return report

    async def ensure_indexes(self) -> IndexReport:
        """Create indexes. Never deletes or modifies orders.

        The (user_id, created_at, _id) index is always created. The unique
        reference index is created only when the audit finds no duplicate,
        missing, stale or invalid reference data; otherwise the affected order
        ids are logged and returned for a human to resolve.
        """
        await self._orders.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
            name=USER_ORDERS_INDEX,
        )

        report = await self.audit_references()
        report.created.append(USER_ORDERS_INDEX)

        if report.has_problems:
            # Order ids and counts only: the duplicate map is keyed by payment references,
            # so it is never logged.
            duplicate_order_ids = sorted({oid for ids in report.duplicate_references.values() for oid in ids})
            logging.error(
                "Reference audit found problems; unique index NOT created. "
                "duplicate_groups=%d duplicate_order_ids=%s missing_reference_lower=%s "
                "stale_reference_lower=%s invalid_reference=%s",
                len(report.duplicate_references),
                duplicate_order_ids,
                report.missing_reference_lower,
                report.stale_reference_lower,
                report.invalid_reference,
            )
            return report

        await self._orders.create_index(
            "reference_lower",
            unique=True,
            name=REFERENCE_INDEX,
            partialFilterExpression={"reference_lower": {"$type": "string"}},
        )
        report.created.append(REFERENCE_INDEX)
        return report

    # ---- Orders ---------------------------------------------------------------

    async def insert_order(self, order: dict) -> dict:
        """Insert a new order and return the stored document.

        `order["id"]` becomes the string _id. created_at is a timezone-aware UTC
        datetime (now when absent/None; a supplied datetime or ISO string is
        validated and converted). reference_lower is always derived from
        `reference`. Raises ValueError for an invalid reference or created_at,
        and DuplicateKeyError if the id, or (once the unique index exists) the
        reference, is already stored.
        """
        doc = dict(order)
        doc["_id"] = str(doc["id"])
        doc["created_at"] = coerce_created_at(doc.get("created_at"))
        doc["reference_lower"] = normalize_reference(doc.get("reference"))
        await self._orders.insert_one(doc)
        return doc

    async def get_order(self, order_id: str) -> dict | None:
        return await self._orders.find_one({"_id": order_id})

    async def update_order(
        self,
        order_id: str,
        fields: dict,
        *,
        only_if_status: str | Iterable[str] | None = None,
    ) -> bool:
        """$set `fields` on an order. Returns True if an order was matched.

        With only_if_status (a single status string, or an iterable of them), the
        update applies only while the order's current status is one of those
        values (a compare-and-set for status changes). Updating `reference`
        also refreshes reference_lower.
        """
        if not fields:
            raise ValueError("update_order requires at least one field to set")
        forbidden = _PROTECTED_FIELDS.intersection(fields)
        if forbidden:
            raise ValueError(f"Cannot update protected order fields: {sorted(forbidden)}")

        update = dict(fields)
        if "reference" in update:
            update["reference_lower"] = normalize_reference(update["reference"])

        query: dict = {"_id": order_id}
        if only_if_status is not None:
            statuses = [only_if_status] if isinstance(only_if_status, str) else list(only_if_status)
            if not statuses or not all(isinstance(s, str) and s for s in statuses):
                raise ValueError("only_if_status must be a status string or a non-empty list of them")
            query["status"] = {"$in": statuses}

        result = await self._orders.update_one(query, {"$set": update})
        return result.matched_count == 1

    async def claim_order(self, order_id: str) -> dict | None:
        """Atomically move an order from pending/held to fulfilling.

        Returns the updated order if this call claimed it, otherwise None (the
        order doesn't exist, is in any other state, or needs manual reconciliation).
        """
        return await self._orders.find_one_and_update(
            {
                "_id": order_id,
                "status": {"$in": ["pending", "held"]},
                "outcome": {"$nin": BLOCKING_OUTCOMES},
            },
            {"$set": {"status": "fulfilling", "claim_token": uuid.uuid4().hex}},
            return_document=ReturnDocument.AFTER,
        )

    async def owns_claim(self, order_id: str, claim_token: str | None) -> bool:
        """Fence check: True only while the order is still `fulfilling` under this
        exact claim token. Recovery, reconciliation or a newer claim all invalidate it."""
        if not claim_token:
            return False
        doc = await self._orders.find_one(
            {"_id": order_id, "status": "fulfilling", "claim_token": claim_token}, {"_id": 1}
        )
        return doc is not None

    async def reject_order(self, order_id: str) -> dict | None:
        """Atomically reject a pending/held order that doesn't need reconciliation.

        Returns the updated order, or None if it can't be rejected (missing, in
        another state, or its supplier outcome is unknown/purchased).
        """
        return await self._orders.find_one_and_update(
            {
                "_id": order_id,
                "status": {"$in": ["pending", "held"]},
                "outcome": {"$nin": BLOCKING_OUTCOMES},
            },
            {"$set": {"status": "rejected"}},
            return_document=ReturnDocument.AFTER,
        )

    async def unlock_order(self, order_id: str, admin_id: int, note: str) -> dict | None:
        """Record an admin's attestation that the supplier confirmed no purchase exists and
        no submitted request is still pending. The bot cannot check this itself.

        Only an order awaiting reconciliation with an *unknown* outcome can be
        unlocked; a purchased order never can. The order returns to `held`, the
        attempt counter is incremented (so the next purchase uses a fresh supplier
        idempotency key), and the admin id, the time and the admin's confirmation
        note are stored on the order.
        """
        note = " ".join(str(note).split())
        if not 3 <= len(note) <= 200:
            raise ValueError("the confirmation note must be 3 to 200 characters")
        return await self._orders.find_one_and_update(
            {
                "_id": order_id,
                "status": "reconcile",
                "outcome": "unknown",
            },
            {
                "$set": {
                    "status": "held",
                    "outcome": "verified_none",
                    "reconciled_by": str(admin_id),
                    "reconciled_at": datetime.now(timezone.utc),
                    "reconciliation_note": note,
                },
                "$inc": {"attempt": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def recover_interrupted_orders(self) -> list[dict]:
        """Move orders left in `fulfilling` to `reconcile`, with an explanatory `error`.

        The supplier outcome of an interrupted order is unknown, so it is set to
        "unknown" (an already-recorded "purchased" outcome is kept). These orders
        need manual reconciliation: they can't be claimed or rejected. Each order
        is changed only if it is still `fulfilling`, so a concurrent change is never
        overwritten. Returns just the orders this call recovered. Never contacts a
        supplier and never changes `attempt`.
        """
        cursor = self._orders.find({"status": "fulfilling"}, {"_id": 1})
        stuck_ids = [doc["_id"] async for doc in cursor]

        recovered = []
        for order_id in stuck_ids:
            order = await self._orders.find_one_and_update(
                {"_id": order_id, "status": "fulfilling"},
                [
                    {
                        "$set": {
                            "status": "reconcile",
                            "outcome": {
                                "$cond": [{"$eq": ["$outcome", "purchased"]}, "purchased", "unknown"]
                            },
                            "error": INTERRUPTED_REASON,
                        }
                    }
                ],
                return_document=ReturnDocument.AFTER,
            )
            if order is not None:
                recovered.append(order)
        return recovered

    async def pending_unnotified(self, limit: int = 20) -> list[dict]:
        """Saved `pending` orders whose new-order admin alert is not confirmed delivered.

        Only orders explicitly marked `admin_notified: False` are returned (orders saved
        before the marker existed have no field and are not included), oldest first."""
        cursor = (
            self._orders.find({"status": "pending", "admin_notified": False})
            .sort([("created_at", ASCENDING), ("_id", ASCENDING)])
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def mark_admin_notified(self, order_id: str) -> bool:
        """Record that Telegram confirmed delivery of the new-order alert. Touches only the
        notification marker, never the order's payment or fulfilment state."""
        result = await self._orders.update_one(
            {"_id": order_id, "admin_notified": False},
            {"$set": {"admin_notified": True, "admin_notified_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count == 1

    async def recent_orders(self, user_id: int, limit: int = 5) -> list[dict]:
        """A user's most recent orders, newest first.

        Sorted by created_at descending, then _id descending, so the order is
        deterministic. Orders with no created_at sort last.
        """
        cursor = (
            self._orders.find({"user_id": user_id})
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def reference_exists(self, reference: str) -> bool:
        """Case-insensitive duplicate check across all orders, whatever their status."""
        doc = await self._orders.find_one({"reference_lower": normalize_reference(reference)}, {"_id": 1})
        return doc is not None

    # ---- Preferences ----------------------------------------------------------

    async def get_lang(self, user_id: int) -> str:
        doc = await self._prefs.find_one({"_id": str(user_id)})
        return resolve_lang(doc.get("lang") if doc else None)

    async def set_lang(self, user_id: int, lang: str) -> None:
        if lang not in SUPPORTED_LANGS:
            raise ValueError(f"Unsupported language: {lang!r}")
        await self._prefs.update_one({"_id": str(user_id)}, {"$set": {"lang": lang}}, upsert=True)

    async def all_langs(self) -> dict[str, str]:
        """Every stored preference as {user_id: lang}, legacy values resolved to 'en'."""
        return {doc["_id"]: resolve_lang(doc.get("lang")) async for doc in self._prefs.find({})}

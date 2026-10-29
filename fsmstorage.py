"""aiogram FSM storage backed by MongoDB, so checkout, search and unlock flows survive a restart.

One document per FSM key (bot, chat, user, thread, destiny): {_id, state, data, updated_at}.
Abandoned flows are cleaned up by a TTL index on `updated_at` (see Storage.ensure_indexes).
The data holds only what the flows put there (order name, price, offers, a reservation
token, an unlock nonce): never credentials or secrets. Database errors propagate to aiogram,
where the global error handler answers the user neutrally.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

FSM_TTL_SECONDS = 7 * 24 * 3600


def document_id(key: StorageKey) -> str:
    parts = (
        key.bot_id,
        getattr(key, "business_connection_id", None) or "-",
        key.chat_id,
        key.user_id,
        key.thread_id or 0,
        key.destiny,
    )
    return ":".join(str(part) for part in parts)


class MongoFSMStorage(BaseStorage):
    def __init__(self, collection):
        self._collection = collection

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._collection.update_one(
            {"_id": document_id(key)},
            {"$set": {"state": value, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_state(self, key: StorageKey) -> Optional[str]:
        doc = await self._collection.find_one({"_id": document_id(key)}, {"state": 1})
        return doc.get("state") if doc else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError(f"FSM data must be a dict, got {type(data).__name__}")
        await self._collection.update_one(
            {"_id": document_id(key)},
            {"$set": {"data": dict(data), "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        if not data:  # a cleared flow leaves nothing behind
            await self._collection.delete_one({"_id": document_id(key), "state": None, "data": {}})

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        doc = await self._collection.find_one({"_id": document_id(key)}, {"data": 1})
        return dict(doc.get("data") or {}) if doc else {}

    async def close(self) -> None:
        pass  # the MongoDB client belongs to Storage, which closes it

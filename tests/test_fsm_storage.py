"""#20: conversation state (checkout, search, unlock flows) is kept in MongoDB and survives a restart."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Message, MessageEntity, Update, User

import bot
from conftest import ADMIN_ID, CUSTOMER_ID, TXID, FakeBot, make_callback, new_order
from fsmstorage import MongoFSMStorage, document_id

KEY = StorageKey(bot_id=1, chat_id=10, user_id=20)


def fsm(storage):
    return MongoFSMStorage(storage.fsm_collection)


def context(db_storage, user_id=CUSTOMER_ID):
    """A fresh storage object over the same collection, as after a restart."""
    return FSMContext(fsm(db_storage), StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


# ---- the storage ---------------------------------------------------------------------------------

async def test_state_and_data_round_trip_including_state_objects(any_storage):
    store = fsm(any_storage)
    assert await store.get_state(KEY) is None and await store.get_data(KEY) == {}

    await store.set_state(KEY, bot.Checkout.waiting_reference)
    await store.set_data(KEY, {"price": 4.2, "offers": [{"supplier": "pandora", "variant_id": None, "cost": 3.0}]})

    assert await store.get_state(KEY) == "Checkout:waiting_reference"
    assert await store.get_data(KEY) == {
        "price": 4.2,
        "offers": [{"supplier": "pandora", "variant_id": None, "cost": 3.0}],
    }
    await store.set_state(KEY, "Search:waiting_query")  # plain strings work too
    assert await store.get_state(KEY) == "Search:waiting_query"


async def test_keys_are_isolated_per_chat_user_thread_and_destiny(any_storage):
    store = fsm(any_storage)
    keys = [
        StorageKey(bot_id=1, chat_id=10, user_id=20),
        StorageKey(bot_id=1, chat_id=10, user_id=21),
        StorageKey(bot_id=1, chat_id=11, user_id=20),
        StorageKey(bot_id=1, chat_id=10, user_id=20, thread_id=5),
        StorageKey(bot_id=1, chat_id=10, user_id=20, destiny="other"),
        StorageKey(bot_id=2, chat_id=10, user_id=20),
    ]
    assert len({document_id(k) for k in keys}) == len(keys)
    for i, key in enumerate(keys):
        await store.set_data(key, {"n": i})
    for i, key in enumerate(keys):
        assert await store.get_data(key) == {"n": i}


async def test_returned_data_is_a_copy(any_storage):
    store = fsm(any_storage)
    await store.set_data(KEY, {"a": 1})

    (await store.get_data(KEY))["a"] = 99

    assert await store.get_data(KEY) == {"a": 1}


async def test_update_data_merges_and_clear_removes_everything(any_storage):
    ctx = FSMContext(fsm(any_storage), KEY)
    await ctx.set_state(bot.Search.waiting_query)
    await ctx.update_data(a=1)
    await ctx.update_data(b=2)
    assert await ctx.get_data() == {"a": 1, "b": 2}

    await ctx.clear()

    assert await ctx.get_state() is None and await ctx.get_data() == {}
    assert await any_storage.fsm_collection.find_one({"_id": document_id(KEY)}) is None


async def test_non_dict_data_is_rejected(any_storage):
    with pytest.raises(TypeError):
        await fsm(any_storage).set_data(KEY, ["not", "a", "dict"])


async def test_abandoned_flows_expire_through_a_ttl_index(any_storage):
    info = await any_storage.fsm_collection.index_information()
    ttl = [spec for spec in info.values() if spec.get("expireAfterSeconds") is not None]
    assert ttl and ttl[0]["expireAfterSeconds"] == 7 * 24 * 3600


# ---- surviving a restart ---------------------------------------------------------------------------

async def test_a_checkout_continues_after_a_restart(storage, monkeypatch):
    """The buyer chooses a payment method; the bot restarts; their payment proof still works."""
    listing = bot.Listing("k1", "Netflix 1M", "", [bot.Offer("pandora", "p1", None, 3.0, 4)])

    async def fake_catalog(force=False):
        return {"k1": listing}

    monkeypatch.setattr(bot, "get_catalog", fake_catalog)
    before_restart = context(storage)
    await bot.choose_payment(make_callback("pay:trc20:k1", user_id=CUSTOMER_ID), before_restart)

    after_restart = context(storage)  # a brand-new storage object: nothing in memory
    assert await after_restart.get_state() == "Checkout:waiting_reference"
    assert (await after_restart.get_data())["reservation_token"]
    message = SimpleNamespace(
        text=TXID,
        from_user=SimpleNamespace(id=CUSTOMER_ID, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=FakeBot(),
    )
    await bot.receive_reference(message, after_restart)

    order = await storage._orders.find_one({})
    assert order is not None and order["user_id"] == CUSTOMER_ID and order["stock_reservation"] == "held"
    assert await after_restart.get_state() is None  # the flow finished and cleaned up


async def test_a_search_prompt_survives_a_restart(storage):
    ctx = context(storage)
    await bot.cb_search(make_callback("search", user_id=CUSTOMER_ID), ctx)

    assert await context(storage).get_state() == "Search:waiting_query"


async def test_the_unlock_flow_survives_restarts_at_every_step(storage):
    await storage.insert_order(new_order("ord1", status="reconcile", outcome="unknown", reference="cd" * 32))

    await bot.unlock_start(make_callback("unlock:ord1"), context(storage, ADMIN_ID))  # restart
    assert await context(storage, ADMIN_ID).get_state() == "UnlockFlow:waiting_note"

    note = SimpleNamespace(
        text="Pandora ref 12345 confirmed none",
        from_user=SimpleNamespace(id=ADMIN_ID, full_name="A", username="a"),
        answer=AsyncMock(),
    )
    await bot.unlock_note(note, context(storage, ADMIN_ID))  # restart
    restored = context(storage, ADMIN_ID)
    assert await restored.get_state() == "UnlockFlow:confirming"
    nonce = (await restored.get_data())["nonce"]

    await bot.unlock_confirm(make_callback(f"unlockgo:{nonce}"), context(storage, ADMIN_ID))  # restart

    order = await storage.get_order("ord1")
    assert order["status"] == "held" and order["reconciled_by"] == str(ADMIN_ID)
    assert await context(storage, ADMIN_ID).get_state() is None


# ---- wiring ------------------------------------------------------------------------------------------

def test_startup_installs_the_mongodb_storage_in_the_dispatcher(storage, monkeypatch):
    monkeypatch.setattr(bot.dp.fsm, "storage", bot.dp.fsm.storage)  # restored after the test

    bot.install_fsm_storage(storage)

    assert isinstance(bot.dp.storage, MongoFSMStorage)


async def test_the_real_dispatcher_persists_state_to_mongodb(storage, monkeypatch):
    async def fake_call(self, method, request_timeout=None):
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    monkeypatch.setattr(bot.dp.fsm, "storage", bot.dp.fsm.storage)
    bot.install_fsm_storage(storage)
    real_bot = Bot(token="123456:" + "A" * 35)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=4242, type="private"),
            from_user=User(id=4242, is_bot=False, first_name="Cust"),
            text="/search",
            entities=[MessageEntity(type="bot_command", offset=0, length=7)],
        ),
    )

    await bot.dp.feed_update(real_bot, update)

    docs = [d async for d in storage.fsm_collection.find({})]
    assert len(docs) == 1 and docs[0]["state"] == "Search:waiting_query"
    assert docs[0]["_id"].startswith(f"{real_bot.id}:")

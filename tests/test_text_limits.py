"""#27: Telegram's 4096 limit is counted in UTF-16 code units; #24: the Bot session is closed on
every way out of startup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import PyMongoError

import bot
from conftest import CUSTOMER_ID, FakeBot, make_callback, new_order
from textutil import split_utf16, truncate_utf16, utf16_len

EMOJI = "😀"  # one code point, two UTF-16 units


# ---- the helpers ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,units",
    [("", 0), ("abc", 3), ("é", 1), ("日本語", 3), (EMOJI, 2), ("a" + EMOJI + "b", 4), (EMOJI * 3, 6)],
)
def test_utf16_len_counts_like_telegram(text, units):
    assert utf16_len(text) == units
    assert utf16_len(text) == len(text.encode("utf-16-le")) // 2  # the definition


def test_truncate_never_exceeds_the_limit_and_never_splits_a_character():
    assert truncate_utf16("a" + EMOJI + "b", 2) == "a"  # the emoji would need units 2-3
    assert truncate_utf16(EMOJI * 2, 3) == EMOJI
    assert truncate_utf16(EMOJI * 2, 4) == EMOJI * 2
    assert truncate_utf16("abc", 10) == "abc" and truncate_utf16("abc", 0) == "" and truncate_utf16("", 5) == ""
    for limit in range(0, 12):
        cut = truncate_utf16("x" + EMOJI * 5 + "yz", limit)
        assert utf16_len(cut) <= limit
        cut.encode("utf-16-le")  # would raise on a split surrogate pair


def test_split_pieces_fit_and_rejoin_to_the_original():
    text = "line" + EMOJI * 3000 + "é" * 50
    pieces = split_utf16(text, 4000)

    assert "".join(pieces) == text
    assert all(0 < utf16_len(p) <= 4000 for p in pieces)
    assert len(pieces) == 2 and utf16_len(pieces[0]) == 4000
    assert split_utf16("", 10) == [] and split_utf16("abc", 10) == ["abc"]
    assert split_utf16(EMOJI * 3, 3) == [EMOJI, EMOJI, EMOJI]  # 2 units each, so one per piece


# ---- where the bot builds long messages ------------------------------------------------------------------

async def test_an_emoji_heavy_admin_alert_is_sent_in_parts_telegram_accepts():
    fake = FakeBot()
    text = EMOJI * 3000  # 3000 code points but 6000 UTF-16 units: old code sent it as one 3000-character part

    sent = await bot.alert_admin(fake, text)

    parts = [t for cid, t, _ in fake.sent if str(cid) == "111"]
    assert sent is True and len(parts) == 2
    assert all(utf16_len(p) <= bot.TELEGRAM_MAX_UNITS for p in parts)
    assert "".join(parts) == text


async def test_the_markup_goes_only_on_the_last_part():
    fake = FakeBot()

    await bot.alert_admin(fake, EMOJI * 3000, reply_markup="MARKUP")

    markups = [kw.get("reply_markup") for cid, _, kw in fake.sent if str(cid) == "111"]
    assert markups == [None, "MARKUP"]


async def test_the_customer_delivery_message_fits(storage):
    fake = FakeBot()
    order = {"id": "o1", "name": "Netflix", "user_id": CUSTOMER_ID}

    assert await bot.deliver_credentials(fake, order, [EMOJI * 3000]) is True

    (text,) = fake.texts_to(CUSTOMER_ID)
    assert utf16_len(text) <= bot.TELEGRAM_MAX_UNITS and text.startswith("🎉 Your order: Netflix")


async def test_my_orders_fits(storage, credential_box):
    for i in range(3):
        await storage.insert_order(
            new_order(
                f"o{i}", status="delivered", reference=f"{i:064x}",
                credentials_enc=credential_box.encrypt_list([EMOJI * 1500]),
            )
        )

    text = await bot.render_orders(CUSTOMER_ID)

    assert utf16_len(text) <= bot.TELEGRAM_MAX_UNITS


async def test_a_long_emoji_description_keeps_the_product_page_within_the_limit(monkeypatch):
    offer = bot.Offer("pandora", "p1", None, 3.0, 5)
    listing = bot.Listing("k1", "Netflix 1M", EMOJI * 4000, [offer])

    async def fake_catalog(force=False):
        return {"k1": listing}

    monkeypatch.setattr(bot, "get_catalog", fake_catalog)
    callback = make_callback("product:k1", user_id=CUSTOMER_ID)

    await bot.show_product(callback)

    (text,), _ = callback.message.answer.await_args
    assert utf16_len(text) <= bot.TELEGRAM_MAX_UNITS


# ---- the Bot session is closed on every way out of startup ----------------------------------------------------

class FakeStorageForStartup:
    def __init__(self, report, recover_error=None):
        self.report, self.recover_error = report, recover_error
        self.fsm_collection = None
        self.close = AsyncMock()
        self.connect = AsyncMock()

    async def ensure_indexes(self):
        return self.report

    async def all_langs(self):
        return {}

    async def recover_interrupted_orders(self):
        if self.recover_error:
            raise self.recover_error
        return []


class FakeAiogramBot:
    instances = []

    def __init__(self, token):
        self.session = SimpleNamespace(close=AsyncMock())
        self.set_my_commands = AsyncMock()
        FakeAiogramBot.instances.append(self)


@pytest.fixture
def startup(monkeypatch):
    """Everything main() needs, faked, so only its exit handling is under test."""
    FakeAiogramBot.instances = []
    monkeypatch.setattr(bot, "Bot", FakeAiogramBot)
    monkeypatch.setattr(bot, "configure_logging", lambda: None)
    monkeypatch.setattr(bot, "install_fsm_storage", lambda s: None)
    monkeypatch.setattr(bot.CredentialBox, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(bot, "flush_outbox", AsyncMock())

    def install(storage):
        monkeypatch.setattr(bot.Storage, "from_env", classmethod(lambda cls: storage))

    return install


def clean_report():
    return SimpleNamespace(has_problems=False)


def problem_report():
    return SimpleNamespace(
        has_problems=True, duplicate_references={}, missing_reference_lower=["o1"],
        stale_reference_lower=[], invalid_reference=[],
    )


async def test_session_is_closed_when_startup_stops_for_bad_order_data(startup):
    storage = FakeStorageForStartup(problem_report())
    startup(storage)

    with pytest.raises(SystemExit) as exit_info:
        await bot.main()

    assert "o1" in str(exit_info.value)
    (fake,) = FakeAiogramBot.instances
    fake.session.close.assert_awaited_once()
    storage.close.assert_awaited_once()


async def test_session_is_closed_when_recovery_fails(startup):
    storage = FakeStorageForStartup(clean_report(), recover_error=PyMongoError("down"))
    startup(storage)

    with pytest.raises(SystemExit):
        await bot.main()

    (fake,) = FakeAiogramBot.instances
    fake.session.close.assert_awaited_once()
    storage.close.assert_awaited_once()


async def test_session_is_closed_on_an_unexpected_error_too(startup):
    storage = FakeStorageForStartup(clean_report())
    startup(storage)
    original = FakeAiogramBot.__init__

    def failing_init(self, token):
        original(self, token)
        self.set_my_commands = AsyncMock(side_effect=RuntimeError("telegram down"))

    FakeAiogramBot.__init__ = failing_init
    try:
        with pytest.raises(RuntimeError):
            await bot.main()
    finally:
        FakeAiogramBot.__init__ = original

    (fake,) = FakeAiogramBot.instances
    fake.session.close.assert_awaited_once()
    storage.close.assert_awaited_once()


async def test_a_failing_session_close_still_closes_the_database(startup):
    storage = FakeStorageForStartup(problem_report())
    startup(storage)
    original = FakeAiogramBot.__init__

    def init_with_broken_close(self, token):
        original(self, token)
        self.session.close = AsyncMock(side_effect=RuntimeError("already closed"))

    FakeAiogramBot.__init__ = init_with_broken_close
    try:
        with pytest.raises((SystemExit, RuntimeError)):
            await bot.main()
    finally:
        FakeAiogramBot.__init__ = original

    storage.close.assert_awaited_once()


async def test_no_session_exists_to_close_if_startup_stops_before_the_bot_is_created(startup):
    storage = FakeStorageForStartup(clean_report())
    startup(storage)
    storage.connect.side_effect = PyMongoError("no server")

    with pytest.raises(SystemExit):
        await bot.main()

    assert FakeAiogramBot.instances == []  # nothing leaked, nothing to close
    storage.close.assert_awaited_once()

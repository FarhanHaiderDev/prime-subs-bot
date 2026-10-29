"""Item 4: structured logs with order ids, and a grep of real log output for secrets."""

import io
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import requests
from cryptography.fernet import Fernet

import bot
from conftest import CUSTOMER_ID, TXID, FakeBot, make_callback, new_order
from logsetup import (
    SECRET_PATTERNS,
    JsonFormatter,
    Redactor,
    collect_secret_values,
    configure_logging,
    current_order_id,
    order_context,
    set_order_id,
)

# Realistic-looking but fake secrets, used only in these tests.
FAKE_TOKEN = "123456789:" + "AbCdEfGhIj" * 3 + "AbCde"
FAKE_PANDORA_KEY = "sk_live_" + "Z9y8X7w6V5u4T3s2R1q0"
FAKE_CANBOSO_KEY = "canboso-secret-key-123456"
FAKE_ENCRYPTION_KEY = Fernet.generate_key().decode()
FAKE_DB_PASSWORD = "Sup3rS3cretPw"
FAKE_MONGO_URI = f"mongodb+srv://dbuser:{FAKE_DB_PASSWORD}@cluster0.example.mongodb.net/?retryWrites=true"
FAKE_ENV = {
    "BOT_TOKEN": FAKE_TOKEN,
    "SUPPLIER_API_KEY": FAKE_CANBOSO_KEY,
    "SUPPLIER_2_API_KEY": FAKE_PANDORA_KEY,
    "ENCRYPTION_KEY": FAKE_ENCRYPTION_KEY,
    "MONGODB_URI": FAKE_MONGO_URI,
}
ALL_SECRETS = [FAKE_TOKEN, FAKE_PANDORA_KEY, FAKE_CANBOSO_KEY, FAKE_ENCRYPTION_KEY, FAKE_DB_PASSWORD]
CREDENTIALS = "user:pass"  # what the supplier delivers; must never be logged


class LogCapture:
    def __init__(self):
        self.buffer = io.StringIO()
        self.handler = logging.StreamHandler(self.buffer)
        self.handler.setFormatter(JsonFormatter(Redactor(collect_secret_values(FAKE_ENV))))

    def raw(self):
        return self.buffer.getvalue()

    def lines(self):
        return [json.loads(line) for line in self.raw().splitlines() if line.strip()]

    def clear(self):
        self.buffer.seek(0)
        self.buffer.truncate(0)


@pytest.fixture
def logs():
    capture = LogCapture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture.handler)
    root.setLevel(logging.INFO)
    yield capture
    root.removeHandler(capture.handler)
    root.setLevel(old_level)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)


def assert_no_secrets(raw: str):
    for secret in ALL_SECRETS:
        assert secret not in raw, f"a secret value leaked into the logs: {secret[:6]}..."
    for pattern, _replacement in SECRET_PATTERNS:
        assert not pattern.search(raw), f"secret pattern {pattern.pattern!r} matched the log output"
    assert CREDENTIALS not in raw, "purchased credentials were logged"
    assert TXID not in raw, "a payment proof was logged"


# ---- format ---------------------------------------------------------------------------------

def test_every_line_is_json_with_the_order_id_from_the_context(logs):
    log = logging.getLogger("t")
    log.error("outside any order")
    with order_context("ord-42"):
        log.error("inside an order")
        log.warning("helper that never mentions the id")
    log.error("outside again")

    lines = logs.lines()
    assert [l["order_id"] for l in lines] == [None, "ord-42", "ord-42", None]
    assert all({"ts", "level", "logger", "order_id", "msg"} <= set(l) for l in lines)
    assert lines[1]["level"] == "ERROR" and lines[1]["msg"] == "inside an order"


def test_set_order_id_tags_the_current_task_and_none_clears_it(logs):
    set_order_id("abc")
    assert current_order_id() == "abc"
    logging.getLogger("t").error("tagged")
    set_order_id(None)
    logging.getLogger("t").error("untagged")

    assert [l["order_id"] for l in logs.lines()] == ["abc", None]


def test_tracebacks_are_included_in_the_json_line(logs):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("t").exception("it failed")

    (line,) = logs.lines()
    assert "Traceback" in line["traceback"] and "RuntimeError: boom" in line["traceback"]


def test_configure_logging_installs_one_json_handler():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    stream = io.StringIO()
    try:
        configure_logging(stream=stream, environ=FAKE_ENV)
        logging.getLogger("t").error("hello %s", FAKE_TOKEN)
        assert len(root.handlers) == 1
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    (line,) = [json.loads(l) for l in stream.getvalue().splitlines()]
    assert FAKE_TOKEN not in stream.getvalue() and "[REDACTED]" in line["msg"]


# ---- redaction -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        f"connecting to https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
        f"Authorization: Bearer {FAKE_PANDORA_KEY}",
        f"headers={{'X-API-Key': '{FAKE_CANBOSO_KEY}'}}",
        f"could not connect to {FAKE_MONGO_URI}",
        f"bad key {FAKE_ENCRYPTION_KEY}",
        f"password was {FAKE_DB_PASSWORD}",
    ],
)
def test_exact_values_and_patterns_are_redacted(text):
    redacted = Redactor(collect_secret_values(FAKE_ENV)).redact(text)

    for secret in ALL_SECRETS:
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_patterns_catch_secrets_that_are_not_in_the_environment():
    redactor = Redactor([])  # nothing configured
    for text in (
        "token 987654321:" + "Q" * 35,
        "key sk_live_abcdef123456",  # secret-scan: allow (deliberate fake)
        "Bearer abcdefgh12345678",
        "mongodb://user:hunter2@host/db",  # secret-scan: allow (deliberate fake)
        "fernet " + Fernet.generate_key().decode(),
    ):
        assert redactor.redact(text) != text


def test_ordinary_text_is_left_alone():
    text = "Order ab12cd34ef queued; supplier pandora; 3 attempts; user 777"
    assert Redactor(collect_secret_values(FAKE_ENV)).redact(text) == text


def test_short_env_values_are_not_treated_as_secrets():
    assert collect_secret_values({"BOT_TOKEN": "abc"}) == []


def test_mongo_credentials_are_collected_from_the_uri():
    values = collect_secret_values({"MONGODB_URI": FAKE_MONGO_URI})
    assert FAKE_DB_PASSWORD in values and FAKE_MONGO_URI in values


# ---- grep of real flows ------------------------------------------------------------------------

async def test_logs_from_real_flows_contain_no_secrets_and_every_line_has_the_order_id(
    storage, pandora, logs, monkeypatch
):
    """Run failure and success paths whose exceptions carry secrets, then grep the output."""
    # 1) a network failure during the purchase -> ambiguous -> reconciliation; the Telegram
    #    edit also fails with an error message that contains the bot token (as aiohttp's would).
    pandora.raise_on_order = requests.ConnectionError(f"Authorization: Bearer {FAKE_PANDORA_KEY}")
    await storage.insert_order(new_order("ord1", reference="01" * 32))
    callback = make_callback("approve:ord1")
    callback.message.edit_text.side_effect = RuntimeError(
        f"Cannot connect to https://api.telegram.org/bot{FAKE_TOKEN}/editMessageText"
    )
    logs.clear()
    await bot.approve_order(callback)
    flow = logs.lines()
    assert flow, "the failure path should log"
    assert {l["order_id"] for l in flow} == {"ord1"}, "every line of an order flow carries its order id"
    assert any(FAKE_TOKEN not in l.get("traceback", "") and "[REDACTED" in l.get("traceback", "") for l in flow)

    # 2) a successful purchase and delivery (credentials flow through, must not be logged)
    pandora.raise_on_order = None
    await storage.insert_order(new_order("ord2", reference="02" * 32))
    await bot.approve_order(make_callback("approve:ord2"))

    # 3) delivery fails, then the background redelivery runs (credentials are decrypted there)
    await storage.insert_order(new_order("ord3", reference="03" * 32))
    await bot.approve_order(make_callback("approve:ord3", fake_bot=FakeBot(fail_for=[CUSTOMER_ID])))
    from datetime import datetime, timedelta, timezone

    await storage._orders.update_one(
        {"_id": "ord3"}, {"$set": {"purchased_at": datetime.now(timezone.utc) - timedelta(minutes=10)}}
    )
    await bot.redeliver_undelivered(FakeBot(fail_for=[CUSTOMER_ID]))

    # 4) the global error handler with an exception that carries secrets
    update = SimpleNamespace(update_id=9, message=SimpleNamespace(answer=AsyncMock()), callback_query=None)
    try:
        raise RuntimeError(f"db down: {FAKE_MONGO_URI} token {FAKE_TOKEN} key {FAKE_ENCRYPTION_KEY}")
    except RuntimeError as exc:
        await bot.on_error(SimpleNamespace(update=update, exception=exc), FakeBot())

    # 5) a rejected proof submission and a language-save failure
    await bot.receive_reference(
        SimpleNamespace(
            text=TXID,
            from_user=SimpleNamespace(id=900, full_name="C", username="c"),
            answer=AsyncMock(),
            bot=FakeBot(),
        ),
        SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "method": "trc20",
                    "name": "Netflix 1M",
                    "price": 4.2,
                    "offers": [{"supplier": "pandora", "product_id": "p1", "variant_id": None, "cost": 3.0}],
                }
            ),
            clear=AsyncMock(),
        ),
    )
    monkeypatch.setattr(storage, "set_lang", AsyncMock(side_effect=RuntimeError(f"cannot reach {FAKE_MONGO_URI}")))
    await bot.set_language(make_callback("lang:es"))

    raw = logs.raw()
    assert raw, "the flows should have produced log output"
    for line in raw.splitlines():
        json.loads(line)  # every line is valid JSON
    assert_no_secrets(raw)

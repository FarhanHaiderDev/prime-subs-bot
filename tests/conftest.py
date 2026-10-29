"""Offline test fixtures: no real network, no real MongoDB, no real .env.

* The project's .env is never read (load_dotenv is disabled before `bot` is imported).
* MongoDB is an in-memory mongomock-motor client injected into Storage.
* Pandora's HTTP API is replaced by FakePandora.
* Telegram objects are lightweight fakes.
"""

import os
import sys
import uuid
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Never load the real .env.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

os.environ.update(
    {
        "BOT_TOKEN": "test-token",
        "ADMIN_CHAT_ID": "111",
        "SUPPLIER_2_API_KEY": "test-pandora-key",
        "MARKUP_PERCENT": "40",
        "MARKUP_FIXED": "0",
        "TRC20_ADDRESS": "TTestTronAddress",
        "BEP20_ADDRESS": "0xTestBscAddress",
        "BINANCE_PAY_ID": "TestBinanceId",
    }
)
for _name in ("SUPPLIER_API_KEY", "CANBOSO_ENABLED", "MONGODB_URI", "ENCRYPTION_KEY"):
    os.environ.pop(_name, None)

import bot  # noqa: E402
from credbox import CredentialBox  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402
from storage import Storage  # noqa: E402

ADMIN_ID = 111
CUSTOMER_ID = 777
TXID = "ab" * 32  # a well-formed 64-hex proof


@pytest.fixture(autouse=True)
def credential_box(monkeypatch):
    box = CredentialBox(Fernet.generate_key().decode())
    monkeypatch.setattr(bot, "credbox", box)
    return box


@pytest.fixture
async def storage(monkeypatch):
    s = Storage("mongodb://unused.invalid", "test_db")
    s._client = AsyncMongoMockClient()
    await s.ensure_indexes()
    monkeypatch.setattr(bot, "storage", s)
    return s


def _real_mongo_uri():
    """MONGODB_TEST_URI points at a THROWAWAY MongoDB used only by the tests. Each test
    gets its own randomly named database that is dropped afterwards. Atlas hosts are
    refused unless MONGODB_TEST_ALLOW_REMOTE=1, so production is never touched by accident."""
    uri = os.environ.get("MONGODB_TEST_URI")
    if not uri:
        if os.environ.get("REQUIRE_REAL_MONGO") == "1":  # set in CI: a missing server must fail, not skip
            pytest.fail("REQUIRE_REAL_MONGO=1 but MONGODB_TEST_URI is not set")
        pytest.skip("MONGODB_TEST_URI is not set; real-MongoDB tests are skipped")
    if "mongodb.net" in uri and os.environ.get("MONGODB_TEST_ALLOW_REMOTE") != "1":
        pytest.skip("MONGODB_TEST_URI looks like Atlas; set MONGODB_TEST_ALLOW_REMOTE=1 to allow")
    return uri


@pytest.fixture(params=["mongomock", "real"])
async def any_storage(request, monkeypatch):
    """The same test on the in-memory database and (when configured) a real MongoDB."""
    if request.param == "mongomock":
        s = Storage("mongodb://unused.invalid", "test_db")
        s._client = AsyncMongoMockClient()
        await s.ensure_indexes()
        monkeypatch.setattr(bot, "storage", s)
        yield s
        return

    uri = _real_mongo_uri()
    db_name = f"psb_test_{uuid.uuid4().hex[:12]}"
    s = Storage(uri, db_name)
    await s.connect()
    await s.ensure_indexes()
    monkeypatch.setattr(bot, "storage", s)
    try:
        yield s
    finally:
        await s._client.drop_database(db_name)
        await s.close()


class FakeBot:
    """Records every Telegram message the code sends.

    fail_for: chat ids whose sends raise (e.g. a customer who blocked the bot).
    on_send: optional async hook called with (chat_id, text) before each send.
    """

    def __init__(self, fail_for=(), on_send=None):
        self.sent = []
        self.attempts = {}
        self.fail_for = {str(c) for c in fail_for}
        self.on_send = on_send

    async def send_message(self, chat_id, text, **kwargs):
        self.attempts[str(chat_id)] = self.attempts.get(str(chat_id), 0) + 1
        if self.on_send:
            await self.on_send(chat_id, text)
        if str(chat_id) in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text, kwargs))

    def texts_to(self, chat_id):
        return [text for cid, text, _ in self.sent if str(cid) == str(chat_id)]


def make_callback(data, user_id=ADMIN_ID, message_text="alert text", fake_bot=None):
    message = SimpleNamespace(
        text=message_text, edit_text=AsyncMock(), answer=AsyncMock(), delete=AsyncMock()
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Admin", username="admin"),
        message=message,
        bot=fake_bot or FakeBot(),
        answer=AsyncMock(),
    )


def new_order(order_id="ord1", offers=None, price=4.2, status="pending", **overrides):
    """A freshly submitted order document (price 4.2 = cost 3.0 * 1.40)."""
    offers = offers or [asdict(bot.Offer("pandora", "p1", None, 3.0))]
    order = {
        "id": order_id,
        "status": status,
        "user_id": CUSTOMER_ID,
        "user_label": "Customer (@cust, id 777)",
        "name": "Netflix 1M",
        "price": price,
        "method": "trc20",
        "reference": TXID,
        "offers": offers,
        "admin_notified": True,
    }
    order.update(overrides)
    return order


class FakePandora:
    """Stands in for requests.request(...) against Pandora; records every call."""

    def __init__(self):
        self.calls = []
        self.quote = {"can_purchase": True, "unit_price": "3.00", "price_version": "v1"}
        self.order_status = 200
        self.order = {"id": "po_1", "status": "delivered", "delivery": {"items": ["user:pass"]}}
        self.raise_on_order = None  # an exception to raise on POST /orders (e.g. a network error)

    def _response(self, status, body):
        return SimpleNamespace(
            ok=200 <= status < 300, status_code=status, headers={}, json=lambda: body
        )

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/quotes"):
            return self._response(200, self.quote)
        if url.endswith("/orders") and method == "POST":
            if self.raise_on_order:
                raise self.raise_on_order
            return self._response(self.order_status, self.order)
        if "/orders/" in url and method == "GET":
            return self._response(200, self.order)
        raise AssertionError(f"unexpected supplier request: {method} {url}")

    @property
    def urls(self):
        return [url for _, url, _ in self.calls]

    def purchase_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/orders")]


@pytest.fixture
def pandora(monkeypatch):
    fake = FakePandora()
    monkeypatch.setattr(bot.requests, "request", fake.request)
    return fake

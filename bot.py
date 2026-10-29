import asyncio
import hashlib
import logging
import math
import os
import re
import time
import uuid
from email.utils import parsedate_to_datetime
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError, PyMongoError

from credbox import CredentialBox, CredentialDecryptError, CredentialKeyError
from fsmstorage import MongoFSMStorage
from logsetup import configure_logging, order_context, set_order_id
from ratelimit import RateLimitMiddleware, SlidingWindowLimiter, wait_text
from textutil import split_utf16, truncate_utf16
from storage import (
    ORDER_RESERVATION_TTL_SECONDS,
    RESERVATION_TTL_SECONDS,
    RESERVE_MAX_TRACKED_STOCK,
    Storage,
    outbox_fields,
    reservation_key,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Supplier 1 (Canboso): X-API-Key header.
SUPPLIER_API_KEY = os.getenv("SUPPLIER_API_KEY")
CANBOSO_PRODUCTS_URL = "https://canboso.com/api/v2/telegram-buyer/products"
# Canboso is OFF until its purchase flow is real. While off it is neither listed nor
# priced nor used for fulfilment, so a cheaper Canboso offer can never set a price that
# Pandora (the only implemented source) cannot honour.
CANBOSO_ENABLED = (os.getenv("CANBOSO_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
# Flip to True in the same change that implements purchase_canboso(); startup refuses
# CANBOSO_ENABLED=true while this is False.
CANBOSO_PURCHASE_IMPLEMENTED = False

# Supplier 2 (Pandora Digital): Bearer token.
SUPPLIER_2_API_KEY = os.getenv("SUPPLIER_2_API_KEY")
PANDORA_BASE_URL = "https://api.pandoradigital.shop/api/v1"

# Retail price = lowest supplier cost * (1 + MARKUP_PERCENT / 100) + MARKUP_FIXED
MARKUP_PERCENT = float(os.getenv("MARKUP_PERCENT", "40"))
MARKUP_FIXED = float(os.getenv("MARKUP_FIXED", "0"))

# Payment methods. A method is only offered when its destination is set in .env.
PAYMENT_METHODS = {
    "binance": {
        "label": "Binance Pay (ID/Email)",
        "coin": "USDT",
        "network": "Binance Pay (internal transfer, no blockchain network)",
        "destination": os.getenv("BINANCE_PAY_ID"),
        "dest_label": "Binance Pay ID / Email",
        "ref_label": "Binance Order ID",
    },
    "bep20": {
        "label": "BEP20 (USDT)",
        "coin": "USDT",
        "network": "BEP20 (BNB Smart Chain)",
        "destination": os.getenv("BEP20_ADDRESS"),
        "dest_label": "BEP20 (BNB Smart Chain) USDT address",
        "ref_label": "transaction hash (TXID)",
    },
    "trc20": {
        "label": "TRC20 (USDT)",
        "coin": "USDT",
        "network": "TRC20 (TRON)",
        "destination": os.getenv("TRC20_ADDRESS"),
        "dest_label": "TRC20 (TRON) USDT address",
        "ref_label": "transaction hash (TXID)",
    },
}

TXID_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
BINANCE_ORDER_RE = re.compile(r"^[0-9A-Za-z]{6,64}$")

PAGE_SIZE = 8
REQUEST_TIMEOUT = 15
ORDER_TIMEOUT = 30
FENCE_TIMEOUT = 20
CATALOG_TTL = 30

dp = Dispatcher()

# Spam protection, per user (the admin is exempt). Messages and button presses are throttled
# by outer middleware; payment-proof attempts have their own, stricter limit (see
# receive_reference).
message_limiter = RateLimitMiddleware(SlidingWindowLimiter(8, 10), is_exempt=lambda uid: is_admin(uid))
button_limiter = RateLimitMiddleware(
    SlidingWindowLimiter(15, 10), is_exempt=lambda uid: is_admin(uid), notice_each_event=True
)
proof_limiter = SlidingWindowLimiter(5, 600)  # 5 payment-proof attempts per 10 minutes
dp.message.outer_middleware(message_limiter)
dp.callback_query.outer_middleware(button_limiter)


class Checkout(StatesGroup):
    waiting_reference = State()


class UnlockFlow(StatesGroup):
    waiting_note = State()
    confirming = State()


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

class SupplierError(Exception):
    """Proven failure: this supplier did NOT purchase anything, so trying the
    next one (or a normal Retry) is safe. Anything not provable is an
    AmbiguousOrderError instead."""


class PriceChanged(SupplierError):
    """The supplier's live cost is above what the order was priced on. Nothing was bought
    (this is raised before any purchase request), and it must NOT fail the order: the admin
    decides whether to buy at the new cost."""

    def __init__(self, supplier: str, new_cost: float):
        super().__init__(f"{supplier} cost is now {new_cost:.2f}, above the order's cost ceiling")
        self.supplier = supplier
        self.new_cost = new_cost


class AmbiguousOrderError(Exception):
    """An order may or may not have been placed; do NOT try another supplier."""


class ClaimLostError(AmbiguousOrderError):
    """This instance no longer owns the order (it was recovered, reconciled or
    re-claimed elsewhere). Stop before sending any further supplier request."""


class FenceUnavailable(Exception):
    """The ownership check itself couldn't be completed (database error/timeout)."""


@dataclass
class Offer:
    supplier: str  # "canboso" | "pandora"
    product_id: str
    variant_id: str | None
    cost: float
    stock: int | None = None  # None = unknown


@dataclass
class Listing:
    key: str
    name: str
    description: str
    offers: list[Offer]  # cheapest first

    @property
    def cost(self) -> float:
        return self.offers[0].cost


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def parse_price(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value"):
            if isinstance(value.get(key), (int, float)):
                return float(value[key])
        value = value.get("text")
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return float(match.group().replace(",", ".")) if match else None


def fetch_canboso() -> list[dict]:
    response = requests.get(
        CANBOSO_PRODUCTS_URL,
        headers={"X-API-Key": SUPPLIER_API_KEY},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError(f"Canboso reported failure: {data}")

    items = []
    for p in data.get("products", []):
        availability = p.get("availability") or {}
        try:
            stock = int(availability.get("available") or 0)
        except (TypeError, ValueError):
            stock = 0
        cost = parse_price(p.get("price"))
        if stock <= 0 or cost is None or not p.get("productId"):
            continue
        items.append({
            "name": str(p.get("name") or f"Product {p['productId']}"),
            "description": str(p.get("description") or ""),
            "offer": Offer("canboso", str(p["productId"]), None, cost),
        })
    return items


def pandora_headers(**extra) -> dict:
    return {"Authorization": f"Bearer {SUPPLIER_2_API_KEY}", **extra}


def pandora_error_code(response: requests.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return f"HTTP_{response.status_code}"


def fetch_pandora() -> list[dict]:
    items, cursor = [], None
    for _ in range(20):  # hard page cap
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(
            f"{PANDORA_BASE_URL}/products",
            headers=pandora_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        for p in data.get("items", []):
            stock = p.get("available_stock")  # None = unknown; the quote decides
            cost = parse_price(p.get("unit_price"))
            if stock == 0 or cost is None:
                continue
            name = p["name"] + (f" - {p['variant_name']}" if p.get("variant_name") else "")
            items.append({
                "name": name,
                "description": str(p.get("description") or ""),
                "offer": Offer("pandora", p["id"], p.get("variant_id"), cost, stock),
            })
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return items


_catalog_cache: tuple[float, dict[str, Listing]] | None = None


def supplier_is_live(name: str) -> bool:
    """A supplier is used for listing, pricing and fulfilment only if it is configured
    and its purchase flow is actually implemented and switched on."""
    if name == "pandora":
        return bool(SUPPLIER_2_API_KEY)
    if name == "canboso":
        return CANBOSO_ENABLED and CANBOSO_PURCHASE_IMPLEMENTED and bool(SUPPLIER_API_KEY)
    return False


def build_catalog() -> dict[str, Listing]:
    """Query the live suppliers, merge matching products, cheapest offer first."""
    raw: list[dict] = []
    for name, fetch in (
        ("canboso", fetch_canboso),
        ("pandora", fetch_pandora),
    ):
        if not supplier_is_live(name):
            continue
        try:
            raw.extend(fetch())
        except (requests.RequestException, ValueError, KeyError):
            logging.exception("Failed to fetch products from %s", name)

    catalog: dict[str, Listing] = {}
    for item in raw:
        norm = normalize(item["name"])
        if not norm:
            continue
        key = hashlib.sha1(norm.encode()).hexdigest()[:12]
        listing = catalog.get(key)
        if listing is None:
            catalog[key] = Listing(key, item["name"], item["description"], [item["offer"]])
        else:
            listing.offers.append(item["offer"])
            listing.description = listing.description or item["description"]
    for listing in catalog.values():
        listing.offers.sort(key=lambda o: o.cost)
    return catalog


async def get_catalog(force: bool = False) -> dict[str, Listing]:
    global _catalog_cache
    if not force and _catalog_cache and time.monotonic() - _catalog_cache[0] < CATALOG_TTL:
        return _catalog_cache[1]
    catalog = await asyncio.to_thread(build_catalog)
    if catalog:
        _catalog_cache = (time.monotonic(), catalog)
    return catalog


def retail_price(cost: float) -> float:
    return round(cost * (1 + MARKUP_PERCENT / 100) + MARKUP_FIXED, 2)


# --------------------------------------------------------------------------
# Fulfilment
# --------------------------------------------------------------------------

RETRY_AFTER_DEFAULT_SECONDS = 5.0
RETRY_AFTER_MAX_SECONDS = 30.0
_DELTA_SECONDS = re.compile(r"\d+(?:\.\d+)?")


def parse_retry_after(value, now: datetime | None = None) -> float:
    """How many seconds to wait, from a Retry-After header: either a number of seconds or an
    HTTP date (RFC 9110). A missing or unreadable value gives the default; the wait is never
    negative and never longer than the cap, so a bad header can neither crash a purchase nor
    stall it."""
    if value is None:
        return RETRY_AFTER_DEFAULT_SECONDS
    text = str(value).strip()
    if _DELTA_SECONDS.fullmatch(text):
        seconds = float(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return RETRY_AFTER_DEFAULT_SECONDS
        if when is None:
            return RETRY_AFTER_DEFAULT_SECONDS
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_MAX_SECONDS)


def purchase_canboso(offer: Offer, order_id: str, max_cost: float, fence) -> tuple[list[str], float]:
    # Canboso's order-creation API isn't documented yet. Until it is implemented (and
    # CANBOSO_PURCHASE_IMPLEMENTED is set), Canboso is not live, so this is never reached.
    raise SupplierError("Canboso automatic purchase is not implemented")


def purchase_pandora(offer: Offer, order_id: str, max_cost: float, fence) -> tuple[list[str], float]:
    """Returns (credentials, the unit price the supplier actually charged)."""
    if not SUPPLIER_2_API_KEY:
        raise SupplierError("SUPPLIER_2_API_KEY is not set")

    def call(method: str, path: str, **kwargs) -> requests.Response:
        return requests.request(
            method, f"{PANDORA_BASE_URL}{path}", headers=pandora_headers(**kwargs.pop("headers", {})),
            timeout=ORDER_TIMEOUT, **kwargs,
        )

    body = {"product_id": offer.product_id, "quantity": 1}
    if offer.variant_id:
        body["variant_id"] = offer.variant_id

    # Quote immediately before ordering (price protection). No /orders request has
    # been sent yet, so any failure here provably purchased nothing.
    try:
        response = call("POST", "/quotes", json=body)
    except requests.RequestException as exc:
        raise SupplierError(f"Pandora quote unreachable ({type(exc).__name__})")
    if not response.ok:
        raise SupplierError(f"Pandora quote failed: {pandora_error_code(response)}")
    quote = response.json()
    if not quote.get("can_purchase"):
        raise SupplierError("Pandora quote: cannot purchase (out of stock or unavailable)")
    unit_price = quote["unit_price"]
    if float(unit_price) > max_cost:
        raise PriceChanged("pandora", float(unit_price))

    order_body = {
        **body,
        "expected_unit_price": unit_price,
        "price_version": quote["price_version"],
        "client_order_reference": order_id,
    }
    # One stable idempotency key per logical purchase; safe to retry on timeouts.
    headers = {"Idempotency-Key": f"bot-{order_id}"}
    order = None
    maybe_placed = False  # True once any /orders request may have reached the supplier
    for attempt in range(3):
        # Fence: never send a purchase request unless this instance still owns the claim.
        try:
            fence()
        except FenceUnavailable:
            if maybe_placed:
                raise AmbiguousOrderError("Could not verify order ownership after an earlier uncertain request")
            raise SupplierError("Could not verify order ownership; no purchase request was sent")
        try:
            response = call("POST", "/orders", json=order_body, headers=headers)
        except (requests.Timeout, requests.ConnectionError):
            maybe_placed = True
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code == 429:
            maybe_placed = True  # conservative: not treated as a proven rejection
            time.sleep(parse_retry_after(response.headers.get("Retry-After")))
            continue
        if response.status_code >= 500:
            maybe_placed = True
            time.sleep(2 * (attempt + 1))
            continue
        if not response.ok:
            code = pandora_error_code(response)
            # Only a clean 4xx on a first, uncertain-free attempt is a proven rejection.
            # A key conflict means an earlier order exists, so it is never "no purchase".
            if maybe_placed or response.status_code == 408 or code == "IDEMPOTENCY_CONFLICT":
                raise AmbiguousOrderError(f"Pandora order response is not a proven rejection: {code}")
            raise SupplierError(f"Pandora order rejected: {code}")
        order = response.json()
        break
    if order is None:
        raise AmbiguousOrderError("Pandora order request outcome unknown after retries")

    # Poll while processing.
    deadline = time.monotonic() + 90
    while order.get("status") in ("pending", "processing"):
        if time.monotonic() > deadline:
            raise AmbiguousOrderError(f"Pandora order {order.get('id')} still processing")
        time.sleep(3)
        try:
            response = call("GET", f"/orders/{order['id']}")
            if response.ok:
                order = response.json()
        except requests.RequestException:
            continue

    if order.get("status") == "delivered":
        items = (order.get("delivery") or {}).get("items") or []
        if not items:
            raise AmbiguousOrderError(f"Pandora order {order.get('id')} delivered with no items")
        return [str(i) for i in items], float(unit_price)
    if order.get("status") == "failed":
        # The supplier itself reports a terminal failure for this order: nothing delivered.
        raise SupplierError(f"Pandora order failed: {order.get('failure_code')}")
    raise AmbiguousOrderError(f"Pandora order {order.get('id')} in state {order.get('status')}")


PURCHASERS = {"canboso": purchase_canboso, "pandora": purchase_pandora}


def fulfil(order: dict, fence) -> tuple[list[str], Offer, list[str]]:
    """Try offers cheapest-first. Returns (credentials, winning offer, failure log)."""
    failures = []
    # Never buy above the cost the customer's price was derived from (+1 cent rounding).
    max_cost = (order["price"] - MARKUP_FIXED) / (1 + MARKUP_PERCENT / 100) + 0.01
    if order.get("approved_cost") is not None:  # the admin accepted buying at a higher cost
        max_cost = max(max_cost, float(order["approved_cost"]) + 0.01)
    # A new attempt (after a definitive failure) needs a fresh idempotency key.
    purchase_ref = purchase_ref_for(order)
    # Only sources that are actually live are considered (an order stored with an offer
    # from a source that is no longer live must not try it).
    live_offers = [Offer(**raw) for raw in order["offers"] if supplier_is_live(raw["supplier"])]
    if not live_offers:
        raise SupplierError("no live supplier is available for this order")
    price_change = None
    for offer in live_offers:
        try:
            credentials, charged = PURCHASERS[offer.supplier](offer, purchase_ref, max_cost, fence)
            return credentials, replace(offer, cost=charged), failures  # cost = what was really paid
        except PriceChanged as exc:
            price_change = price_change or exc  # try the other sources first
            failures.append(f"{offer.supplier}: {exc}")
        except SupplierError as exc:
            failures.append(f"{offer.supplier}: {exc}")
            logging.warning("Order %s: %s failed: %s", order["id"], offer.supplier, exc)
    if price_change is not None:
        raise price_change  # no source could be used at the old price: the admin decides
    raise SupplierError("; ".join(failures) or "no offers")


# --------------------------------------------------------------------------
# Order persistence (MongoDB via storage.py)
# --------------------------------------------------------------------------

RETRY_LATER = (
    "Sorry, something went wrong on our side and your order was not saved. "
    "Please send your reference again in a moment."
)
DUPLICATE_REFERENCE = (
    "This payment proof (transaction hash / Binance order ID) has already been used for another order. "
    "Each proof can back only one order. If you believe this is a mistake, please contact support."
)
PURCHASED_UNRESOLVED = (
    "The supplier purchase completed but its credentials could not be stored. "
    "Reconcile manually; never retry or reject."
)


def log_db_error(action: str, exc: Exception, **ids) -> None:
    """Log a database failure without echoing the driver message, which can
    contain payment references or connection details."""
    logging.error("MongoDB error while %s %s: %s", action, ids or "", type(exc).__name__)


def purchase_ref_for(order: dict) -> str:
    """The supplier client reference / idempotency basis for this order's current attempt."""
    return f"{order['id']}-{order.get('attempt', 0)}"


SAVED, STALE, FAILED = "saved", "stale", "failed"


async def _already_applied(order_id: str, fields: dict) -> bool:
    """True if the stored order already holds every value in `fields` (a previous
    attempt may have applied before its reply was lost)."""
    try:
        doc = await storage.get_order(order_id)
    except PyMongoError as exc:
        log_db_error("verifying order write", exc, order=order_id)
        return False
    return doc is not None and all(doc.get(key) == value for key, value in fields.items())


async def persist_order(
    order_id: str, fields: dict, expected: str | list[str], claim_token: str, attempts: int = 3
) -> str:
    """Write order fields only while the order still has an `expected` status AND still
    carries this attempt's `claim_token`.

    Returns SAVED, STALE (the order is no longer in an expected status or belongs to a
    newer attempt, so nothing was written), or FAILED (the database errored on every
    attempt). Only SAVED means the write took effect. The token is mandatory: a late
    handler from an older attempt must never write onto a newer attempt's order.
    """
    had_error = False
    for attempt in range(attempts):
        try:
            matched = await storage.update_order(
                order_id, fields, only_if_status=expected, claim_token=claim_token
            )
        except PyMongoError as exc:
            had_error = True
            log_db_error("saving order", exc, order=order_id, attempt=attempt + 1)
            if attempt < attempts - 1:
                await asyncio.sleep(1 + attempt)
            continue
        if matched:
            return SAVED
        if had_error and await _already_applied(order_id, fields):
            return SAVED
        logging.error(
            "Order %s not updated: no longer in expected status %s or claimed by a newer attempt (fields %s)",
            order_id, expected, sorted(fields),
        )
        return STALE
    return FAILED


def unsaved_note(result: str) -> str:
    if result == STALE:
        return (
            "Its state was changed by something else while this was being recorded, so nothing was "
            "overwritten and no buttons are offered. Check the order before acting."
        )
    return (
        "The database write failed, so the order is still 'fulfilling' and has no buttons. "
        "After the next restart it will appear as needing manual reconciliation."
    )


TELEGRAM_MAX_UNITS = 4096  # Telegram's message limit, counted in UTF-16 code units
TELEGRAM_CHUNK = 4000  # alert parts stay a little below it


async def _send_admin_part(bot: Bot, text: str, reply_markup, protect_content: bool, attempts: int, order_id) -> bool:
    last_error = "unknown"
    for attempt in range(attempts):
        try:
            await bot.send_message(
                ADMIN_CHAT_ID, text, reply_markup=reply_markup, protect_content=protect_content
            )
            return True
        except TelegramRetryAfter as exc:
            last_error, delay = type(exc).__name__, min(exc.retry_after, 30)
        except TelegramEntityTooLarge as exc:  # permanent, not a transient network problem
            logging.error("Admin alert not sent for order %s (%s, not retried)", order_id or "-", type(exc).__name__)
            return False
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as exc:
            last_error, delay = type(exc).__name__, 1 + attempt
        except Exception as exc:  # e.g. forbidden / bad request: retrying won't help
            logging.error("Admin alert not sent for order %s (%s, not retried)", order_id or "-", type(exc).__name__)
            return False
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    logging.error(
        "Admin alert not delivered for order %s after %s attempts (%s)", order_id or "-", attempts, last_error
    )
    return False


async def alert_admin(
    bot: Bot,
    text: str,
    reply_markup=None,
    protect_content: bool = False,
    attempts: int = 3,
    order_id: str | None = None,
) -> bool:
    """Send an alert to the admin chat. Returns True only if every part was delivered.

    Transient Telegram failures are retried a bounded number of times. Failures are
    logged with the order id and error type only, never the alert text (it can hold
    payment references or credentials). Callers must check the result when the alert
    is the only record of something critical.
    """
    if not ADMIN_CHAT_ID:
        logging.error("ADMIN_CHAT_ID not set; admin alert not sent (order %s)", order_id or "-")
        return False
    chunks = split_utf16(text, TELEGRAM_CHUNK) or [text]
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        if not await _send_admin_part(bot, chunk, markup, protect_content, attempts, order_id):
            return False
    return True


async def flag_unalerted_purchase(callback: CallbackQuery, order: dict) -> None:
    """A purchased order's credentials alert could not be delivered: say so loudly.

    The order stays blocked from Retry/Reject and is never shown as resolved. No
    credentials are logged or written anywhere new.
    """
    purchase_ref = f"{order['id']}-{order.get('attempt', 0)}"
    logging.error(
        "Order %s: PURCHASED but the admin alert was NOT delivered. It stays blocked from "
        "Retry/Reject. Recover the credentials from the supplier (client reference %s).",
        order["id"], purchase_ref,
    )
    try:
        await callback.message.edit_text(
            f"{callback.message.text}\n\n⚠️ PURCHASED, but the credentials alert could not be delivered. "
            "The order stays blocked from Retry/Reject. Recover it from the supplier "
            f"(client reference {purchase_ref})."
        )
    except Exception as exc:
        logging.error("Could not mark the admin message for order %s (%s)", order["id"], type(exc).__name__)


ADMIN_ID_RE = re.compile(r"[1-9][0-9]{0,15}")


def validate_admin_chat_id(value) -> str:
    """ADMIN_CHAT_ID must be the admin's numeric Telegram USER id: a positive whole number.

    Admin buttons compare the clicking user's id with it, so a group/channel id (negative),
    a @username, a zero or a malformed value would silently lock the admin out. Raises
    ValueError with a message that says what to fix, without echoing the value."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(
            "ADMIN_CHAT_ID is not set. Set it to your numeric Telegram user id (ask @userinfobot)."
        )
    if ADMIN_ID_RE.fullmatch(text):
        return text
    if text.startswith("-"):
        hint = "that looks like a group or channel id; only a personal user id (a positive number) can approve orders"
    elif text.startswith("@") or not any(ch.isdigit() for ch in text):
        hint = "usernames are not accepted; use the numeric user id"
    elif text.lstrip("+").startswith("0") or text.startswith("+"):
        hint = "write the number without a sign or leading zeros"
    else:
        hint = "it must contain digits only"
    raise ValueError(f"ADMIN_CHAT_ID is invalid: {hint}.")


def apply_admin_chat_id() -> None:
    """Validate the configured admin id once at startup and store its normalised form."""
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = validate_admin_chat_id(ADMIN_CHAT_ID)


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_CHAT_ID) and str(user_id) == str(ADMIN_CHAT_ID)


def admin_keyboard(order_id: str, retry: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔁 Retry" if retry else "✅ Approve", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{order_id}"),
    )
    return builder.as_markup()


def reconcile_keyboard(order_id: str):
    """Only offered for an order whose outcome is unknown, never for a purchased one."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔓 Unlock (needs Pandora confirmation)", callback_data=f"unlock:{order_id}"))
    return builder.as_markup()


# --------------------------------------------------------------------------
# Customer flow
# --------------------------------------------------------------------------

TEXTS = {
    "en": {
        "welcome": "Welcome! Choose an option below.",
        "store": "🛍 Store / Products",
        "search": "🔍 Search",
        "orders": "📦 Orders",
        "support": "💬 Support",
        "language": "🌐 Language",
        "back": "🔙 Back to Menu",
        "lang_set": "Language set to English 🇬🇧",
    },
    "hi": {
        "welcome": "स्वागत है! नीचे दिए गए विकल्पों में से चुनें।",
        "store": "🛍 स्टोर / प्रोडक्ट्स",
        "search": "🔍 खोजें",
        "orders": "📦 ऑर्डर",
        "support": "💬 सहायता",
        "language": "🌐 भाषा",
        "back": "🔙 मेनू पर वापस",
        "lang_set": "भाषा हिंदी 🇮🇳 पर सेट हो गई है।",
    },
    "es": {
        "welcome": "¡Bienvenido! Elige una opción a continuación.",
        "store": "🛍 Tienda / Productos",
        "search": "🔍 Buscar",
        "orders": "📦 Pedidos",
        "support": "💬 Soporte",
        "language": "🌐 Idioma",
        "back": "🔙 Volver al menú",
        "lang_set": "Idioma configurado en español 🇪🇸",
    },
    "zh": {
        "welcome": "欢迎！请选择下方选项。",
        "store": "🛍 商店 / 产品",
        "search": "🔍 搜索",
        "orders": "📦 订单",
        "support": "💬 客服",
        "language": "🌐 语言",
        "back": "🔙 返回菜单",
        "lang_set": "语言已设置为中文 🇨🇳",
    },
}
SUPPORT_USERNAME = (os.getenv("SUPPORT_USERNAME") or "").lstrip("@")
prefs: dict[str, str] = {}  # in-memory cache; MongoDB (via storage.py) is the durable copy
storage: Storage | None = None
credbox: CredentialBox | None = None  # encrypts credentials stored on orders


def t(user_id: int, key: str) -> str:
    return TEXTS.get(prefs.get(str(user_id), "en"), TEXTS["en"])[key]


class Search(StatesGroup):
    waiting_query = State()


def back_button(user_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=t(user_id, "back"), callback_data="menu")


def back_menu(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(back_button(user_id))
    return builder.as_markup()


def main_menu(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t(user_id, "store"), callback_data="browse:0"))
    builder.row(
        InlineKeyboardButton(text=t(user_id, "search"), callback_data="search"),
        InlineKeyboardButton(text=t(user_id, "orders"), callback_data="my_orders"),
    )
    builder.row(
        InlineKeyboardButton(text=t(user_id, "support"), callback_data="support"),
        InlineKeyboardButton(text=t(user_id, "language"), callback_data="language"),
    )
    return builder.as_markup()


BOT_COMMANDS = [
    BotCommand(command="start", description="Start new session"),
    BotCommand(command="menu", description="Show main dashboard"),
    BotCommand(command="products", description="Browse store & products"),
    BotCommand(command="orders", description="View your orders"),
    BotCommand(command="search", description="Search products"),
    BotCommand(command="support", description="Contact customer support"),
]

CUSTOMER_STATUS = {
    "pending": "Pending Approval",
    "held": "Pending Approval",
    "fulfilling": "Processing",
    "reconcile": "Processing",
    "delivered": "Delivered",
    "rejected": "Rejected",
    "purchased_undelivered": "Processing",
    "price_review": "Processing",
    "delivery_failed": "Failed",
}
MY_ORDERS_LIMIT = 5


async def send_menu(message: Message, user_id: int) -> None:
    await message.answer(t(user_id, "welcome"), reply_markup=main_menu(user_id))


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await send_menu(message, message.from_user.id)


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    uid = callback.from_user.id
    try:
        await callback.message.edit_text(t(uid, "welcome"), reply_markup=main_menu(uid))
    except Exception:  # e.g. the message can't be edited; send a fresh one
        await send_menu(callback.message, uid)


# ---- Orders ---------------------------------------------------------------

def order_credentials(order: dict) -> list[str]:
    """The order's credentials, decrypted. Legacy plaintext orders are still readable."""
    token = order.get("credentials_enc")
    if token:
        try:
            return credbox.decrypt_list(token)
        except CredentialDecryptError:
            logging.error("Order %s: stored credentials could not be decrypted", order.get("id"))
            return []
    return list(order.get("credentials") or [])


async def render_orders(user_id: int) -> str:
    try:
        mine = await storage.recent_orders(user_id, MY_ORDERS_LIMIT)  # newest first
    except PyMongoError as exc:
        log_db_error("loading order history", exc, user=user_id)
        return "Sorry, I couldn't load your orders right now. Please try again later."
    if not mine:
        return "You have no past orders yet."

    blocks = []
    for order in mine:
        status = CUSTOMER_STATUS.get(order["status"], "Failed")
        block = f"📦 {order['name']}\nAmount: {order['price']:.2f} USDT\nStatus: {status}"
        if order["status"] == "delivered":
            credentials = order_credentials(order)
            if credentials:
                block += "\nYour details:\n" + "\n".join(credentials)
            elif order.get("credentials_enc"):
                block += "\nYour details could not be displayed right now. Please contact support."
        blocks.append(block)
    return truncate_utf16("Your recent orders:\n\n" + "\n\n".join(blocks), TELEGRAM_MAX_UNITS)


@dp.message(Command("orders"))
async def cmd_orders(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await render_orders(message.from_user.id), reply_markup=back_menu(message.from_user.id))


@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    uid = callback.from_user.id
    await callback.message.answer(await render_orders(uid), reply_markup=back_menu(uid))


# ---- Support / language -----------------------------------------------------

def support_view(user_id: int):
    builder = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        text = f"💬 Need help? Contact our support: @{SUPPORT_USERNAME}"
        builder.row(InlineKeyboardButton(text="💬 Contact Support", url=f"https://t.me/{SUPPORT_USERNAME}"))
    else:
        text = "💬 Need help? Please contact the store admin."
    builder.row(back_button(user_id))
    return text, builder.as_markup()


@dp.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    await state.clear()
    text, markup = support_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text, markup = support_view(callback.from_user.id)
    await callback.message.answer(text, reply_markup=markup)


@dp.message(Command("language"))
async def cmd_language(message: Message):
    await message.answer("🌐 Choose your language:", reply_markup=language_markup(message.from_user.id))


def language_markup(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="English 🇬🇧", callback_data="lang:en"),
        InlineKeyboardButton(text="Hindi 🇮🇳", callback_data="lang:hi"),
    )
    builder.row(
        InlineKeyboardButton(text="Spanish 🇪🇸", callback_data="lang:es"),
        InlineKeyboardButton(text="Chinese 🇨🇳", callback_data="lang:zh"),
    )
    builder.row(back_button(user_id))
    return builder.as_markup()


@dp.callback_query(F.data == "language")
async def cb_language(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🌐 Choose your language:", reply_markup=language_markup(callback.from_user.id)
    )


@dp.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery):
    lang = callback.data.split(":", 1)[1]
    if lang not in TEXTS:
        await callback.answer()
        return
    uid = callback.from_user.id
    prefs[str(uid)] = lang  # in-memory first, so the user's choice applies right away
    if storage is not None:
        try:
            await storage.set_lang(uid, lang)
        except Exception:
            logging.exception("Could not save language preference for user %s", uid)
    await callback.answer()
    text = f"{t(uid, 'lang_set')}\n\n{t(uid, 'welcome')}"
    try:
        await callback.message.edit_text(text, reply_markup=main_menu(uid))
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu(uid))


# ---- Catalog ----------------------------------------------------------------

def listing_button(listing: Listing) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=f"{listing.name[:45]} - {retail_price(listing.cost):.2f} USDT",
        callback_data=f"product:{listing.key}",
    )


def nav_row(page: int, total_pages: int, prefix: str) -> list[InlineKeyboardButton]:
    """[ Prev ] [ page / total ] [ Next ]; the middle button does nothing when tapped."""
    if total_pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"{prefix}:{page + 1}"))
    return row


@dp.callback_query(F.data == "ignore")
async def ignore_tap(callback: CallbackQuery):
    await callback.answer()


async def render_browse(page: int, user_id: int):
    catalog = await get_catalog()
    if not catalog:
        return "No products available right now. Please try again later.", back_menu(user_id)

    listings = sorted(catalog.values(), key=lambda l: l.name.lower())
    total_pages = math.ceil(len(listings) / PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    builder = InlineKeyboardBuilder()
    for listing in listings[start:start + PAGE_SIZE]:
        builder.row(listing_button(listing))
    nav = nav_row(page, total_pages, "browse")
    if nav:
        builder.row(*nav)
    builder.row(back_button(user_id))
    return f"🛍 Showing products (Page {page + 1} of {total_pages})", builder.as_markup()


@dp.message(Command("products"))
async def cmd_products(message: Message, state: FSMContext):
    await state.clear()
    text, markup = await render_browse(0, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("browse:"))
async def browse_products(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text, markup = await render_browse(int(callback.data.split(":")[1]), callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("product:"))
async def show_product(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    await callback.answer()

    listing = (await get_catalog()).get(key)
    if listing is None:
        await callback.message.answer("That product is no longer available.")
        return

    lines = [listing.name, f"Price: {retail_price(listing.cost):.2f} USDT"]
    if listing.description:
        lines.append(truncate_utf16(listing.description, 3000))  # the whole message must stay under 4096 UTF-16 units

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Buy Now", callback_data=f"buy:{key}"))
    builder.row(InlineKeyboardButton(text="⬅️ Back to products", callback_data="browse:0"))
    builder.row(back_button(callback.from_user.id))
    await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())


# ---- Search -----------------------------------------------------------------

SEARCH_PROMPT = "Type a keyword or product name to search (e.g. Canva, ChatGPT, Netflix):"


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await state.set_state(Search.waiting_query)
    await message.answer(SEARCH_PROMPT, reply_markup=back_menu(message.from_user.id))


@dp.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Search.waiting_query)
    await callback.answer()
    await callback.message.answer(SEARCH_PROMPT, reply_markup=back_menu(callback.from_user.id))


last_search: dict[int, str] = {}  # user id -> most recent query, for paging


async def render_search(query: str, page: int, user_id: int):
    words = query.lower().split()
    matches = sorted(
        (
            l for l in (await get_catalog()).values()
            if all(w in l.name.lower() or w in l.description.lower() for w in words)
        ),
        key=lambda l: l.name.lower(),
    )

    builder = InlineKeyboardBuilder()
    shown = query.strip()[:50]
    if matches:
        total_pages = math.ceil(len(matches) / PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * PAGE_SIZE
        for listing in matches[start:start + PAGE_SIZE]:
            builder.row(listing_button(listing))
        nav = nav_row(page, total_pages, "sr")
        if nav:
            builder.row(*nav)
        text = f"🔍 {len(matches)} result(s) for “{shown}” (Page {page + 1} of {total_pages})"
    else:
        text = f"No in-stock products found for “{shown}”."
    builder.row(InlineKeyboardButton(text="🔍 Search again", callback_data="search"))
    builder.row(back_button(user_id))
    return text, builder.as_markup()


@dp.message(Search.waiting_query, F.text, ~F.text.startswith("/"))
async def receive_search(message: Message, state: FSMContext):
    await state.clear()
    last_search[message.from_user.id] = message.text.strip()
    text, markup = await render_search(message.text, 0, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("sr:"))
async def search_page(callback: CallbackQuery):
    await callback.answer()
    query = last_search.get(callback.from_user.id)
    if not query:
        await callback.message.answer("That search has expired. Please search again.")
        return
    text, markup = await render_search(query, int(callback.data.split(":")[1]), callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("buy:"))
async def buy_now(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    await callback.answer()

    methods = {k: m for k, m in PAYMENT_METHODS.items() if m["destination"]}
    if not methods:
        await callback.message.answer("Payments are not configured yet. Please contact support.")
        return

    listing = (await get_catalog(force=True)).get(key)
    if listing is None:
        await callback.message.answer("That product is no longer available.")
        return

    builder = InlineKeyboardBuilder()
    for method_key, method in methods.items():
        builder.row(InlineKeyboardButton(text=method["label"], callback_data=f"pay:{method_key}:{key}"))
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel_order"))
    await callback.message.answer(
        f"Order: {listing.name}\n"
        f"Amount: {retail_price(listing.cost):.2f} USDT\n\n"
        "Choose a payment method:",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("pay:"))
async def choose_payment(callback: CallbackQuery, state: FSMContext):
    _, method_key, key = callback.data.split(":", 2)
    await callback.answer()

    method = PAYMENT_METHODS.get(method_key)
    if method is None or not method["destination"]:
        await callback.message.answer("That payment method isn't available.")
        return

    listing = (await get_catalog(force=True)).get(key)
    if listing is None:
        await callback.message.answer("That product is no longer available.")
        return

    try:
        reserved = await reserve_for_checkout(callback.from_user.id, listing.offers[0])
    except PyMongoError as exc:
        log_db_error("reserving stock", exc, user=callback.from_user.id)
        await callback.message.answer(CHECKOUT_RETRY_LATER)
        return
    if reserved == "gone":
        await callback.message.answer(STOCK_GONE_MESSAGE)
        return

    price = retail_price(listing.cost)
    await state.set_state(Checkout.waiting_reference)
    await state.update_data(
        name=listing.name,
        price=price,
        method=method_key,
        offers=[asdict(o) for o in listing.offers],
        **(reserved or {}),
    )

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel_order"))
    await callback.message.answer(
        f"Order: {listing.name}\n"
        f"Amount: {price:.2f} USDT\n"
        f"Method: {method['label']}\n\n"
        f"Send exactly this amount to the {method['dest_label']}:\n{method['destination']}\n\n"
        f"After paying, reply here with the {method['ref_label']}."
        + (
            f"\n\nThis item is reserved for you for {RESERVATION_TTL_SECONDS // 60} minutes."
            if reserved
            else ""
        ),
        reply_markup=builder.as_markup(),
    )


STOCK_GONE_MESSAGE = (
    "Sorry, this item has just run out of stock or is being held for other buyers right now. "
    "Please check again in a few minutes."
)
CHECKOUT_RETRY_LATER = "Sorry, we couldn't start your order right now. Please try again in a moment."


async def reserve_for_checkout(user_id: int, offer: Offer):
    """Hold a unit for the buyer while they pay.

    Returns None when the stock is unknown or plentiful (nothing to reserve), "gone" when no
    unit is available, or the reservation details to keep in the checkout state."""
    stock = offer.stock
    if stock is None or stock > RESERVE_MAX_TRACKED_STOCK:
        return None
    key = reservation_key(offer.supplier, offer.product_id, offer.variant_id)
    token = await storage.reserve_stock(key, user_id, stock, RESERVATION_TTL_SECONDS)
    if token is None:
        return "gone"
    return {"reservation_token": token, "reservation_key": key, "reservation_stock": stock}


async def release_checkout_reservation(state) -> None:
    """The buyer cancelled: give the unit back right away instead of waiting for the TTL."""
    token = (await state.get_data()).get("reservation_token")
    if not token:
        return
    try:
        await storage.release_reservation(token=token)
    except PyMongoError as exc:
        log_db_error("releasing a stock reservation", exc)


async def release_order_stock(order_id: str) -> None:
    """The order was rejected, failed or its purchase is done: free its reserved unit."""
    try:
        await storage.release_reservation(order_id=order_id)
    except PyMongoError as exc:
        log_db_error("releasing a stock reservation", exc, order=order_id)


async def attach_order_reservation(order_id: str, user_id: int, data: dict) -> str:
    """Tie the buyer's reservation to the order just created from their payment proof.

    The buyer has already paid, so the order is created whatever happens here: if the
    reservation expired and the unit was given away, one more attempt is made to hold a
    unit, and otherwise the order is flagged "lapsed" so the admin knows the purchase may
    fail. Returns "held", "lapsed", "untracked" or "unknown" (database error)."""
    token = data.get("reservation_token")
    if not token:
        return "untracked"
    try:
        if await storage.attach_reservation(token, order_id, ORDER_RESERVATION_TTL_SECONDS):
            return "held"
        new_token = await storage.reserve_stock(
            data["reservation_key"], user_id, data["reservation_stock"], ORDER_RESERVATION_TTL_SECONDS
        )
        if new_token and await storage.attach_reservation(new_token, order_id, ORDER_RESERVATION_TTL_SECONDS):
            return "held"
        return "lapsed"
    except (PyMongoError, KeyError) as exc:
        logging.error("Order %s: could not attach the stock reservation (%s)", order_id, type(exc).__name__)
        return "unknown"


@dp.callback_query(F.data == "cancel_order")
async def cancel_order(callback: CallbackQuery, state: FSMContext):
    await release_checkout_reservation(state)
    await state.clear()
    await callback.answer("Order cancelled")
    await send_menu(callback.message, callback.from_user.id)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await release_checkout_reservation(state)
    await state.clear()
    await send_menu(message, message.from_user.id)


# Registered after /cancel, and commands are excluded so they never reach the validator.
@dp.message(Checkout.waiting_reference, F.text, ~F.text.startswith("/"))
async def receive_reference(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        wait = proof_limiter.check(message.from_user.id)
        if wait:
            await message.answer(
                f"You have submitted too many payment proofs. Please wait {wait_text(wait)} and try again."
            )
            return
    reference = message.text.strip()
    data = await state.get_data()
    method = PAYMENT_METHODS[data["method"]]

    pattern = BINANCE_ORDER_RE if data["method"] == "binance" else TXID_RE
    if not pattern.match(reference):
        await message.answer(
            f"That doesn't look like a valid {method['ref_label']}. Please try again, or /cancel."
        )
        return
    try:
        if await storage.reference_exists(reference):
            await message.answer(DUPLICATE_REFERENCE)
            return
    except PyMongoError as exc:
        log_db_error("checking reference", exc, user=message.from_user.id)
        await message.answer(RETRY_LATER)
        return

    user = message.from_user
    order_id = uuid.uuid4().hex[:10]
    set_order_id(order_id)
    order = {
        "id": order_id,
        "status": "pending",
        "user_id": user.id,
        "user_label": f"{user.full_name} (@{user.username or '-'}, id {user.id})",
        "name": data["name"],
        "price": data["price"],
        "method": data["method"],
        "reference": reference,
        "pay_to": method["destination"],  # snapshot of where the customer was told to pay
        "offers": data["offers"],
        "admin_notified": False,  # durable outbox marker: set True only once Telegram confirms the alert
    }
    # Persist before confirming; keep the checkout state if this fails so the user can resend.
    try:
        await storage.insert_order(order)
    except DuplicateKeyError as exc:
        # Lost a race with another submission of the same payment proof (the unique index
        # on reference_lower rejected it). An order-id collision is not the user's doing.
        key_pattern = (getattr(exc, "details", None) or {}).get("keyPattern") or {}
        await message.answer(RETRY_LATER if "_id" in key_pattern else DUPLICATE_REFERENCE)
        return
    except PyMongoError as exc:
        log_db_error("saving new order", exc, order=order_id, user=user.id)
        await message.answer(RETRY_LATER)
        return

    order["stock_reservation"] = await attach_order_reservation(order_id, user.id, data)
    if order["stock_reservation"] != "untracked":
        try:
            await storage.update_order(
                order_id, {"stock_reservation": order["stock_reservation"]}, only_if_status="pending"
            )
        except PyMongoError as exc:
            log_db_error("recording the stock reservation", exc, order=order_id)

    await state.clear()

    await message.answer(
        "Thanks! Your payment is pending verification. "
        "Your order will be delivered here as soon as it is confirmed.",
        reply_markup=main_menu(user.id),
    )

    delivered = await alert_admin(
        message.bot,
        new_order_alert(order),
        reply_markup=admin_keyboard(order_id),
        order_id=order_id,
    )
    if delivered:
        try:
            await storage.mark_admin_notified(order_id)
        except PyMongoError as exc:
            # The alert was sent; if the marker isn't saved, a duplicate alert will follow. Never a loss.
            log_db_error("recording admin notification", exc, order=order_id)
    # If not delivered, the marker stays False and the retry loop re-sends it.



# --------------------------------------------------------------------------
# Admin: approve -> purchase -> deliver
# --------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    set_order_id(order_id)
    try:
        # Refuses orders that are fulfilling or need manual reconciliation.
        order = await storage.reject_order(order_id)
    except PyMongoError as exc:
        log_db_error("rejecting order", exc, order=order_id)
        await callback.answer("Database error - nothing was changed. Try again.", show_alert=True)
        return
    if order is None:
        await callback.answer("This order can't be rejected now.", show_alert=True)
        return

    await release_order_stock(order_id)
    await callback.answer("Rejected")
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\n❌ Rejected")
    except Exception:
        logging.exception("Could not edit admin message")
    try:
        await callback.bot.send_message(
            order["user_id"], "Your payment could not be verified, so the order was cancelled. Contact support."
        )
    except Exception:
        logging.exception("Could not notify user %s", order["user_id"])


@dp.callback_query(F.data.startswith("approve:"))
async def approve_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    set_order_id(order_id)

    # The atomic pending/held -> fulfilling claim is the only gate to a supplier call.
    try:
        order = await storage.claim_order(order_id)
    except PyMongoError as exc:
        log_db_error("claiming order", exc, order=order_id)
        await callback.answer("Database error - nothing was purchased. Try again.", show_alert=True)
        return
    if order is None:
        await callback.answer("This order can't be approved now.", show_alert=True)
        return
    await process_claimed_order(callback, order)


async def process_claimed_order(callback: CallbackQuery, order: dict) -> None:
    """Everything after an order has been claimed (by Approve/Retry, or by approving a price
    change): the purchase, persistence of the credentials, delivery, and every failure path."""
    order_id = order["id"]
    bot = callback.bot
    phase = "pre"  # pre: no supplier call yet; buying: call in flight; post: purchase completed

    try:
        await callback.answer("Purchasing…")
        try:
            await callback.message.edit_text(f"{callback.message.text}\n\n⏳ Approved - purchasing…")
        except Exception:
            logging.exception("Could not edit admin message")
        try:
            await bot.send_message(order["user_id"], "Payment confirmed ✅ Preparing your order…")
        except Exception:
            logging.exception("Could not notify user %s", order["user_id"])

        loop = asyncio.get_running_loop()
        claim_token = order.get("claim_token")

        def fence() -> None:
            """Runs in the worker thread before every purchase request."""
            future = asyncio.run_coroutine_threadsafe(storage.owns_claim(order_id, claim_token), loop)
            try:
                owns = future.result(timeout=FENCE_TIMEOUT)
            except Exception as exc:
                future.cancel()
                raise FenceUnavailable(type(exc).__name__) from None
            if not owns:
                raise ClaimLostError("this instance no longer owns the order")

        phase = "buying"
        try:
            credentials, offer, failures = await asyncio.to_thread(fulfil, order, fence)
        except PriceChanged as exc:
            await request_price_decision(bot, order, exc)
            return
        except SupplierError as exc:
            # Every SupplierError is a proven no-purchase failure, so a normal Retry stays available.
            await hold_order(bot, order, f"All suppliers failed: {exc}", attempt=order.get("attempt", 0) + 1)
            return
        except ClaimLostError:
            # Recovery or another instance took the order over. Nothing is written here
            # (the claim is gone) and no further supplier request is sent from this instance.
            logging.error("Order %s: claim lost during the supplier call; stopped", order_id)
            await alert_admin(
                bot,
                f"⚠️ Order #{order_id}: this bot instance lost ownership of the order (it was recovered "
                "or changed by another instance) and stopped before sending any further supplier request. "
                "It was not overwritten. It stays in manual reconciliation: check the supplier account.",
                order_id=order_id,
            )
            return
        except AmbiguousOrderError as exc:
            await reconcile_order(bot, order, f"Purchase outcome unknown: {exc}")
            return
        except Exception as exc:
            # Not provably a no-purchase failure, so it is treated as unknown.
            logging.error("Unclassified %s fulfilling order %s", type(exc).__name__, order_id)
            await reconcile_order(
                bot, order, f"Unclassified error ({type(exc).__name__}), treated as unknown outcome"
            )
            return

        phase = "post"
        # The purchase is done: whatever happens below, the credentials must not be lost.
        # They are stored (encrypted) on the order BEFORE any delivery is attempted.
        purchased_fields = {
            "status": "purchased_undelivered",
            "outcome": "purchased",
            "supplier": offer.supplier,
            "actual_cost": offer.cost,
            "credentials_enc": credbox.encrypt_list(credentials),
            "purchased_at": datetime.now(timezone.utc),
        }
        result = await persist_order(order_id, purchased_fields, "fulfilling", claim_token)
        if result == STALE:
            # Something else moved the order mid-purchase (e.g. a restart recovered it to
            # reconcile). Complete it only from reconcile; never overwrite a newer status.
            result = await persist_order(order_id, purchased_fields, "reconcile", claim_token)
        await release_order_stock(order_id)  # the unit is bought: the supplier's stock now reflects it
        saved = result == SAVED
        stale = result == STALE
        marked = False
        if result == FAILED:
            # Block Retry/Reject: this order IS purchased even though its credentials weren't stored.
            marked = (
                await persist_order(
                    order_id,
                    {"status": "reconcile", "outcome": "purchased", "error": PURCHASED_UNRESOLVED},
                    ["fulfilling", "reconcile"],
                    claim_token,
                )
                == SAVED
            )

        delivered = await deliver_credentials(bot, order, credentials)

        status_update_failed = False
        if saved and delivered:
            status_update_failed = (
                await persist_order(
                    order_id,
                    {"status": "delivered", "delivered_at": datetime.now(timezone.utc)},
                    "purchased_undelivered",
                    claim_token,
                )
                != SAVED
            )

        if not saved or not delivered or status_update_failed:
            problems = []
            if stale:
                problems.append(
                    "The order's state was changed by something else while the purchase was running, so "
                    "the purchase could not be recorded and nothing was overwritten. It IS PURCHASED: "
                    "do not unlock, retry or reject it; reconcile manually."
                )
            elif not saved and marked:
                problems.append(
                    "The credentials could not be stored in the database. The order is marked "
                    "PURCHASED but unresolved and is blocked from Retry/Reject. Reconcile it manually."
                )
            elif not saved:
                problems.append(
                    "The database is unavailable, so nothing could be recorded. The order is still "
                    "'fulfilling' and will appear as needing reconciliation after a restart. It IS "
                    "PURCHASED: never unlock, retry or reject it."
                )
            if not saved and delivered:
                problems.append("The credentials were sent to the customer.")
            if not delivered and saved:
                problems.append(
                    "Delivery to the customer failed. The credentials are stored (encrypted) on the order, "
                    "now 'purchased_undelivered', and the bot retries delivery automatically. If the "
                    "customer has blocked the bot, forward the credentials below manually."
                )
            elif not delivered:
                problems.append("Delivery to the customer failed; forward the credentials manually.")
            if status_update_failed:
                problems.append(
                    "The credentials reached the customer but the order could not be marked delivered; "
                    "the bot may send them again."
                )
            sent = await alert_admin(
                bot,
                f"🚨 Order #{order_id} was purchased but needs attention.\n"
                f"User: {order['user_label']}\n"
                + "\n".join(problems)
                + "\n\n"
                + "\n".join(credentials),
                protect_content=True,
                attempts=5,
                order_id=order_id,
            )
            if not sent:
                # Never treated as resolved: the order stays blocked (reconcile/purchased or
                # purchased_undelivered) and is flagged in the log and on the admin's approval message.
                await flag_unalerted_purchase(callback, order)
            return

        note = f" (fallbacks: {'; '.join(failures)})" if failures else ""
        try:
            await callback.message.edit_text(f"{callback.message.text}\n\n✅ Delivered via {offer.supplier}{note}")
        except Exception:
            logging.exception("Could not edit admin message")
    except Exception as exc:
        # Never leave a claimed order silently in 'fulfilling'. The supplier is not called again here.
        logging.error("Unexpected %s after claim for order %s (phase=%s)", type(exc).__name__, order_id, phase)
        try:
            if phase == "pre":
                # No supplier call was made, so this is a proven no-purchase failure.
                await hold_order(
                    bot, order,
                    f"Internal error before any supplier call ({type(exc).__name__}); no purchase was made.",
                )
            elif phase == "post":
                result = await persist_order(
                    order_id,
                    {"status": "reconcile", "outcome": "purchased", "error": PURCHASED_UNRESOLVED},
                    "fulfilling",
                    claim_token,
                )
                sent = await alert_admin(
                    bot,
                    f"🚨 Order #{order_id} was PURCHASED but an unexpected error interrupted the follow-up.\n"
                    f"User: {order['user_label']}\n"
                    + (
                        "It is recorded as purchased/unresolved and is blocked from Retry/Reject. "
                        if result == SAVED
                        else "Its state could not be recorded (it may already have been updated); check it. "
                    )
                    + "Never unlock, retry or reject it. The credentials may or may not have reached "
                    "the customer.\n\n"
                    + "\n".join(credentials),
                    protect_content=True,
                    attempts=5,
                    order_id=order_id,
                )
                if not sent:
                    await flag_unalerted_purchase(callback, order)
            else:
                await reconcile_order(
                    bot, order, f"Unexpected error ({type(exc).__name__}) during the supplier call"
                )
        except Exception as inner:
            logging.error(
                "Could not record failure state for order %s: %s", order_id, type(inner).__name__
            )


CUSTOMER_DELAY_NOTICE = (
    "Your payment is confirmed, but delivery is delayed. "
    "Our team has been notified and will sort it out shortly."
)
# Statuses in which the customer no longer needs a "delayed" notice.
NOTICE_OBSOLETE_STATUSES = {"rejected", "delivered", "purchased_undelivered", "delivery_failed"}


async def hold_order(bot: Bot, order: dict, reason: str, *, attempt: int | None = None) -> None:
    """Park an order after a PROVEN no-purchase failure; ordinary Retry/Reject apply.

    The admin alert and the customer notice are recorded on the order in the same write as
    the state change, then sent; if sending fails they are retried with backoff, also after
    a restart (see flush_outbox)."""
    fields = {"status": "held", "outcome": "definitive_failure", "error": reason, **outbox_fields("held")}
    if attempt is not None:
        fields["attempt"] = attempt
    result = await persist_order(order["id"], fields, "fulfilling", order["claim_token"])
    logging.error("Order %s held: %s", order["id"], reason)
    if result == SAVED:
        await release_order_stock(order["id"])
        await flush_one(bot, order["id"])
        return
    await alert_admin(
        bot,
        f"🚨 EMERGENCY: order #{order['id']} is on hold\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
        f"Reason: {reason}\n\n"
        "The customer has paid but has not received the product. " + unsaved_note(result),
        order_id=order["id"],
    )
    await notify_delay(bot, order)


async def reconcile_order(bot: Bot, order: dict, reason: str) -> None:
    """Park an order whose supplier outcome is unknown: no Retry or Reject, and no
    attempt change, until an admin verifies and explicitly unlocks it. Alert and customer
    notice are durable, as in hold_order."""
    result = await persist_order(
        order["id"],
        {"status": "reconcile", "outcome": "unknown", "error": reason, **outbox_fields("reconcile")},
        "fulfilling",
        order["claim_token"],
    )
    logging.error("Order %s needs manual reconciliation: %s", order["id"], reason)
    if result == SAVED:
        await release_order_stock(order["id"])
        await flush_one(bot, order["id"])
        return
    await alert_admin(
        bot,
        f"🚨 MANUAL RECONCILIATION: order #{order['id']}\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
        f"Supplier client reference: {purchase_ref_for(order)}\n"
        f"Reason: {reason}\n\n"
        "The supplier purchase may or may not have happened. Check the supplier account for the "
        "client reference above. Retry and Reject are blocked. " + unsaved_note(result),
        order_id=order["id"],
    )
    await notify_delay(bot, order)


async def request_price_decision(bot: Bot, order: dict, change: PriceChanged) -> None:
    """The live cost is above the order's ceiling. Do not fail the order: park it for the
    admin with the new cost, margin and difference (the alert is durable, see order_alert)."""
    old_cost = next(
        (float(o["cost"]) for o in order["offers"] if o["supplier"] == change.supplier),
        float(order["offers"][0]["cost"]),
    )
    review = {
        "supplier": change.supplier,
        "old_cost": old_cost,
        "new_cost": change.new_cost,
        "detected_at": datetime.now(timezone.utc),
    }
    result = await persist_order(
        order["id"],
        {"status": "price_review", "outcome": "definitive_failure", "price_review": review, **outbox_fields("price")},
        "fulfilling",
        order["claim_token"],
    )
    logging.warning("Order %s waits for a price decision (cost %.2f -> %.2f)", order["id"], old_cost, change.new_cost)
    if result == SAVED:
        await flush_one(bot, order["id"])
        return
    await alert_admin(
        bot,
        f"💱 Order #{order['id']}: the cost rose to {change.new_cost:.2f} (was {old_cost:.2f}) and the "
        "decision could not be recorded. " + unsaved_note(result),
        order_id=order["id"],
    )
    await notify_delay(bot, order)


@dp.callback_query(F.data.startswith("pricego:"))
async def price_approve(callback: CallbackQuery):
    """Approve buying at the new (higher) cost."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    set_order_id(order_id)
    try:
        order = await storage.claim_price_review(order_id)
    except PyMongoError as exc:
        log_db_error("claiming a price decision", exc, order=order_id)
        await callback.answer("Database error - nothing was purchased. Try again.", show_alert=True)
        return
    if order is None:
        await callback.answer("This order is not waiting for a price decision.", show_alert=True)
        return
    await process_claimed_order(callback, order)


@dp.callback_query(F.data.startswith("pricehold:"))
async def price_hold(callback: CallbackQuery):
    """Hold the order instead of buying at the new cost (Retry and Reject then apply)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    set_order_id(order_id)
    try:
        order = await storage.hold_price_review(order_id)
    except PyMongoError as exc:
        log_db_error("holding a price decision", exc, order=order_id)
        await callback.answer("Database error - nothing was changed. Try again.", show_alert=True)
        return
    if order is None:
        await callback.answer("This order is not waiting for a price decision.", show_alert=True)
        return
    await release_order_stock(order_id)
    await callback.answer("Held")
    text = (
        f"⏸ Order #{order_id} is on hold after the price change. Nothing was bought. "
        "Tap Retry to look at the price again, or Reject to cancel."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_keyboard(order_id, retry=True))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_keyboard(order_id, retry=True))


async def notify_delay(bot: Bot, order: dict) -> None:
    """Best-effort customer notice for the cases where nothing could be recorded."""
    try:
        await bot.send_message(order["user_id"], CUSTOMER_DELAY_NOTICE)
    except Exception:
        logging.exception("Could not notify user %s", order["user_id"])


def price_keyboard(order_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Approve at new cost", callback_data=f"pricego:{order_id}"),
        InlineKeyboardButton(text="⏸ Hold", callback_data=f"pricehold:{order_id}"),
    )
    return builder.as_markup()


def price_review_text(order: dict) -> str:
    review = order["price_review"]
    price, old_cost, new_cost = float(order["price"]), float(review["old_cost"]), float(review["new_cost"])
    difference = new_cost - old_cost
    percent = (difference / old_cost * 100) if old_cost else 0.0
    margin_old, margin_new = price - old_cost, price - new_cost
    text = (
        "💱 PRICE CHANGED - your decision is needed\n"
        f"Order #{order['id']}\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']}\n\n"
        f"Customer paid: {price:.2f} USDT\n"
        f"Cost when ordered: {old_cost:.2f}\n"
        f"Cost now ({review['supplier']}): {new_cost:.2f}\n"
        f"Difference: {difference:+.2f} ({percent:+.1f}%)\n"
        f"Margin if you buy now: {margin_new:.2f} USDT ({margin_new / price * 100:.1f}% of the price)\n"
        f"Margin at order time: {margin_old:.2f} USDT ({margin_old / price * 100:.1f}%)\n"
    )
    if margin_new < 0:
        text += f"⚠️ Buying now sells at a LOSS of {-margin_new:.2f} USDT.\n"
    return text + "\nNothing has been bought. Approve to buy at the new cost, or Hold."


def order_alert(order: dict):
    """The admin alert owed for an order, built from its CURRENT state, or None when the
    order has moved on and the alert is no longer relevant. Never contains credentials;
    an unlock button is offered only for an unknown outcome, never for a purchased order."""
    status, outcome, kind = order["status"], order.get("outcome"), order.get("alert_kind")
    head = (
        f"Order #{order['id']}\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
    )
    reason = order.get("error", "-")
    if kind == "price" and status == "price_review" and "price_review" in order:
        return price_review_text(order), price_keyboard(order["id"])
    if kind == "held" and status == "held":
        return (
            "🚨 EMERGENCY: order is on hold\n" + head + f"Reason: {reason}\n\n"
            "The customer has paid but has not received the product. No purchase was made. "
            "Fix the issue and tap Retry, or Reject to cancel.",
            admin_keyboard(order["id"], retry=True),
        )
    if kind in ("reconcile", "recovered") and status == "reconcile":
        title = (
            "⚠️ Order was interrupted by a restart and needs manual reconciliation"
            if kind == "recovered"
            else "🚨 MANUAL RECONCILIATION"
        )
        ref = f"Supplier client reference: {purchase_ref_for(order)}\n"
        if outcome == "purchased":
            return (title + "\n" + head + ref + "\nIt is recorded as PURCHASED. Never retry or reject it.", None)
        return (
            title + "\n" + head + ref + (f"Reason: {reason}\n" if kind == "reconcile" else "") + "\n"
            "The supplier purchase may or may not have happened. Check the supplier account for the "
            "client reference above. Retry and Reject are blocked until Pandora confirms that no "
            "purchase exists and no request is still pending, and you record that confirmation.",
            reconcile_keyboard(order["id"]),
        )
    return None


async def deliver_order_alert(bot: Bot, order: dict) -> bool:
    """Send the owed admin alert; the flag is cleared only after Telegram confirms."""
    built = order_alert(order)
    if built is not None:
        text, markup = built
        if not await alert_admin(bot, text, reply_markup=markup, order_id=order["id"]):
            try:
                attempts = await storage.record_alert_failure(order["id"])
                logging.error("Order %s: admin alert failed (attempt %s); will retry with backoff", order["id"], attempts)
            except PyMongoError as exc:
                log_db_error("recording alert failure", exc, order=order["id"])
            return False
    try:  # sent, or no longer relevant
        await storage.mark_alert_sent(order["id"])
    except PyMongoError as exc:
        log_db_error("clearing alert flag", exc, order=order["id"])  # a duplicate alert may follow
    return True


async def deliver_customer_notice(bot: Bot, order: dict) -> bool:
    """Tell the customer their order is delayed; stops after MAX_NOTICE_ATTEMPTS failures."""
    if order["status"] not in NOTICE_OBSOLETE_STATUSES:
        try:
            await bot.send_message(order["user_id"], CUSTOMER_DELAY_NOTICE)
        except Exception as exc:
            logging.error("Order %s: customer notice failed (%s)", order["id"], type(exc).__name__)
            try:
                await storage.record_notice_failure(order["id"])
            except PyMongoError as db_exc:
                log_db_error("recording notice failure", db_exc, order=order["id"])
            return False
    try:
        await storage.mark_notice_sent(order["id"])
    except PyMongoError as exc:
        log_db_error("clearing notice flag", exc, order=order["id"])
    return True


async def flush_one(bot: Bot, order_id: str) -> None:
    """Send what one order owes right now (used right after its state change)."""
    try:
        order = await storage.get_order(order_id)
    except PyMongoError as exc:
        log_db_error("loading order for its outbox", exc, order=order_id)
        return
    if order is None:
        return
    with order_context(order_id):
        if order.get("alert_pending"):
            await deliver_order_alert(bot, order)
        if order.get("notice_pending"):
            await deliver_customer_notice(bot, order)


async def flush_outbox(bot: Bot) -> None:
    """Send every alert and notice that is owed and whose backoff has elapsed. Safe to
    repeat; this is what re-sends unsent alerts after a restart."""
    try:
        alerts, notices = await storage.alerts_due(), await storage.notices_due()
    except PyMongoError as exc:
        log_db_error("loading the outbox", exc)
        return
    for order in alerts:
        with order_context(order["id"]):
            await deliver_order_alert(bot, order)
    for order in notices:
        with order_context(order["id"]):
            await deliver_customer_notice(bot, order)


UNLOCK_NOTE_MIN, UNLOCK_NOTE_MAX = 3, 200


def unlock_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel - leave it blocked", callback_data="unlockno"))
    return builder.as_markup()


async def unlockable_order(order_id: str) -> dict | None:
    """The order, only if it is still awaiting reconciliation with an unknown outcome."""
    order = await storage.get_order(order_id)
    if order is None or order["status"] != "reconcile" or order.get("outcome") != "unknown":
        return None
    return order


@dp.callback_query(F.data.startswith("unlock:"))
async def unlock_start(callback: CallbackQuery, state: FSMContext):
    """Step 1: explain what must be confirmed with Pandora and ask for the confirmation note."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    set_order_id(order_id)
    try:
        order = await unlockable_order(order_id)
    except PyMongoError as exc:
        log_db_error("loading order to unlock", exc, order=order_id)
        await callback.answer("Database error - try again.", show_alert=True)
        return
    if order is None:
        await callback.answer("This order can't be unlocked.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(UnlockFlow.waiting_note)
    await state.update_data(order_id=order_id)
    await callback.message.answer(
        f"Unlock order #{order_id} for a retry purchase?\n"
        f"Supplier client reference: {purchase_ref_for(order)}\n\n"
        "This bot CANNOT check Pandora. Contact Pandora (or check your Pandora account) first and get "
        "confirmation of BOTH:\n"
        "1. no purchase exists for that client reference; and\n"
        "2. no submitted request is still pending or could complete later.\n\n"
        f"Then reply with Pandora's confirmation reference or a short note ({UNLOCK_NOTE_MIN}-{UNLOCK_NOTE_MAX} "
        "characters). If you cannot get that confirmation, tap Cancel: the order stays blocked, with "
        "Retry and Reject unavailable.",
        reply_markup=unlock_cancel_keyboard(),
    )


@dp.message(UnlockFlow.waiting_note, F.text, ~F.text.startswith("/"))
async def unlock_note(message: Message, state: FSMContext):
    """Step 2: take the confirmation note, then show the final confirmation screen."""
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    note = " ".join(message.text.split())
    if not UNLOCK_NOTE_MIN <= len(note) <= UNLOCK_NOTE_MAX:
        await message.answer(
            f"Please send Pandora's confirmation reference or a short note "
            f"({UNLOCK_NOTE_MIN}-{UNLOCK_NOTE_MAX} characters), or tap Cancel.",
            reply_markup=unlock_cancel_keyboard(),
        )
        return
    order_id = (await state.get_data())["order_id"]
    set_order_id(order_id)
    try:
        order = await unlockable_order(order_id)
    except PyMongoError as exc:
        log_db_error("loading order to unlock", exc, order=order_id)
        await message.answer("Database error - please send the note again, or tap Cancel.")
        return
    if order is None:
        await state.clear()
        await message.answer("This order can no longer be unlocked. Nothing was changed.")
        return

    nonce = uuid.uuid4().hex[:8]
    await state.set_state(UnlockFlow.confirming)
    await state.update_data(note=note, nonce=nonce)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Confirm: Pandora confirmed both", callback_data=f"unlockgo:{nonce}")
    )
    builder.row(InlineKeyboardButton(text="✖️ Cancel - leave it blocked", callback_data="unlockno"))
    await message.answer(
        f"FINAL CONFIRMATION - order #{order_id}\n"
        f"Supplier client reference: {purchase_ref_for(order)}\n"
        f"Your note: {note}\n\n"
        "By confirming you state that Pandora has confirmed to you that:\n"
        "1. no purchase exists for that client reference; and\n"
        "2. no submitted request is still pending or could complete later.\n\n"
        "The bot has NOT verified this with Pandora. Your admin ID, the time and your note will be "
        "recorded on the order, and a retry purchase (a new attempt) will become possible. "
        "If either point is not confirmed, tap Cancel.",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("unlockgo:"))
async def unlock_confirm(callback: CallbackQuery, state: FSMContext):
    """Step 3: unlock, only from the live final-confirmation screen of this flow."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    nonce = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if await state.get_state() != UnlockFlow.confirming.state or data.get("nonce") != nonce:
        await callback.answer(
            "This confirmation has expired. Start again from the order's Unlock button.", show_alert=True
        )
        return
    order_id, note = data["order_id"], data["note"]
    set_order_id(order_id)
    try:
        order = await storage.unlock_order(order_id, callback.from_user.id, note)
    except PyMongoError as exc:
        log_db_error("unlocking order", exc, order=order_id)
        await callback.answer("Database error - nothing was changed. Try again.", show_alert=True)
        return
    await state.clear()
    if order is None:
        await callback.answer(
            "This order can't be unlocked (it may be purchased or already changed).", show_alert=True
        )
        return

    await callback.answer("Unlocked")
    logging.warning(
        "Order %s unlocked for retry by admin %s on their Pandora confirmation; next attempt number is %s",
        order_id, callback.from_user.id, order.get("attempt"),
    )
    text = (
        f"🔓 Order #{order_id} unlocked on your confirmation that Pandora reported no purchase and no "
        f"pending request (attempt {order.get('attempt')}). Your ID, the time and your note were recorded. "
        "Tap Retry to purchase, or Reject to cancel."
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_keyboard(order_id, retry=True))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_keyboard(order_id, retry=True))


@dp.callback_query(F.data.startswith("unlockyes:"))
async def unlock_outdated(callback: CallbackQuery):
    """Buttons from the previous one-tap flow. They no longer unlock anything."""
    await callback.answer(
        "This button is outdated and does nothing. Use the order's Unlock button to start again.",
        show_alert=True,
    )


@dp.callback_query(F.data == "unlockno")
async def unlock_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    await state.clear()
    await callback.answer("Cancelled")
    text = "Unlock cancelled. The order stays in reconciliation; Retry and Reject remain unavailable."
    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)


STOCK_NOTES = {
    "held": "\nStock: a unit is reserved for this order.",
    "lapsed": (
        "\n⚠️ Stock: the reservation expired before the proof arrived and the unit may have gone, "
        "so the purchase can fail."
    ),
}


def new_order_alert(order: dict, delayed: bool = False) -> str:
    """The new-order alert the admin approves from. It states what payment to expect
    (amount, coin/network, destination) next to the proof the customer submitted, so the
    payment can be checked before tapping Approve. The proof is needed here and is never
    logged. Contains no credentials."""
    method = PAYMENT_METHODS.get(order["method"], {})
    coin = method.get("coin", "USDT")
    network = method.get("network", order["method"])
    proof_label = method.get("ref_label", "payment proof")
    best = order["offers"][0]
    fallback = f" (fallback: {order['offers'][1]['supplier']})" if len(order["offers"]) > 1 else ""
    return (
        ("🔔 Delayed notification (the first alert may not have arrived)\n" if delayed else "")
        + f"New order to verify (#{order['id']})\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']}\n\n"
        "EXPECTED PAYMENT\n"
        f"Amount: {order['price']:.2f} {coin}\n"
        f"Coin / network: {coin} on {network}\n"
        f"Paid to: {order.get('pay_to') or '(not recorded)'}\n\n"
        "SUBMITTED PROOF\n"
        f"{proof_label}: {order['reference']}\n\n"
        f"Check that exactly {order['price']:.2f} {coin} arrived on {network} at the address above, "
        "and that this proof is that payment, before you tap Approve.\n"
        f"Source: {best['supplier']} at cost {best['cost']:.2f}{fallback}"
        + STOCK_NOTES.get(order.get("stock_reservation"), "")
    )


NOTIFY_RETRY_SECONDS = 60
UNDELIVERED_MIN_AGE_SECONDS = 120  # let the approving handler finish its own delivery first
MAX_AUTO_DELIVERY_ATTEMPTS = 5


async def deliver_credentials(bot: Bot, order: dict, credentials: list[str]) -> bool:
    """Send the purchased credentials to the customer. True only if Telegram accepted it."""
    text = truncate_utf16(f"🎉 Your order: {order['name']}\n\n" + "\n".join(credentials), TELEGRAM_MAX_UNITS)
    try:
        await bot.send_message(order["user_id"], text)
        return True
    except Exception as exc:
        logging.error(
            "Could not deliver order %s to user %s (%s)", order["id"], order["user_id"], type(exc).__name__
        )
        return False


async def redeliver_undelivered(bot: Bot) -> None:
    """Recovery path for `purchased_undelivered` orders: their credentials are already
    stored (encrypted), so just retry delivery. Repeated deliveries are possible after a
    crash between sending and marking delivered; losing the credentials is not."""
    try:
        due = await storage.undelivered_orders(UNDELIVERED_MIN_AGE_SECONDS, MAX_AUTO_DELIVERY_ATTEMPTS)
    except PyMongoError as exc:
        log_db_error("loading undelivered orders", exc)
        return
    for order in due:
        with order_context(order["id"]):
            await _redeliver_one(bot, order)


async def _redeliver_one(bot: Bot, order: dict) -> None:
    try:
        credentials = credbox.decrypt_list(order["credentials_enc"])
    except (CredentialDecryptError, KeyError):
        logging.error("Order %s: undelivered credentials could not be decrypted", order["id"])
        return
    if await deliver_credentials(bot, order, credentials):
        try:
            await storage.mark_delivered(order["id"])
        except PyMongoError as exc:
            log_db_error("marking order delivered", exc, order=order["id"])
        return
    try:
        attempts = await storage.record_delivery_failure(order["id"])
    except PyMongoError as exc:
        log_db_error("recording delivery failure", exc, order=order["id"])
        return
    if attempts >= MAX_AUTO_DELIVERY_ATTEMPTS:
        await alert_admin(
            bot,
            f"🚨 Order #{order['id']}: automatic delivery failed {attempts} times and has stopped. "
            f"The customer may have blocked the bot.\nUser: {order['user_label']}\n"
            "Forward the credentials manually (the order stays purchased_undelivered):\n\n"
            + "\n".join(credentials),
            protect_content=True,
            attempts=5,
            order_id=order["id"],
        )


async def notify_pending_orders(bot: Bot) -> None:
    """Send (or re-send) the new-order alert for saved pending orders whose alert is not
    confirmed delivered. Safe to repeat: the worst case is a duplicate alert."""
    try:
        unnotified = await storage.pending_unnotified()
    except PyMongoError as exc:
        log_db_error("loading unnotified orders", exc)
        return
    for order in unnotified:
        with order_context(order["id"]):
            delivered = await alert_admin(
                bot,
                new_order_alert(order, delayed=True),
                reply_markup=admin_keyboard(order["id"]),
                order_id=order["id"],
            )
            if not delivered:
                return  # Telegram is still unavailable: retry the whole batch next round
            try:
                await storage.mark_admin_notified(order["id"])
            except PyMongoError as exc:
                log_db_error("recording admin notification", exc, order=order["id"])


async def notification_retry_loop(bot: Bot) -> None:
    while True:
        for background_pass in (notify_pending_orders, redeliver_undelivered, flush_outbox):
            try:
                await background_pass(bot)
            except Exception as exc:  # never let the loop die
                logging.error("Background pass %s failed (%s)", background_pass.__name__, type(exc).__name__)
        await asyncio.sleep(NOTIFY_RETRY_SECONDS)


USER_ERROR_MESSAGE = "Sorry, something went wrong on our side. Please try again in a moment."
ERROR_ALERT_INTERVAL = 60  # seconds between admin alerts for the same kind of error
_last_error_alert: dict[str, float] = {}


def install_fsm_storage(db_storage: Storage) -> None:
    """Keep conversation state (checkout, search, unlock flows) in MongoDB instead of memory,
    so it survives a restart."""
    dp.fsm.storage = MongoFSMStorage(db_storage.fsm_collection)


UNBLOCK_USAGE = (
    "Usage: /unblock <payment proof> <reason>\n"
    "Frees the payment proof (TXID / Binance order ID) of a REJECTED order so it can be used "
    "again. A reason of 3-200 characters is required and is written to the audit log."
)


@dp.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject):
    """Admin only (silently ignored for everyone else): unblock a rejected payment proof."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split(None, 1)
    reason = " ".join(parts[1].split()) if len(parts) == 2 else ""
    if not 3 <= len(reason) <= 200:
        await message.answer(UNBLOCK_USAGE)
        return
    try:
        code, order = await storage.unblock_reference(parts[0], message.from_user.id, reason)
    except ValueError:
        await message.answer(UNBLOCK_USAGE)
        return
    except PyMongoError as exc:
        log_db_error("unblocking a payment proof", exc, user=message.from_user.id)
        await message.answer("Database error - nothing was changed. Try again.")
        return
    if code == "not_found":
        await message.answer("No order uses that payment proof.")
    elif code == "not_rejected":
        await message.answer(
            f"That proof belongs to order #{order['id']}, which is {order['status']}, not rejected. "
            "Only a rejected order's proof can be unblocked. Nothing was changed."
        )
    else:
        with order_context(order["id"]):
            logging.warning("Admin %s unblocked the payment proof of rejected order %s", message.from_user.id, order["id"])
        await message.answer(
            f"✅ The payment proof of rejected order #{order['id']} ({order['user_label']}) can be used again. "
            "The change was recorded in the audit log."
        )


def update_kind(update) -> str:
    for kind in ("message", "callback_query"):
        if getattr(update, kind, None) is not None:
            return kind
    return "update"


@dp.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Last line of defence for every handler: log the full traceback, tell the user
    something neutral, and send the admin a SHORT alert.

    The alert carries only the exception type and the update kind/id: no message text, no
    exception message (it can contain data) and nothing from the order. Alerts for the same
    exception type are throttled so a failing handler cannot flood the admin chat."""
    exc, update = event.exception, event.update
    kind = update_kind(update)
    logging.error("Unhandled %s in a %s handler", type(exc).__name__, kind, exc_info=exc)

    try:
        if getattr(update, "callback_query", None) is not None:
            await update.callback_query.answer(USER_ERROR_MESSAGE, show_alert=True)
        elif getattr(update, "message", None) is not None:
            await update.message.answer(USER_ERROR_MESSAGE)
    except Exception as notify_exc:
        logging.error("Could not send the error notice to the user (%s)", type(notify_exc).__name__)

    error_name = type(exc).__name__
    now = time.monotonic()
    if now - _last_error_alert.get(error_name, float("-inf")) >= ERROR_ALERT_INTERVAL:
        _last_error_alert[error_name] = now
        await alert_admin(
            bot,
            f"⚠️ Unhandled {error_name} while handling a {kind} (update {getattr(update, 'update_id', '?')}). "
            "The full traceback is in the logs.",
        )
    return True


async def start_health_server() -> web.AppRunner:
    """Tiny HTTP server so hosts like Render see an open port and a healthy service."""
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="Bot is running", status=200)

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Health server listening on port %s", port)
    return runner


async def main():
    configure_logging()
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    if CANBOSO_ENABLED and not CANBOSO_PURCHASE_IMPLEMENTED:
        raise SystemExit(
            "CANBOSO_ENABLED is set but Canboso purchases are not implemented yet; unset it."
        )
    if not (supplier_is_live("pandora") or supplier_is_live("canboso")):
        raise SystemExit("No live supplier: set SUPPLIER_2_API_KEY (Pandora).")
    try:
        apply_admin_chat_id()
    except ValueError as exc:
        raise SystemExit(str(exc))

    global storage, credbox
    try:
        credbox = CredentialBox.from_env()
    except CredentialKeyError as exc:
        raise SystemExit(f"{exc}. Generate one with the command in .env.example.")
    try:
        storage = Storage.from_env()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    try:
        await storage.connect()
        report = await storage.ensure_indexes()
        prefs.update(await storage.all_langs())
        install_fsm_storage(storage)
    except PyMongoError as exc:
        await storage.close()
        # Log the exception type only; driver messages can echo connection details.
        raise SystemExit(f"Could not initialize MongoDB ({type(exc).__name__}).")

    bot = None
    try:
        bot = Bot(token=BOT_TOKEN)

        if report.has_problems:
            affected = sorted(
                {oid for ids in report.duplicate_references.values() for oid in ids}
                | set(report.missing_reference_lower)
                | set(report.stale_reference_lower)
                | set(report.invalid_reference)
            )
            raise SystemExit(
                "Order data needs attention before the bot can start. Affected order IDs: "
                + ", ".join(affected)
            )

        await bot.set_my_commands(BOT_COMMANDS)

        # Orders caught mid-purchase by a restart: alert the admin; never contact a supplier here.
        try:
            recovered = await storage.recover_interrupted_orders()
        except PyMongoError as exc:
            raise SystemExit(f"Could not recover interrupted orders ({type(exc).__name__}).")
        for order in recovered:
            with order_context(order["id"]):
                logging.warning("Recovered interrupted order %s into reconciliation", order["id"])
        # Their alerts and customer notices are owed on the orders themselves; send them now,
        # and the background loop keeps retrying (with backoff) until Telegram confirms.
        await flush_outbox(bot)

        runner = await start_health_server()
        # Re-send any new-order alert that was never confirmed delivered (including after a restart).
        retry_task = asyncio.create_task(notification_retry_loop(bot))
        try:
            await dp.start_polling(bot)
        finally:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
            await runner.cleanup()
    finally:
        try:
            if bot is not None:
                # Every way out (SystemExit included) must release the HTTP session; polling's own
                # cleanup is never reached when startup stops early. Closing twice is harmless.
                await bot.session.close()
        finally:
            await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import hashlib
import logging
import math
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass

import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError, PyMongoError

from storage import Storage

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Supplier 1 (Canboso): X-API-Key header.
SUPPLIER_API_KEY = os.getenv("SUPPLIER_API_KEY")
CANBOSO_PRODUCTS_URL = "https://canboso.com/api/v2/telegram-buyer/products"

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
        "destination": os.getenv("BINANCE_PAY_ID"),
        "dest_label": "Binance Pay ID / Email",
        "ref_label": "Binance Order ID",
    },
    "bep20": {
        "label": "BEP20 (USDT)",
        "destination": os.getenv("BEP20_ADDRESS"),
        "dest_label": "BEP20 (BNB Smart Chain) USDT address",
        "ref_label": "transaction hash (TXID)",
    },
    "trc20": {
        "label": "TRC20 (USDT)",
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
                "offer": Offer("pandora", p["id"], p.get("variant_id"), cost),
            })
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return items


_catalog_cache: tuple[float, dict[str, Listing]] | None = None


def build_catalog() -> dict[str, Listing]:
    """Query both suppliers, merge matching products, cheapest offer first."""
    raw: list[dict] = []
    for name, fetch, configured in (
        ("canboso", fetch_canboso, SUPPLIER_API_KEY),
        ("pandora", fetch_pandora, SUPPLIER_2_API_KEY),
    ):
        if not configured:
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

def purchase_canboso(offer: Offer, order_id: str, max_cost: float, fence) -> list[str]:
    # Canboso's order-creation API isn't documented yet; fail over to Pandora.
    raise SupplierError("Canboso automatic purchase is not implemented")


def purchase_pandora(offer: Offer, order_id: str, max_cost: float, fence) -> list[str]:
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
        raise SupplierError(f"Pandora price rose to {unit_price}, above the {max_cost:.2f} cost ceiling")

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
            time.sleep(min(int(response.headers.get("Retry-After", "5")), 30))
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
        return [str(i) for i in items]
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
    # A new attempt (after a definitive failure) needs a fresh idempotency key.
    purchase_ref = purchase_ref_for(order)
    for raw in order["offers"]:
        offer = Offer(**raw)
        try:
            return PURCHASERS[offer.supplier](offer, purchase_ref, max_cost, fence), offer, failures
        except SupplierError as exc:
            failures.append(f"{offer.supplier}: {exc}")
            logging.warning("Order %s: %s failed: %s", order["id"], offer.supplier, exc)
    raise SupplierError("; ".join(failures) or "no offers")


# --------------------------------------------------------------------------
# Order persistence (MongoDB via storage.py)
# --------------------------------------------------------------------------

RETRY_LATER = (
    "Sorry, something went wrong on our side and your order was not saved. "
    "Please send your reference again in a moment."
)
DUPLICATE_REFERENCE = "That reference has already been submitted. Contact support if this is a mistake."
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


async def persist_order(order_id: str, fields: dict, expected: str | list[str], attempts: int = 3) -> str:
    """Write order fields only while the order still has an `expected` status.

    Returns SAVED, STALE (the order is no longer in an expected status, so nothing
    was written), or FAILED (the database errored on every attempt). Only SAVED
    means the write took effect.
    """
    had_error = False
    for attempt in range(attempts):
        try:
            matched = await storage.update_order(order_id, fields, only_if_status=expected)
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
            "Order %s not updated: no longer in expected status %s (fields %s)",
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


TELEGRAM_CHUNK = 4000  # Telegram caps a message at 4096 characters


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
    chunks = [text[i:i + TELEGRAM_CHUNK] for i in range(0, len(text), TELEGRAM_CHUNK)] or [text]
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
        if order["status"] == "delivered" and order.get("credentials"):
            block += "\nYour details:\n" + "\n".join(order["credentials"])
        blocks.append(block)
    return ("Your recent orders:\n\n" + "\n\n".join(blocks))[:4096]


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
        lines.append(listing.description[:3000])  # Telegram caps messages at 4096 chars.

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

    price = retail_price(listing.cost)
    await state.set_state(Checkout.waiting_reference)
    await state.update_data(
        name=listing.name,
        price=price,
        method=method_key,
        offers=[asdict(o) for o in listing.offers],
    )

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel_order"))
    await callback.message.answer(
        f"Order: {listing.name}\n"
        f"Amount: {price:.2f} USDT\n"
        f"Method: {method['label']}\n\n"
        f"Send exactly this amount to the {method['dest_label']}:\n{method['destination']}\n\n"
        f"After paying, reply here with the {method['ref_label']}.",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "cancel_order")
async def cancel_order(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Order cancelled")
    await send_menu(callback.message, callback.from_user.id)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await send_menu(message, message.from_user.id)


# Registered after /cancel, and commands are excluded so they never reach the validator.
@dp.message(Checkout.waiting_reference, F.text, ~F.text.startswith("/"))
async def receive_reference(message: Message, state: FSMContext):
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
    order = {
        "id": order_id,
        "status": "pending",
        "user_id": user.id,
        "user_label": f"{user.full_name} (@{user.username or '-'}, id {user.id})",
        "name": data["name"],
        "price": data["price"],
        "method": data["method"],
        "reference": reference,
        "offers": data["offers"],
        "admin_notified": False,  # durable outbox marker: set True only once Telegram confirms the alert
    }
    # Persist before confirming; keep the checkout state if this fails so the user can resend.
    try:
        await storage.insert_order(order)
    except DuplicateKeyError:  # lost a race with another submission of the same reference
        await message.answer(DUPLICATE_REFERENCE)
        return
    except PyMongoError as exc:
        log_db_error("saving new order", exc, order=order_id, user=user.id)
        await message.answer(RETRY_LATER)
        return

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
        delivered_fields = {
            "status": "delivered",
            "outcome": "purchased",
            "supplier": offer.supplier,
            "actual_cost": offer.cost,
            "credentials": credentials,  # kept so "My Orders" can show them later
        }
        result = await persist_order(order_id, delivered_fields, "fulfilling")
        if result == STALE:
            # Something else moved the order mid-purchase (e.g. a restart recovered it to
            # reconcile). Complete it only from reconcile; never overwrite a newer status.
            result = await persist_order(order_id, delivered_fields, "reconcile")
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
                )
                == SAVED
            )

        delivered = True
        try:
            await bot.send_message(
                order["user_id"], (f"🎉 Your order: {order['name']}\n\n" + "\n".join(credentials))[:4096]
            )
        except Exception:
            delivered = False
            logging.error("Could not deliver order %s to user %s", order_id, order["user_id"])

        status_update_failed = False
        if not delivered and saved:
            status_update_failed = await persist_order(order_id, {"status": "delivery_failed"}, "delivered") != SAVED

        if not saved or not delivered:
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
            if not delivered:
                problems.append("Delivery to the customer failed; forward the credentials manually.")
            if status_update_failed:
                problems.append("Its status could not be updated to delivery_failed; check the order state.")
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
                # delivery_failed) and is flagged in the log and on the admin's approval message.
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


async def hold_order(bot: Bot, order: dict, reason: str, *, attempt: int | None = None) -> None:
    """Park an order after a PROVEN no-purchase failure; ordinary Retry/Reject apply."""
    fields = {"status": "held", "outcome": "definitive_failure", "error": reason}
    if attempt is not None:
        fields["attempt"] = attempt
    result = await persist_order(order["id"], fields, "fulfilling")
    logging.error("Order %s held: %s", order["id"], reason)

    summary = (
        f"🚨 EMERGENCY: order #{order['id']} is on hold\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
        f"Reason: {reason}\n\n"
        "The customer has paid but has not received the product. "
    )
    if result == SAVED:
        await alert_admin(
            bot,
            summary + "No purchase was made. Fix the issue and tap Retry, or Reject to cancel.",
            reply_markup=admin_keyboard(order["id"], retry=True),
            order_id=order["id"],
        )
    else:
        await alert_admin(bot, summary + unsaved_note(result), order_id=order["id"])
    await notify_delay(bot, order)


async def reconcile_order(bot: Bot, order: dict, reason: str) -> None:
    """Park an order whose supplier outcome is unknown: no Retry or Reject, and no
    attempt change, until an admin verifies and explicitly unlocks it."""
    result = await persist_order(
        order["id"],
        {
            "status": "reconcile",
            "outcome": "unknown",
            "error": reason,
        },
        "fulfilling",
    )
    logging.error("Order %s needs manual reconciliation: %s", order["id"], reason)

    summary = (
        f"🚨 MANUAL RECONCILIATION: order #{order['id']}\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
        f"Supplier client reference: {purchase_ref_for(order)}\n"
        f"Reason: {reason}\n\n"
        "The supplier purchase may or may not have happened. Check the supplier account for the "
        "client reference above. Retry and Reject are blocked. "
    )
    if result == SAVED:
        await alert_admin(
            bot,
            summary + "Retry stays blocked until you confirm with Pandora that no purchase exists and no "
            "request is still pending, and record that confirmation via the button below.",
            reply_markup=reconcile_keyboard(order["id"]),
            order_id=order["id"],
        )
    else:
        await alert_admin(bot, summary + unsaved_note(result), order_id=order["id"])
    await notify_delay(bot, order)


async def notify_delay(bot: Bot, order: dict) -> None:
    try:
        await bot.send_message(
            order["user_id"],
            "Your payment is confirmed, but delivery is delayed. Our team has been notified and will sort it out shortly.",
        )
    except Exception:
        logging.exception("Could not notify user %s", order["user_id"])


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


def new_order_alert(order: dict, delayed: bool = False) -> str:
    """The new-order alert for the admin (the payment reference is needed to verify it;
    it is never logged). Contains no credentials."""
    method = PAYMENT_METHODS.get(order["method"], {})
    best = order["offers"][0]
    fallback = f" (fallback: {order['offers'][1]['supplier']})" if len(order["offers"]) > 1 else ""
    return (
        ("🔔 Delayed notification (the first alert may not have arrived)\n" if delayed else "")
        + f"New order to verify (#{order['id']})\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']}\n"
        f"Amount: {order['price']:.2f} USDT\n"
        f"Method: {method.get('label', order['method'])}\n"
        f"Reference ({method.get('ref_label', 'reference')}): {order['reference']}\n"
        f"Source: {best['supplier']} at cost {best['cost']:.2f}{fallback}"
    )


NOTIFY_RETRY_SECONDS = 60


async def notify_pending_orders(bot: Bot) -> None:
    """Send (or re-send) the new-order alert for saved pending orders whose alert is not
    confirmed delivered. Safe to repeat: the worst case is a duplicate alert."""
    try:
        unnotified = await storage.pending_unnotified()
    except PyMongoError as exc:
        log_db_error("loading unnotified orders", exc)
        return
    for order in unnotified:
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
        try:
            await notify_pending_orders(bot)
        except Exception as exc:  # never let the loop die
            logging.error("Notification retry pass failed (%s)", type(exc).__name__)
        await asyncio.sleep(NOTIFY_RETRY_SECONDS)


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
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    if not SUPPLIER_API_KEY and not SUPPLIER_2_API_KEY:
        raise SystemExit("Set SUPPLIER_API_KEY and/or SUPPLIER_2_API_KEY.")
    if not ADMIN_CHAT_ID:
        raise SystemExit("ADMIN_CHAT_ID environment variable is not set.")

    global storage
    try:
        storage = Storage.from_env()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    try:
        await storage.connect()
        report = await storage.ensure_indexes()
        prefs.update(await storage.all_langs())
    except PyMongoError as exc:
        await storage.close()
        # Log the exception type only; driver messages can echo connection details.
        raise SystemExit(f"Could not initialize MongoDB ({type(exc).__name__}).")

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
            purchased = order.get("outcome") == "purchased"
            await alert_admin(
                bot,
                f"⚠️ Order #{order['id']} was interrupted by a restart and needs manual reconciliation.\n"
                f"User: {order['user_label']}\n"
                f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
                f"Supplier client reference: {purchase_ref_for(order)}\n\n"
                + (
                    "It is recorded as PURCHASED. Never retry or reject it."
                    if purchased
                    else "The supplier purchase may or may not have happened. Check the supplier account "
                    "for the client reference above. Retry and Reject are blocked until Pandora confirms that "
                    "no purchase exists and no request is still pending, and you record that confirmation."
                ),
                reply_markup=None if purchased else reconcile_keyboard(order["id"]),
                order_id=order["id"],
            )

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
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

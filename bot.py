import asyncio
import hashlib
import json
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
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

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
CATALOG_TTL = 30
ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")

dp = Dispatcher()


class Checkout(StatesGroup):
    waiting_reference = State()


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

class SupplierError(Exception):
    """This supplier can't fulfil the order; trying the next one is safe."""


class AmbiguousOrderError(Exception):
    """An order may or may not have been placed; do NOT try another supplier."""


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

def purchase_canboso(offer: Offer, order_id: str, max_cost: float) -> list[str]:
    # Canboso's order-creation API isn't documented yet; fail over to Pandora.
    raise SupplierError("Canboso automatic purchase is not implemented")


def purchase_pandora(offer: Offer, order_id: str, max_cost: float) -> list[str]:
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

    # Quote immediately before ordering (price protection).
    response = call("POST", "/quotes", json=body)
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
    for attempt in range(3):
        try:
            response = call("POST", "/orders", json=order_body, headers=headers)
        except (requests.Timeout, requests.ConnectionError):
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(min(int(response.headers.get("Retry-After", "5")), 30))
            continue
        if response.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if not response.ok:
            raise SupplierError(f"Pandora order rejected: {pandora_error_code(response)}")
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
        raise SupplierError(f"Pandora order failed: {order.get('failure_code')}")
    raise AmbiguousOrderError(f"Pandora order {order.get('id')} in state {order.get('status')}")


PURCHASERS = {"canboso": purchase_canboso, "pandora": purchase_pandora}


def fulfil(order: dict) -> tuple[list[str], Offer, list[str]]:
    """Try offers cheapest-first. Returns (credentials, winning offer, failure log)."""
    failures = []
    # Never buy above the cost the customer's price was derived from (+1 cent rounding).
    max_cost = (order["price"] - MARKUP_FIXED) / (1 + MARKUP_PERCENT / 100) + 0.01
    # A new attempt (after a definitive failure) needs a fresh idempotency key.
    purchase_ref = f"{order['id']}-{order.get('attempt', 0)}"
    for raw in order["offers"]:
        offer = Offer(**raw)
        try:
            return PURCHASERS[offer.supplier](offer, purchase_ref, max_cost), offer, failures
        except SupplierError as exc:
            failures.append(f"{offer.supplier}: {exc}")
            logging.warning("Order %s: %s failed: %s", order["id"], offer.supplier, exc)
    raise SupplierError("; ".join(failures) or "no offers")


# --------------------------------------------------------------------------
# Order storage (credentials are never persisted)
# --------------------------------------------------------------------------

orders: dict[str, dict] = {}
_fulfilling: set[str] = set()


def load_orders() -> None:
    try:
        with open(ORDERS_FILE, encoding="utf-8") as f:
            orders.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logging.exception("Could not read %s", ORDERS_FILE)


def save_orders() -> None:
    tmp = ORDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2)
    os.replace(tmp, ORDERS_FILE)


async def alert_admin(bot: Bot, text: str, reply_markup=None) -> None:
    if not ADMIN_CHAT_ID:
        logging.error("ADMIN_CHAT_ID not set; admin alert dropped: %s", text)
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=reply_markup)
    except Exception:
        logging.exception("Failed to send admin alert")


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_CHAT_ID) and str(user_id) == str(ADMIN_CHAT_ID)


def admin_keyboard(order_id: str, retry: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔁 Retry" if retry else "✅ Approve", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{order_id}"),
    )
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
PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prefs.json")
prefs: dict[str, str] = {}


def load_prefs() -> None:
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            prefs.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logging.exception("Could not read %s", PREFS_FILE)


def save_prefs() -> None:
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except OSError:
        logging.exception("Could not write %s", PREFS_FILE)


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

def render_orders(user_id: int) -> str:
    mine = [o for o in orders.values() if o["user_id"] == user_id]
    if not mine:
        return "You have no past orders yet."

    blocks = []
    for order in reversed(mine[-MY_ORDERS_LIMIT:]):  # newest first
        status = CUSTOMER_STATUS.get(order["status"], "Failed")
        block = f"📦 {order['name']}\nAmount: {order['price']:.2f} USDT\nStatus: {status}"
        if order["status"] == "delivered" and order.get("credentials"):
            block += "\nYour details:\n" + "\n".join(order["credentials"])
        blocks.append(block)
    return ("Your recent orders:\n\n" + "\n\n".join(blocks))[:4096]


@dp.message(Command("orders"))
async def cmd_orders(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(render_orders(message.from_user.id), reply_markup=back_menu(message.from_user.id))


@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    uid = callback.from_user.id
    await callback.message.answer(render_orders(uid), reply_markup=back_menu(uid))


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
    prefs[str(uid)] = lang
    save_prefs()
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
    if any(o["reference"].lower() == reference.lower() for o in orders.values()):
        await message.answer("That reference has already been submitted. Contact support if this is a mistake.")
        return

    await state.clear()

    user = message.from_user
    order_id = uuid.uuid4().hex[:10]
    orders[order_id] = {
        "id": order_id,
        "status": "pending",
        "user_id": user.id,
        "user_label": f"{user.full_name} (@{user.username or '-'}, id {user.id})",
        "name": data["name"],
        "price": data["price"],
        "method": data["method"],
        "reference": reference,
        "offers": data["offers"],
    }
    save_orders()

    await message.answer(
        "Thanks! Your payment is pending verification. "
        "Your order will be delivered here as soon as it is confirmed.",
        reply_markup=main_menu(user.id),
    )

    best = data["offers"][0]
    await alert_admin(
        message.bot,
        f"New order to verify (#{order_id})\n"
        f"User: {orders[order_id]['user_label']}\n"
        f"Product: {data['name']}\n"
        f"Amount: {data['price']:.2f} USDT\n"
        f"Method: {method['label']}\n"
        f"Reference ({method['ref_label']}): {reference}\n"
        f"Source: {best['supplier']} at cost {best['cost']:.2f}"
        + (f" (fallback: {data['offers'][1]['supplier']})" if len(data["offers"]) > 1 else ""),
        reply_markup=admin_keyboard(order_id),
    )



# --------------------------------------------------------------------------
# Admin: approve -> purchase -> deliver
# --------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Not allowed", show_alert=True)
        return
    order = orders.get(callback.data.split(":", 1)[1])
    if order is None or order["status"] not in ("pending", "held") or order["id"] in _fulfilling:
        await callback.answer("This order can't be rejected now.", show_alert=True)
        return
    order["status"] = "rejected"
    save_orders()
    await callback.answer("Rejected")
    await callback.message.edit_text(f"{callback.message.text}\n\n❌ Rejected")
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
    order = orders.get(callback.data.split(":", 1)[1])
    if order is None or order["status"] not in ("pending", "held") or order["id"] in _fulfilling:
        await callback.answer("This order can't be approved now.", show_alert=True)
        return

    _fulfilling.add(order["id"])  # blocks double-taps while the purchase runs
    await callback.answer("Purchasing…")
    bot = callback.bot

    try:
        try:
            await callback.message.edit_text(f"{callback.message.text}\n\n⏳ Approved - purchasing…")
        except Exception:
            logging.exception("Could not edit admin message")
        try:
            await bot.send_message(order["user_id"], "Payment confirmed ✅ Preparing your order…")
        except Exception:
            logging.exception("Could not notify user %s", order["user_id"])

        try:
            credentials, offer, failures = await asyncio.to_thread(fulfil, order)
        except SupplierError as exc:
            order["attempt"] = order.get("attempt", 0) + 1
            await hold_order(bot, order, f"All suppliers failed: {exc}")
            return
        except AmbiguousOrderError as exc:
            await hold_order(
                bot, order, f"Purchase outcome unknown, NOT retrying another supplier: {exc}"
            )
            return
        except Exception as exc:
            logging.exception("Unhandled error fulfilling order %s", order["id"])
            await hold_order(bot, order, f"Unhandled error: {exc!r}")
            return

        order["status"] = "delivered"
        order["supplier"] = offer.supplier
        order["actual_cost"] = offer.cost
        order["credentials"] = credentials  # kept so "My Orders" can show them later
        save_orders()

        text = f"🎉 Your order: {order['name']}\n\n" + "\n".join(credentials)
        try:
            await bot.send_message(order["user_id"], text[:4096])
        except Exception:
            # Don't lose paid-for credentials: hand them to the admin.
            logging.error("Could not deliver order %s to user %s", order["id"], order["user_id"])
            order["status"] = "delivery_failed"
            order["credentials"] = credentials
            save_orders()
            await alert_admin(
                bot,
                f"🚨 Order #{order['id']} was purchased but delivery to the user failed.\n"
                f"User: {order['user_label']}\nForward these manually:\n\n" + "\n".join(credentials),
            )
            return

        note = f" (fallbacks: {'; '.join(failures)})" if failures else ""
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ Delivered via {offer.supplier}{note}"
        )
    finally:
        _fulfilling.discard(order["id"])


async def hold_order(bot: Bot, order: dict, reason: str) -> None:
    order["status"] = "held"
    order["error"] = reason
    save_orders()
    logging.error("Order %s held: %s", order["id"], reason)
    await alert_admin(
        bot,
        f"🚨 EMERGENCY: order #{order['id']} is on hold\n"
        f"User: {order['user_label']}\n"
        f"Product: {order['name']} ({order['price']:.2f} USDT)\n"
        f"Reason: {reason}\n\n"
        "The customer has paid but has not received the product. "
        "Fix the issue and tap Retry, or Reject to cancel.",
        reply_markup=admin_keyboard(order["id"], retry=True),
    )
    try:
        await bot.send_message(
            order["user_id"],
            "Your payment is confirmed, but delivery is delayed. Our team has been notified and will sort it out shortly.",
        )
    except Exception:
        logging.exception("Could not notify user %s", order["user_id"])


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

    load_orders()
    load_prefs()
    bot = Bot(token=BOT_TOKEN)
    await bot.set_my_commands(BOT_COMMANDS)
    runner = await start_health_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

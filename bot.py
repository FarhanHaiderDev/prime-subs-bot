import asyncio
import logging
import os

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPPLIER_API_KEY = os.getenv("SUPPLIER_API_KEY")
PRODUCTS_URL = "https://canboso.com/api/v2/telegram-buyer/products"

PAGE_SIZE = 8
REQUEST_TIMEOUT = 15

dp = Dispatcher()


def fetch_products() -> list[dict]:
    """Blocking call to the supplier API; run it via asyncio.to_thread."""
    response = requests.get(
        PRODUCTS_URL,
        headers={"X-API-Key": SUPPLIER_API_KEY},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError(f"Supplier API reported failure: {data}")
    return data.get("products", [])


def product_title(product: dict) -> str:
    return str(product.get("name") or f"Product {product.get('productId')}")


def main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🛍 Browse products", callback_data="browse:0"))
    return builder.as_markup()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Welcome! Tap the button below to browse available products.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data.startswith("browse:"))
async def browse_products(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.answer()

    try:
        products = await asyncio.to_thread(fetch_products)
    except (requests.RequestException, ValueError):
        logging.exception("Failed to fetch products")
        await callback.message.edit_text(
            "Sorry, I couldn't load products right now. Please try again later.",
            reply_markup=main_menu(),
        )
        return

    if not products:
        await callback.message.edit_text("No products available.", reply_markup=main_menu())
        return

    start = page * PAGE_SIZE
    chunk = products[start:start + PAGE_SIZE]

    builder = InlineKeyboardBuilder()
    for product in chunk:
        builder.row(
            InlineKeyboardButton(
                text=product_title(product)[:60],
                callback_data=f"product:{product['productId']}",
            )
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"browse:{page - 1}"))
    if start + PAGE_SIZE < len(products):
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"browse:{page + 1}"))
    if nav:
        builder.row(*nav)

    await callback.message.edit_text(
        f"Products (page {page + 1}):", reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("product:"))
async def show_product(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    await callback.answer()

    try:
        products = await asyncio.to_thread(fetch_products)
    except (requests.RequestException, ValueError):
        logging.exception("Failed to fetch products")
        await callback.message.answer("Sorry, I couldn't load that product. Please try again.")
        return

    product = next((p for p in products if p.get("productId") == product_id), None)
    if product is None:
        await callback.message.answer("That product is no longer available.")
        return

    lines = [product_title(product)]
    price = product.get("price") or {}
    if price.get("text"):
        lines.append(f"Price: {price['text']}")
    availability = product.get("availability") or {}
    lines.append(f"In stock: {availability.get('available', 0)} (sold: {availability.get('sold', 0)})")
    if product.get("description"):
        # Telegram messages are capped at 4096 chars.
        lines.append(str(product["description"])[:3000])

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Back to products", callback_data="browse:0"))
    await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")
    if not SUPPLIER_API_KEY:
        raise SystemExit("SUPPLIER_API_KEY environment variable is not set.")

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""Fix 5: the admin approval message shows the expected amount, the coin/network and the
submitted proof, so the payment can be verified before approving."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
from conftest import FakeBot, TXID, new_order

CASES = [
    ("trc20", "TRC20 (TRON)", "TTestTronAddress", "transaction hash (TXID)", TXID),
    ("bep20", "BEP20 (BNB Smart Chain)", "0xTestBscAddress", "transaction hash (TXID)", "0x" + TXID),
    ("binance", "Binance Pay", "TestBinanceId", "Binance Order ID", "123456789012345678"),
]


@pytest.mark.parametrize("method,network,pay_to,proof_label,proof", CASES)
def test_alert_shows_amount_network_destination_and_proof(method, network, pay_to, proof_label, proof):
    order = new_order("ord1", method=method, reference=proof, pay_to=pay_to, price=12.6)

    text = bot.new_order_alert(order)

    assert "Amount: 12.60 USDT" in text
    assert f"Coin / network: USDT on {network}" in text
    assert f"Paid to: {pay_to}" in text
    assert f"{proof_label}: {proof}" in text
    assert "before you tap Approve" in text


def test_delayed_alert_keeps_all_the_verification_details():
    order = new_order("ord1", method="trc20", pay_to="TTestTronAddress")

    text = bot.new_order_alert(order, delayed=True)

    assert "Delayed notification" in text
    assert "Amount: 4.20 USDT" in text and "TRC20 (TRON)" in text and TXID in text


def test_alert_never_contains_credentials_or_supplier_secrets():
    text = bot.new_order_alert(new_order("ord1", pay_to="TTestTronAddress"))

    assert "credentials" not in text.lower()
    assert "test-pandora-key" not in text


async def test_submitted_order_alert_reaches_the_admin_with_an_approve_button(storage):
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "method": "bep20",
                "name": "Netflix 1M",
                "price": 4.2,
                "offers": [{"supplier": "pandora", "product_id": "p1", "variant_id": None, "cost": 3.0}],
            }
        ),
        clear=AsyncMock(),
    )
    fake_bot = FakeBot()
    message = SimpleNamespace(
        text="0x" + TXID,
        from_user=SimpleNamespace(id=777, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=fake_bot,
    )

    await bot.receive_reference(message, state)

    (admin_chat, text, kwargs), = [m for m in fake_bot.sent if str(m[0]) == "111"]
    assert "Amount: 4.20 USDT" in text
    assert "BEP20 (BNB Smart Chain)" in text
    assert "0xTestBscAddress" in text  # where the customer was told to pay
    assert "0x" + TXID in text  # the proof as submitted
    callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(data.startswith("approve:") for data in callbacks)
    order = await storage.get_order((await storage._orders.find_one({}))["_id"])
    assert order["pay_to"] == "0xTestBscAddress"


async def test_alert_uses_the_destination_the_customer_was_actually_given(storage, monkeypatch):
    order = new_order("ord1", pay_to="TOldAddress")
    # the configured address changes after the customer paid the old one
    monkeypatch.setitem(bot.PAYMENT_METHODS["trc20"], "destination", "TNewAddress")

    text = bot.new_order_alert(order)

    assert "Paid to: TOldAddress" in text
    assert "TNewAddress" not in text

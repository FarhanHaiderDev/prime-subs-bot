"""Fix 3: credentials are stored encrypted BEFORE delivery, with an explicit
purchased_undelivered state and an automatic recovery path."""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

import bot
from conftest import CUSTOMER_ID, FakeBot, make_callback, new_order
from credbox import CredentialBox, CredentialDecryptError, CredentialKeyError

SECRET = "user:pass"


async def _age_purchase(storage, order_id, minutes=10):
    await storage._orders.update_one(
        {"_id": order_id},
        {"$set": {"purchased_at": datetime.now(timezone.utc) - timedelta(minutes=minutes)}},
    )


# ---- encryption ------------------------------------------------------------------------

def test_roundtrip_and_token_is_not_plaintext():
    box = CredentialBox(Fernet.generate_key().decode())
    token = box.encrypt_list([SECRET, "second"])

    assert SECRET not in token
    assert box.decrypt_list(token) == [SECRET, "second"]


def test_wrong_key_cannot_decrypt():
    token = CredentialBox(Fernet.generate_key().decode()).encrypt_list([SECRET])
    other = CredentialBox(Fernet.generate_key().decode())

    with pytest.raises(CredentialDecryptError):
        other.decrypt_list(token)


def test_key_rotation_new_key_first_old_key_still_decrypts():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    token = CredentialBox(old_key).encrypt_list([SECRET])

    rotated = CredentialBox(f"{new_key},{old_key}")

    assert rotated.decrypt_list(token) == [SECRET]
    assert CredentialBox(new_key).decrypt_list(rotated.encrypt_list([SECRET])) == [SECRET]


def test_missing_or_invalid_key_is_rejected(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(CredentialKeyError):
        CredentialBox.from_env()
    with pytest.raises(CredentialKeyError):
        CredentialBox("not-a-fernet-key")


# ---- persisted before delivery ----------------------------------------------------------------

async def test_credentials_are_stored_encrypted_before_delivery(storage, pandora, credential_box):
    await storage.insert_order(new_order("ord1"))
    seen_at_send_time = []

    async def record_db_state(chat_id, text):
        if str(chat_id) == str(CUSTOMER_ID) and SECRET in text:  # the credentials message only
            seen_at_send_time.append(await storage.get_order("ord1"))

    callback = make_callback("approve:ord1", fake_bot=FakeBot(on_send=record_db_state))

    await bot.approve_order(callback)

    (before_delivery,) = seen_at_send_time
    assert before_delivery["status"] == "purchased_undelivered"
    assert before_delivery["outcome"] == "purchased"
    assert credential_box.decrypt_list(before_delivery["credentials_enc"]) == [SECRET]
    assert "credentials" not in before_delivery  # never plaintext
    assert SECRET not in str(before_delivery)
    final = await storage.get_order("ord1")
    assert final["status"] == "delivered"
    assert any(SECRET in text for text in callback.bot.texts_to(CUSTOMER_ID))


async def test_failed_delivery_keeps_credentials_in_purchased_undelivered(storage, pandora, credential_box):
    await storage.insert_order(new_order("ord2"))
    callback = make_callback("approve:ord2", fake_bot=FakeBot(fail_for=[CUSTOMER_ID]))

    await bot.approve_order(callback)

    order = await storage.get_order("ord2")
    assert order["status"] == "purchased_undelivered"
    assert credential_box.decrypt_list(order["credentials_enc"]) == [SECRET]
    # blocked from retry/reject: it is already purchased
    assert await storage.claim_order("ord2") is None
    assert await storage.reject_order("ord2") is None
    # the admin is alerted with the credentials so they can be forwarded by hand
    admin_alerts = [(t, kw) for cid, t, kw in callback.bot.sent if str(cid) == "111" and SECRET in t]
    assert admin_alerts and admin_alerts[0][1].get("protect_content") is True


# ---- recovery path ----------------------------------------------------------------------

async def _undelivered_order(storage, pandora, fake_bot, order_id="ord3"):
    await storage.insert_order(new_order(order_id))
    await bot.approve_order(make_callback(f"approve:{order_id}", fake_bot=fake_bot))
    assert (await storage.get_order(order_id))["status"] == "purchased_undelivered"


async def test_redelivery_delivers_and_marks_delivered(storage, pandora):
    await _undelivered_order(storage, pandora, FakeBot(fail_for=[CUSTOMER_ID]))
    await _age_purchase(storage, "ord3")
    recovery_bot = FakeBot()

    await bot.redeliver_undelivered(recovery_bot)

    order = await storage.get_order("ord3")
    assert order["status"] == "delivered"
    assert order["delivered_at"]
    assert SECRET in recovery_bot.texts_to(CUSTOMER_ID)[0]
    # nothing left to redeliver
    again = FakeBot()
    await bot.redeliver_undelivered(again)
    assert again.attempts == {}


async def test_redelivery_leaves_a_fresh_purchase_to_the_approving_handler(storage, pandora):
    await _undelivered_order(storage, pandora, FakeBot(fail_for=[CUSTOMER_ID]))  # purchased just now
    recovery_bot = FakeBot()

    await bot.redeliver_undelivered(recovery_bot)

    assert recovery_bot.attempts == {}
    assert (await storage.get_order("ord3"))["status"] == "purchased_undelivered"


async def test_redelivery_stops_and_alerts_the_admin_after_repeated_failures(storage, pandora):
    await _undelivered_order(storage, pandora, FakeBot(fail_for=[CUSTOMER_ID]))
    await _age_purchase(storage, "ord3")
    blocked = FakeBot(fail_for=[CUSTOMER_ID])

    for _ in range(bot.MAX_AUTO_DELIVERY_ATTEMPTS + 2):
        await bot.redeliver_undelivered(blocked)

    assert blocked.attempts[str(CUSTOMER_ID)] == bot.MAX_AUTO_DELIVERY_ATTEMPTS  # then it stops retrying
    order = await storage.get_order("ord3")
    assert order["status"] == "purchased_undelivered"
    assert order["delivery_attempts"] == bot.MAX_AUTO_DELIVERY_ATTEMPTS
    assert any(SECRET in text for text in blocked.texts_to("111"))  # admin can forward manually


# ---- My Orders ------------------------------------------------------------------------------

async def test_my_orders_decrypts_credentials_of_delivered_orders(storage, pandora):
    await storage.insert_order(new_order("ord4"))
    await bot.approve_order(make_callback("approve:ord4"))

    text = await bot.render_orders(CUSTOMER_ID)

    assert "Status: Delivered" in text
    assert SECRET in text


async def test_my_orders_does_not_show_credentials_before_delivery(storage, pandora):
    await _undelivered_order(storage, pandora, FakeBot(fail_for=[CUSTOMER_ID]))

    text = await bot.render_orders(CUSTOMER_ID)

    assert "Status: Processing" in text
    assert SECRET not in text


async def test_my_orders_still_reads_legacy_plaintext_credentials(storage):
    await storage.insert_order(new_order("old1", status="delivered", credentials=["legacy:cred"]))

    assert "legacy:cred" in await bot.render_orders(CUSTOMER_ID)

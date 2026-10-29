"""Item 2: the global error handler logs the traceback, tells the user something neutral
and alerts the admin briefly, without leaking secrets or credentials."""

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, MessageEntity, Update, User

import bot
from conftest import FakeBot

TOKEN_LIKE = "123456789:AAFakeTokenFakeTokenFakeTokenFakeToke"  # not a real token


@pytest.fixture(autouse=True)
def fresh_throttle():
    bot._last_error_alert.clear()
    yield
    bot._last_error_alert.clear()


def _message_update(update_id=5):
    message = SimpleNamespace(answer=AsyncMock())
    return SimpleNamespace(update_id=update_id, message=message, callback_query=None), message


def _callback_update(update_id=6):
    callback = SimpleNamespace(answer=AsyncMock())
    return SimpleNamespace(update_id=update_id, message=None, callback_query=callback), callback


def _raised(exc):
    """An exception that really was raised, so it carries a traceback like in production."""
    try:
        raise exc
    except Exception as caught:
        return caught


def _admin_alerts(fake_bot):
    return [text for chat_id, text, _ in fake_bot.sent if str(chat_id) == "111"]


async def test_message_error_gets_a_neutral_reply_and_a_short_admin_alert(caplog):
    update, message = _message_update()
    fake_bot = FakeBot()

    with caplog.at_level(logging.ERROR):
        handled = await bot.on_error(
            SimpleNamespace(update=update, exception=_raised(RuntimeError("boom"))), fake_bot
        )

    assert handled is True
    message.answer.assert_awaited_once_with(bot.USER_ERROR_MESSAGE)
    (alert,) = _admin_alerts(fake_bot)
    assert "RuntimeError" in alert and "message" in alert and "update 5" in alert
    assert len(alert) < 200
    # the full traceback is in the logs
    record = next(r for r in caplog.records if "Unhandled RuntimeError" in r.getMessage())
    assert record.exc_info and record.exc_info[1].args == ("boom",)
    assert "Traceback" in logging.Formatter().format(record)


async def test_callback_error_answers_the_button_press():
    update, callback = _callback_update()

    await bot.on_error(SimpleNamespace(update=update, exception=ValueError("x")), FakeBot())

    callback.answer.assert_awaited_once_with(bot.USER_ERROR_MESSAGE, show_alert=True)


async def test_exception_text_never_reaches_the_user_or_the_admin_alert():
    update, message = _message_update()
    fake_bot = FakeBot()
    secret = f"bot token {TOKEN_LIKE} and credentials user:pass"

    await bot.on_error(SimpleNamespace(update=update, exception=RuntimeError(secret)), fake_bot)

    (user_text,) = message.answer.await_args.args
    (alert,) = _admin_alerts(fake_bot)
    for visible in (user_text, alert):
        assert TOKEN_LIKE not in visible and "user:pass" not in visible and "credentials" not in visible


async def test_repeated_errors_of_one_type_alert_the_admin_only_once():
    fake_bot = FakeBot()
    for update_id in (1, 2, 3):
        update, _ = _message_update(update_id)
        await bot.on_error(SimpleNamespace(update=update, exception=RuntimeError("again")), fake_bot)
    update, _ = _message_update(4)
    await bot.on_error(SimpleNamespace(update=update, exception=KeyError("other")), fake_bot)

    alerts = _admin_alerts(fake_bot)
    assert len(alerts) == 2  # one RuntimeError, one KeyError
    assert any("RuntimeError" in a for a in alerts) and any("KeyError" in a for a in alerts)


async def test_a_failing_notice_does_not_break_the_handler(caplog):
    update, message = _message_update()
    message.answer.side_effect = RuntimeError("user blocked the bot")
    fake_bot = FakeBot()

    with caplog.at_level(logging.ERROR):
        handled = await bot.on_error(SimpleNamespace(update=update, exception=RuntimeError("boom")), fake_bot)

    assert handled is True
    assert _admin_alerts(fake_bot)  # the admin is still told
    assert any("Could not send the error notice" in r.getMessage() for r in caplog.records)


async def test_dispatcher_routes_an_exception_in_a_real_handler_to_the_error_handler(monkeypatch):
    """End to end through aiogram: /orders raises, the error handler answers and alerts."""
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)

    async def explode(user_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "render_orders", explode)
    real_bot = Bot(token="123456:" + "A" * 35)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=777, type="private"),
            from_user=User(id=777, is_bot=False, first_name="Cust"),
            text="/orders",
            entities=[MessageEntity(type="bot_command", offset=0, length=7)],
        ),
    )

    await bot.dp.feed_update(real_bot, update)

    sent = [(m.chat_id, m.text) for m in calls if hasattr(m, "text")]
    assert any(str(chat) == "777" and text == bot.USER_ERROR_MESSAGE for chat, text in sent)
    assert any(str(chat) == "111" and "RuntimeError" in text for chat, text in sent)

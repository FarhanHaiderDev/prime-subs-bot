"""Item 3: per-user rate limiting on commands, button presses and payment-proof submissions."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, MessageEntity, Update, User

import bot
from conftest import ADMIN_ID, CUSTOMER_ID, FakeBot, TXID
from ratelimit import RateLimitMiddleware, SlidingWindowLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def fresh_limiters():
    bot.message_limiter.reset()
    bot.button_limiter.reset()
    bot.proof_limiter.reset()
    yield
    bot.message_limiter.reset()
    bot.button_limiter.reset()
    bot.proof_limiter.reset()


# ---- the limiter ------------------------------------------------------------------------

def test_allows_up_to_the_limit_then_blocks_with_a_wait_time():
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 10, clock=clock)

    assert [limiter.check("u") for _ in range(3)] == [0.0, 0.0, 0.0]
    clock.advance(4)
    wait = limiter.check("u")

    assert 5.9 < wait <= 6.0  # 10 s window, first event was 4 s ago


def test_waiting_out_the_returned_time_always_works():
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 10, clock=clock)
    limiter.check("u"), limiter.check("u")
    wait = limiter.check("u")
    assert wait > 0

    clock.advance(wait)

    assert limiter.check("u") == 0.0


def test_a_blocked_attempt_is_not_recorded():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 10, clock=clock)
    limiter.check("u")
    for _ in range(50):  # hammering while blocked must not extend the block
        limiter.check("u")
    clock.advance(10)

    assert limiter.check("u") == 0.0


def test_users_are_limited_independently():
    limiter = SlidingWindowLimiter(1, 10, clock=Clock())

    assert limiter.check(1) == 0.0
    assert limiter.check(1) > 0
    assert limiter.check(2) == 0.0


def test_stale_keys_are_pruned_so_memory_stays_bounded():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 10, clock=clock, max_keys=100)
    for user in range(100):
        limiter.check(user)
    clock.advance(11)

    limiter.check("new-user-1"), limiter.check("new-user-2")

    assert len(limiter._events) < 10


# ---- the middleware -------------------------------------------------------------------

def _event(user_id=CUSTOMER_ID):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())


async def _run(middleware, event):
    handler = AsyncMock(return_value="handled")
    result = await middleware(handler, event, {})
    return handler, result


async def test_message_flood_is_blocked_with_a_single_notice():
    middleware = RateLimitMiddleware(SlidingWindowLimiter(3, 10, clock=Clock()))
    event = _event()
    handled = 0
    for _ in range(10):
        handler, result = await _run(middleware, event)
        handled += handler.await_count

    assert handled == 3
    event.answer.assert_awaited_once()  # one notice per window, not one per blocked message
    assert "Too many requests" in event.answer.await_args.args[0]


async def test_button_presses_are_answered_every_time_when_blocked():
    middleware = RateLimitMiddleware(SlidingWindowLimiter(1, 10, clock=Clock()), notice_each_event=True)
    event = _event()
    for _ in range(4):
        await _run(middleware, event)

    assert event.answer.await_count == 3  # 1 handled, 3 blocked and each answered (spinner cleared)


async def test_admin_is_exempt_and_events_without_a_user_pass_through():
    middleware = RateLimitMiddleware(
        SlidingWindowLimiter(1, 10, clock=Clock()), is_exempt=lambda uid: uid == ADMIN_ID
    )
    admin = _event(ADMIN_ID)
    for _ in range(5):
        handler, _result = await _run(middleware, admin)
        assert handler.await_count == 1
    anonymous = SimpleNamespace(from_user=None, answer=AsyncMock())
    handler, _result = await _run(middleware, anonymous)
    assert handler.await_count == 1


async def test_a_failing_notice_does_not_raise():
    middleware = RateLimitMiddleware(SlidingWindowLimiter(1, 10, clock=Clock()))
    event = _event()
    event.answer.side_effect = RuntimeError("blocked")
    await _run(middleware, event)

    handler, result = await _run(middleware, event)  # blocked, notice fails

    assert handler.await_count == 0 and result is None


# ---- payment-proof submissions -----------------------------------------------------------------

def _proof_message(text, user_id=CUSTOMER_ID):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, full_name="Cust", username="cust"),
        answer=AsyncMock(),
        bot=FakeBot(),
    )


def _state():
    data = {
        "method": "trc20",
        "name": "Netflix 1M",
        "price": 4.2,
        "offers": [{"supplier": "pandora", "product_id": "p1", "variant_id": None, "cost": 3.0}],
    }
    return SimpleNamespace(get_data=AsyncMock(return_value=data), clear=AsyncMock())


async def test_repeated_proof_attempts_are_blocked_before_touching_the_database(storage, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bot, "proof_limiter", SlidingWindowLimiter(3, 600, clock=clock))
    reference_exists = AsyncMock(return_value=False)
    monkeypatch.setattr(storage, "reference_exists", reference_exists)

    for _ in range(3):
        await bot.receive_reference(_proof_message("not-a-valid-proof"), _state())  # invalid format counts too
    blocked = _proof_message(TXID)
    await bot.receive_reference(blocked, _state())

    assert "too many payment proofs" in blocked.answer.await_args.args[0]
    reference_exists.assert_not_called()
    assert await storage._orders.find_one({}) is None  # nothing was stored


async def test_proof_attempts_work_again_after_the_window(storage, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bot, "proof_limiter", SlidingWindowLimiter(1, 600, clock=clock))
    await bot.receive_reference(_proof_message("bad"), _state())
    blocked = _proof_message(TXID)
    await bot.receive_reference(blocked, _state())
    assert "too many payment proofs" in blocked.answer.await_args.args[0]

    clock.advance(601)
    ok = _proof_message(TXID)
    await bot.receive_reference(ok, _state())

    assert await storage._orders.find_one({}) is not None


async def test_limits_are_per_user(storage, monkeypatch):
    monkeypatch.setattr(bot, "proof_limiter", SlidingWindowLimiter(1, 600, clock=Clock()))
    await bot.receive_reference(_proof_message("bad", user_id=1), _state())

    other = _proof_message("bad", user_id=2)
    await bot.receive_reference(other, _state())

    assert "valid" in other.answer.await_args.args[0]  # reached the normal validation


# ---- through the real dispatcher ------------------------------------------------------------------

async def test_a_command_flood_through_the_dispatcher_is_throttled(monkeypatch):
    sent = []

    async def fake_call(self, method, request_timeout=None):
        sent.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    handled = []

    async def counting_send_menu(message, user_id):
        handled.append(user_id)

    monkeypatch.setattr(bot, "send_menu", counting_send_menu)
    real_bot = Bot(token="123456:" + "A" * 35)

    for i in range(20):
        update = Update(
            update_id=i,
            message=Message(
                message_id=i,
                date=datetime.now(),
                chat=Chat(id=42, type="private"),
                from_user=User(id=42, is_bot=False, first_name="Spammer"),
                text="/menu",
                entities=[MessageEntity(type="bot_command", offset=0, length=5)],
            ),
        )
        await bot.dp.feed_update(real_bot, update)

    assert len(handled) == 8  # the message limit
    notices = [m for m in sent if hasattr(m, "text") and "Too many requests" in m.text]
    assert len(notices) == 1

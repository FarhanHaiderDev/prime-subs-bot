"""Per-user rate limiting (in memory, sliding window).

Used as outer middleware on messages and button presses, and directly for payment-proof
submissions. State lives in the process: it resets on restart, which is acceptable for
spam protection (it is not a security boundary).
"""

import math
import time
from collections import deque

from aiogram import BaseMiddleware


class SlidingWindowLimiter:
    """Allow at most `limit` events per `window_seconds` for each key."""

    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic, max_keys: int = 10_000):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[object, deque] = {}

    def check(self, key) -> float:
        """Record an event and return 0.0 if it is allowed, otherwise the seconds to wait.

        A rejected event is not recorded, so waiting out the returned time always works."""
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self.window:
            events.popleft()
        if len(events) >= self.limit:
            return max(self.window - (now - events[0]), 0.001)
        events.append(now)
        if len(self._events) > self._max_keys:
            self._prune(now)
        return 0.0

    def _prune(self, now: float) -> None:
        for key in [k for k, ev in self._events.items() if not ev or now - ev[-1] >= self.window]:
            del self._events[key]

    def reset(self) -> None:
        self._events.clear()


def wait_text(seconds: float) -> str:
    seconds = math.ceil(seconds)
    return f"{seconds} second(s)" if seconds < 90 else f"{math.ceil(seconds / 60)} minute(s)"


class RateLimitMiddleware(BaseMiddleware):
    """Drops events from a user who exceeds the limiter and tells them to slow down.

    Messages get one notice per window (so the notice itself cannot be used to spam);
    button presses are always answered, which also clears the button's loading spinner.
    Exempt users (the admin) and events without a user pass straight through."""

    def __init__(self, limiter: SlidingWindowLimiter, is_exempt=lambda user_id: False, notice_each_event=False):
        self.limiter = limiter
        self.is_exempt = is_exempt
        self.notice_each_event = notice_each_event
        self._notices = SlidingWindowLimiter(1, limiter.window, clock=limiter._clock)

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or self.is_exempt(user.id):
            return await handler(event, data)
        wait = self.limiter.check(user.id)
        if wait == 0.0:
            return await handler(event, data)
        if self.notice_each_event or self._notices.check(user.id) == 0.0:
            try:
                await event.answer(f"Too many requests. Please wait {wait_text(wait)}.")
            except Exception:
                pass  # a failed notice must never turn into an error
        return None

    def reset(self) -> None:
        self.limiter.reset()
        self._notices.reset()

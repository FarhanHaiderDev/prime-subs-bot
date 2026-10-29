"""#17: Retry-After may be a number of seconds or an HTTP date."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import bot
from conftest import make_callback, new_order

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def http_date(moment):
    return format_datetime(moment, usegmt=True)


@pytest.mark.parametrize(
    "value,expected",
    [("7", 7.0), ("0", 0.0), (" 12 ", 12.0), ("2.5", 2.5), (7, 7.0), ("30", 30.0)],
)
def test_seconds(value, expected):
    assert bot.parse_retry_after(value, NOW) == expected


def test_an_http_date_in_the_future_is_converted_to_seconds():
    assert bot.parse_retry_after(http_date(NOW + timedelta(seconds=12)), NOW) == 12.0


def test_other_http_date_forms_are_understood():
    soon = NOW + timedelta(seconds=9)
    assert bot.parse_retry_after("Thu, 29 Oct 2026 12:00:09 GMT", NOW) == 9.0  # IMF-fixdate
    assert bot.parse_retry_after("Thursday, 29-Oct-26 12:00:09 GMT", NOW) == 9.0  # obsolete RFC 850
    assert bot.parse_retry_after("Thu, 29 Oct 2026 14:00:09 +0200", NOW) == 9.0  # numeric offset
    assert bot.parse_retry_after(http_date(soon).replace("GMT", "UTC"), NOW) == 9.0


def test_a_date_in_the_past_means_no_wait():
    assert bot.parse_retry_after(http_date(NOW - timedelta(minutes=5)), NOW) == 0.0


def test_the_wait_is_capped():
    assert bot.parse_retry_after("100000", NOW) == bot.RETRY_AFTER_MAX_SECONDS
    assert bot.parse_retry_after(http_date(NOW + timedelta(days=2)), NOW) == bot.RETRY_AFTER_MAX_SECONDS


@pytest.mark.parametrize("value", [None, "", "   ", "soon", "-5", "1e3", "12 seconds", "Wed, 99 Foo 2026 99:99:99 GMT", "--"])
def test_missing_or_unreadable_values_use_the_default(value):
    assert bot.parse_retry_after(value, NOW) == bot.RETRY_AFTER_DEFAULT_SECONDS


def test_the_default_is_used_without_a_clock_argument_too():
    assert bot.parse_retry_after(None) == bot.RETRY_AFTER_DEFAULT_SECONDS
    assert 0 < bot.parse_retry_after(http_date(datetime.now(timezone.utc) + timedelta(seconds=20))) <= 20


# ---- in the purchase flow ---------------------------------------------------------------------------

async def purchase_after_a_429(storage, pandora, monkeypatch, header):
    sleeps = []
    monkeypatch.setattr(bot.time, "sleep", sleeps.append)
    pandora.order_queue = [
        (429, {"error": {"code": "RATE_LIMITED"}}, {"Retry-After": header} if header is not None else {}),
        (200, pandora.order, {}),
    ]
    await storage.insert_order(new_order("ord1"))
    await bot.approve_order(make_callback("approve:ord1"))
    return sleeps, await storage.get_order("ord1")


async def test_a_429_with_a_seconds_header_waits_that_long_and_the_purchase_completes(storage, pandora, monkeypatch):
    sleeps, order = await purchase_after_a_429(storage, pandora, monkeypatch, "3")

    assert 3.0 in sleeps and order["status"] == "delivered"
    assert len(pandora.purchase_posts()) == 2  # the rate-limited try, then the successful one


async def test_a_429_with_an_http_date_no_longer_breaks_the_purchase(storage, pandora, monkeypatch):
    header = http_date(datetime.now(timezone.utc) + timedelta(seconds=15))

    sleeps, order = await purchase_after_a_429(storage, pandora, monkeypatch, header)

    # before the fix int("Thu, 29 Oct ...") raised and the order was parked as an unknown outcome
    assert order["status"] == "delivered"
    assert any(10 <= s <= 15 for s in sleeps)


async def test_a_429_with_garbage_or_no_header_waits_the_default(storage, pandora, monkeypatch):
    for header in ("whenever", None):
        pandora.order_queue = []
        sleeps, order = await purchase_after_a_429(storage, pandora, monkeypatch, header)
        assert bot.RETRY_AFTER_DEFAULT_SECONDS in sleeps and order["status"] == "delivered"
        await storage._orders.delete_one({"_id": "ord1"})

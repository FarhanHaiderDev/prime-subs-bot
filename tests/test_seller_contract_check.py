"""Item 5: the seller contract check is read-only, never leaks the key or data, and reports shapes."""

import importlib.util
import io
import json
import os
from types import SimpleNamespace

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "seller_contract_check.py")
_spec = importlib.util.spec_from_file_location("seller_contract_check", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

API_KEY = "sk_live_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
PRODUCT_NAME = "Secret Product Name"
BALANCE = "123.45"


class FakeSession:
    """Plays Pandora: canned responses, and a record of every request sent."""

    def __init__(self, overrides=None):
        self.requests = []
        self.overrides = overrides or {}

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        path = url.split("/api/v1", 1)[-1]
        self.requests.append((method, path, headers))
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        if headers["Authorization"].endswith("0" * 24):
            return reply(401, {"error": {"code": "INVALID_API_KEY", "message": "bad key"}})
        if method == "GET" and path == "/balance":
            return reply(200, {"available_balance": BALANCE, "currency": "USD", "client_id": "cli_x"}, rate=True)
        if method == "GET" and path == "/products":
            item = {"id": "prd_1", "name": PRODUCT_NAME, "unit_price": "3.00", "currency": "USD", "available_stock": 4}
            return reply(200, {"items": [item], "next_cursor": None})
        if method == "GET" and path == "/products/prd_1":
            return reply(200, {"id": "prd_1", "name": PRODUCT_NAME, "unit_price": "3.00"})
        if method == "GET" and path.endswith("/delivery-instructions"):
            return reply(200, {"product_id": "prd_1", "delivery_instructions": None})
        if method == "GET" and path == "/webhook":
            return reply(404, {"error": {"code": "WEBHOOK_NOT_FOUND", "message": "none"}})
        if method == "GET" and path == "/webhook-deliveries":
            return reply(200, {"items": []})
        if method == "GET" and path == "/products/does-not-exist":
            return reply(404, {"error": {"code": "PRODUCT_NOT_FOUND", "message": "no"}})
        if method == "GET" and path == "/orders/does-not-exist":
            return reply(404, {"error": {"code": "ORDER_NOT_FOUND", "message": "no"}})
        if method == "POST" and path == "/quotes":
            return reply(200, {"quote_id": "q", "unit_price": "3.00", "can_purchase": True, "price_version": "v1"})
        raise AssertionError(f"unexpected request {method} {path}")


def reply(status, body, rate=False):
    headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"} if rate else {}
    return SimpleNamespace(status_code=status, headers=headers, json=lambda: body)


def run(argv=(), session=None, env=None):
    out = io.StringIO()
    code = check.main(
        list(argv),
        env={"SUPPLIER_2_API_KEY": API_KEY} if env is None else env,
        session=session or FakeSession(),
        out=out,
        pause=0,
    )
    return code, out.getvalue()


# ---- it can never place an order ---------------------------------------------------------------

@pytest.mark.parametrize("method,path", [("POST", "/orders"), ("DELETE", "/webhook"), ("PUT", "/webhook"), ("POST", "/quotes")])
def test_everything_but_reads_is_refused(method, path):
    with pytest.raises(check.UnsafeRequest):
        check.assert_safe_request(method, path)  # even /quotes needs the explicit flag


def test_get_is_allowed_and_quotes_only_with_the_flag():
    check.assert_safe_request("GET", "/anything")
    check.assert_safe_request("POST", "/quotes", allow_quote=True)
    with pytest.raises(check.UnsafeRequest):
        check.assert_safe_request("POST", "/orders", allow_quote=True)


def test_the_default_run_sends_only_get_requests():
    session = FakeSession()
    code, _ = run(session=session)

    assert code == 0
    assert {method for method, _, _ in session.requests} == {"GET"}


def test_with_quote_adds_exactly_one_post_to_quotes_and_nothing_else():
    session = FakeSession()
    code, _ = run(["--with-quote"], session=session)

    posts = [(m, p) for m, p, _ in session.requests if m != "GET"]
    assert code == 0 and posts == [("POST", "/quotes")]


def test_the_plan_never_contains_an_order_endpoint_write():
    for with_quote in (False, True):
        for _label, method, path, *_ in check.build_plan(with_quote):
            assert not (method != "GET" and "orders" in path)


# ---- output: shapes, codes, masking ------------------------------------------------------------

def test_output_shows_shapes_status_and_error_codes_but_no_data_or_key():
    code, output = run()

    assert code == 0
    assert "status 200" in output and "status 401" in output and "status 404" in output
    for error_code in ("INVALID_API_KEY", "PRODUCT_NOT_FOUND", "ORDER_NOT_FOUND"):
        assert error_code in output
    assert '"available_balance": "str"' in output  # the shape, not the amount
    assert "X-RateLimit-Remaining" in output
    assert API_KEY not in output
    assert PRODUCT_NAME not in output and BALANCE not in output  # no response data


def test_the_key_is_masked_where_it_is_mentioned():
    _code, output = run()
    assert "sk_l****K1l2" in output
    assert check.mask(f"Bearer {API_KEY}", [API_KEY]) == "Bearer sk_l****K1l2"


def test_json_output_is_valid_and_also_free_of_secrets_and_data():
    code, output = run(["--json"])

    parsed = json.loads(output)
    assert code == 0 and parsed["results"] and parsed["key"] == "sk_l****K1l2"
    assert API_KEY not in output and PRODUCT_NAME not in output and BALANCE not in output


def test_shape_hides_values_but_shows_enums_and_flags():
    assert check.shape({"a": "secret", "status": "delivered", "n": 5, "ok": True, "can_purchase": True, "x": None}) == {
        "a": "str",
        "can_purchase": True,
        "n": "number",
        "ok": "bool",
        "status": "delivered",
        "x": "null",
    }
    assert check.shape([{"id": "x"}, {"id": "y"}]) == {"list[2]": {"id": "str"}}
    assert check.shape([]) == {"list[0]": None}


# ---- findings -----------------------------------------------------------------------------------

def test_an_unexpected_status_is_reported_and_fails_the_run():
    session = FakeSession({("GET", "/balance"): reply(500, {"error": {"code": "INTERNAL_ERROR"}})})
    code, output = run(session=session)

    assert code == 1
    assert "PROBLEMS" in output and "expected one of [200]" in output


def test_missing_keys_the_bot_depends_on_are_flagged():
    bad_list = reply(200, {"items": [{"id": "prd_1", "name": "x"}]})  # no unit_price
    code, output = run(session=FakeSession({("GET", "/products"): bad_list}))

    assert code == 1 and "unit_price" in output


def test_a_network_failure_is_a_finding_not_a_crash():
    class Down(FakeSession):
        def request(self, *args, **kwargs):
            raise ConnectionError("no route")

    code, output = run(session=Down())

    assert code == 1 and "request failed: ConnectionError" in output


def test_rate_limiting_stops_the_remaining_probes():
    session = FakeSession({("GET", "/balance"): reply(429, {"error": {"code": "RATE_LIMITED"}})})
    code, output = run(session=session)

    assert code == 1 and "rate limited" in output
    assert len(session.requests) == 1


# ---- configuration ---------------------------------------------------------------------------------

def test_a_missing_key_is_a_configuration_error_and_nothing_is_sent():
    session = FakeSession()
    code, _ = run(session=session, env={})

    assert code == 2 and session.requests == []


def test_dry_run_needs_no_key_and_sends_nothing():
    session = FakeSession()
    code, output = run(["--dry-run"], session=session, env={})

    assert code == 0 and session.requests == []
    assert "DRY RUN" in output and "/balance" in output and "/orders/does-not-exist" in output

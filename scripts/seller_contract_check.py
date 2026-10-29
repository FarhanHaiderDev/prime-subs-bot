#!/usr/bin/env python3
"""Read-only contract check for the Pandora Digital reseller API.

Calls only the SAFE endpoints and prints the response SHAPES (keys and types, never the
data), HTTP status codes and error codes it sees, so the bot's assumptions can be compared
with the real API. It NEVER places an order: the request whitelist below allows GET
requests only, plus POST /quotes if (and only if) --with-quote is given. A quote is a price
calculation, not a purchase.

    export SUPPLIER_2_API_KEY=...        # never pass the key on the command line
    python scripts/seller_contract_check.py              # run the checks
    python scripts/seller_contract_check.py --dry-run    # print the plan, send nothing
    python scripts/seller_contract_check.py --with-quote # also check POST /quotes
    python scripts/seller_contract_check.py --json        # machine-readable output

The API key is read from the environment (or, explicitly, from --env-file) and is masked
in all output. Exit status: 0 = every probe behaved as expected, 1 = a probe failed or
returned something unexpected, 2 = configuration error.
"""

import argparse
import json
import os
import sys
import time

DEFAULT_BASE_URL = "https://api.pandoradigital.shop/api/v1"
FAKE_KEY = "sk_live_" + "0" * 24  # deliberately invalid: used only to see the 401 error shape
RATE_LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")

# Values of these fields are enumerations or flags, not data, so they are shown as they are.
SAFE_VALUE_KEYS = {"status", "currency", "code", "delivery_type", "can_purchase", "price_guaranteed"}

# What bot.py reads from the responses; missing keys are reported as contract problems.
BOT_EXPECTATIONS = {
    "products": {"id", "name", "unit_price"},  # per item; also uses variant_id, variant_name, description, available_stock
    "quote": {"can_purchase", "unit_price", "price_version"},
}


class UnsafeRequest(Exception):
    """Raised before sending anything that is not on the read-only whitelist."""


def assert_safe_request(method: str, path: str, allow_quote: bool = False) -> None:
    method = method.upper()
    if method == "GET":
        return
    if method == "POST" and allow_quote and path.rstrip("/") == "/quotes":
        return
    raise UnsafeRequest(f"{method} {path} is not allowed: this script never places orders or changes anything")


def mask(text: str, secrets) -> str:
    for secret in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, f"{secret[:4]}****{secret[-4:]}")
    return text


def shape(value, key=None):
    """The structure of a JSON value without its data."""
    if isinstance(value, dict):
        return {k: shape(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return {f"list[{len(value)}]": shape(value[0]) if value else None}
    if isinstance(value, bool):
        return value if key in SAFE_VALUE_KEYS else "bool"
    if isinstance(value, str):
        return value[:40] if key in SAFE_VALUE_KEYS else "str"
    if isinstance(value, (int, float)):
        return "number"
    return "null" if value is None else type(value).__name__


def find_keys(value, wanted: set[str]) -> set[str]:
    """Which of `wanted` are present in a dict (or in the first item of a list)."""
    if isinstance(value, list):
        value = value[0] if value else {}
    return wanted & set(value) if isinstance(value, dict) else set()


def build_plan(with_quote: bool):
    """The probes, in order. Each is (label, method, path, params, key, expected statuses, expected error code)."""
    plan = [
        ("balance", "GET", "/balance", None, "real", {200}, None),
        ("product list", "GET", "/products", {"limit": 5}, "real", {200}, None),
        ("product detail", "GET", "/products/{product_id}", None, "real", {200}, None),
        ("delivery instructions", "GET", "/products/{product_id}/delivery-instructions", None, "real", {200}, None),
        ("webhook config", "GET", "/webhook", None, "real", {200, 404}, None),
        ("webhook deliveries", "GET", "/webhook-deliveries", {"limit": 1}, "real", {200}, None),
        ("error shape: unknown product", "GET", "/products/does-not-exist", None, "real", {404}, "PRODUCT_NOT_FOUND"),
        ("error shape: unknown order", "GET", "/orders/does-not-exist", None, "real", {404}, "ORDER_NOT_FOUND"),
        ("error shape: invalid key", "GET", "/balance", None, "fake", {401}, "INVALID_API_KEY"),
    ]
    if with_quote:
        plan.append(("quote (price only, no order)", "POST", "/quotes", None, "real", {200}, None))
    return plan


def _error_code(body):
    try:
        return str(body["error"]["code"])
    except (KeyError, TypeError):
        return None


def run_checks(session, base_url, api_key, timeout=15, with_quote=False, pause=0.4):
    results, product_id = [], None
    for label, method, path, params, key_kind, expected, expected_code in build_plan(with_quote):
        record = {"probe": label, "method": method, "path": path, "expected_status": sorted(expected)}
        if "{product_id}" in path:
            if not product_id:
                record.update(skipped="no product id available from the product list")
                results.append(record)
                continue
            path = path.replace("{product_id}", str(product_id))
            record["path"] = path.replace(str(product_id), "{product_id}")
        assert_safe_request(method, path, allow_quote=with_quote)

        body_json = None
        if method == "POST":
            if not product_id:
                record.update(skipped="no product id available from the product list")
                results.append(record)
                continue
            body_json = {"product_id": product_id, "quantity": 1}
        headers = {"Authorization": f"Bearer {FAKE_KEY if key_kind == 'fake' else api_key}"}

        started = time.monotonic()
        try:
            response = session.request(
                method, f"{base_url}{path}", headers=headers, params=params, json=body_json, timeout=timeout
            )
        except Exception as exc:  # network problems are a finding, not a crash
            record.update(error=f"request failed: {type(exc).__name__}", ok=False)
            results.append(record)
            continue
        record["status"] = response.status_code
        record["ms"] = round((time.monotonic() - started) * 1000)
        record["rate_limit"] = {h: response.headers.get(h) for h in RATE_LIMIT_HEADERS if response.headers.get(h)}
        try:
            body = response.json()
        except ValueError:
            body = None
            record["body"] = "non-JSON body"
        else:
            record["shape"] = shape(body)
            record["error_code"] = _error_code(body)

        problems = []
        if response.status_code not in expected:
            problems.append(f"status {response.status_code}, expected one of {sorted(expected)}")
        if expected_code and record.get("error_code") != expected_code:
            problems.append(f"error code {record.get('error_code')!r}, expected {expected_code!r}")
        if label == "product list" and response.status_code == 200 and isinstance(body, dict):
            items = body.get("items") or []
            missing = BOT_EXPECTATIONS["products"] - find_keys(items, BOT_EXPECTATIONS["products"])
            if items and missing:
                problems.append(f"product items lack keys the bot reads: {sorted(missing)}")
            product_id = items[0].get("id") if items and isinstance(items[0], dict) else None
        if label.startswith("quote") and response.status_code == 200 and isinstance(body, dict):
            missing = BOT_EXPECTATIONS["quote"] - set(body)
            if missing:
                problems.append(f"quote lacks keys the bot reads: {sorted(missing)}")
        record["problems"] = problems
        record["ok"] = not problems
        results.append(record)

        if response.status_code == 429:
            record["problems"].append("rate limited: stopping the remaining probes")
            record["ok"] = False
            break
        time.sleep(pause)
    return results


def render_text(results, base_url, masked_key, dry_run):
    out = [f"Pandora contract check - base {base_url} - key {masked_key}" + (" - DRY RUN" if dry_run else "")]
    for i, r in enumerate(results, 1):
        out.append(f"\n[{i}] {r['method']} {r['path']}  ({r['probe']})")
        if r.get("skipped"):
            out.append(f"    skipped: {r['skipped']}")
            continue
        if dry_run:
            out.append(f"    would expect status in {r['expected_status']}")
            continue
        if "error" in r:
            out.append(f"    {r['error']}")
            continue
        out.append(f"    status {r['status']} in {r['ms']} ms; rate-limit headers: {r['rate_limit'] or 'none'}")
        if r.get("error_code"):
            out.append(f"    error code: {r['error_code']}")
        out.append(f"    shape: {json.dumps(r.get('shape', r.get('body')), sort_keys=True)}")
        out.append("    OK" if r["ok"] else "    PROBLEMS: " + "; ".join(r["problems"]))
    if not dry_run:
        codes = sorted({r["error_code"] for r in results if r.get("error_code")})
        bad = [r for r in results if r.get("ok") is False]
        out.append(f"\nError codes seen: {', '.join(codes) or 'none'}")
        out.append(f"Summary: {len(results)} probes, {len(bad)} with problems")
    return "\n".join(out)


def main(argv=None, env=None, session=None, out=None, pause=0.4):
    out = out or sys.stdout
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--with-quote", action="store_true", help="also POST /quotes (a price calculation)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and send nothing")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--env-file", help="read SUPPLIER_2_API_KEY from this file (explicit opt-in)")
    args = parser.parse_args(argv)

    api_key = env.get("SUPPLIER_2_API_KEY", "")
    if args.env_file:
        try:
            from dotenv import dotenv_values

            api_key = dotenv_values(args.env_file).get("SUPPLIER_2_API_KEY") or api_key
        except ImportError:
            print("python-dotenv is not installed; set SUPPLIER_2_API_KEY in the environment.", file=sys.stderr)
            return 2
    if not api_key and not args.dry_run:
        print("SUPPLIER_2_API_KEY is not set (set it in the environment; never pass it as an argument).", file=sys.stderr)
        return 2
    masked_key = mask(api_key, [api_key]) if api_key else "(none)"

    if args.dry_run:
        results = [
            {"probe": p[0], "method": p[1], "path": p[2], "expected_status": sorted(p[5])}
            for p in build_plan(args.with_quote)
        ]
    else:
        if session is None:
            import requests

            session = requests.Session()
        results = run_checks(session, args.base_url.rstrip("/"), api_key, args.timeout, args.with_quote, pause)

    if args.as_json:
        text = json.dumps({"base_url": args.base_url, "key": masked_key, "dry_run": args.dry_run, "results": results}, indent=2)
    else:
        text = render_text(results, args.base_url, masked_key, args.dry_run)
    print(mask(text, [api_key, FAKE_KEY]), file=out)
    if args.dry_run:
        return 0
    return 0 if all(r.get("ok") or r.get("skipped") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

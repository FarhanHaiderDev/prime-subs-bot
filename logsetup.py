"""Structured (JSON-lines) logging with order ids and secret redaction.

* Every line is one JSON object: ts, level, logger, order_id, msg (+ traceback).
* order_id comes from a context variable that the order handlers set (set_order_id /
  order_context), so every line logged while an order is being handled carries it, even
  from helpers that never mention the id.
* Secrets are redacted from the finished line (message AND traceback): the exact values of
  BOT_TOKEN, the API keys, ENCRYPTION_KEY and the MongoDB URI credentials read from the
  environment, plus patterns for tokens/keys/URIs with credentials. Purchased credentials
  cannot be pattern-matched, so the code simply never logs them; tests check that.
"""

import contextvars
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

_order_id = contextvars.ContextVar("order_id", default=None)

SECRET_ENV_NAMES = ("BOT_TOKEN", "SUPPLIER_API_KEY", "SUPPLIER_2_API_KEY", "ENCRYPTION_KEY", "MONGODB_URI")
MIN_SECRET_LENGTH = 6  # shorter values would redact ordinary words

# (pattern, replacement). Order matters: URIs with credentials before generic keys.
SECRET_PATTERNS = [
    (re.compile(r"(mongodb(?:\+srv)?://)[^/\s:@]+:[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[REDACTED:telegram-token]"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]+"), "[REDACTED:api-key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}="), "[REDACTED:fernet-key]"),
]


def set_order_id(order_id) -> None:
    """Tag every log line of the current task with this order id (None clears it)."""
    _order_id.set(None if order_id is None else str(order_id))


def current_order_id():
    return _order_id.get()


@contextmanager
def order_context(order_id):
    token = _order_id.set(str(order_id))
    try:
        yield
    finally:
        _order_id.reset(token)


def collect_secret_values(environ=None) -> list[str]:
    """The exact secret values configured in the environment (and parts of them)."""
    environ = os.environ if environ is None else environ
    values: set[str] = set()
    for name in SECRET_ENV_NAMES:
        raw = (environ.get(name) or "").strip()
        if not raw:
            continue
        values.add(raw)
        if name == "ENCRYPTION_KEY":  # may hold several comma-separated keys
            values.update(part.strip() for part in raw.split(","))
        if name == "MONGODB_URI":
            try:
                parts = urlsplit(raw)
                values.update(filter(None, (unquote(parts.username or ""), unquote(parts.password or ""))))
            except ValueError:
                pass
    return sorted((v for v in values if len(v) >= MIN_SECRET_LENGTH), key=len, reverse=True)


class Redactor:
    def __init__(self, secret_values=()):
        self._values = sorted(set(secret_values), key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, "[REDACTED]")
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        message = self._redactor.redact(record.getMessage())
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "order_id": getattr(record, "order_id", None) or _order_id.get(),
            "msg": message,
        }
        if record.exc_info:
            payload["traceback"] = self._redactor.redact(self.formatException(record.exc_info))
        # final pass over the serialised line, in case a secret survives inside escaped text
        return self._redactor.redact(json.dumps(payload, ensure_ascii=False))


def configure_logging(level=logging.INFO, stream=None, environ=None) -> Redactor:
    """Replace the root handlers with one JSON-lines handler that redacts secrets."""
    redactor = Redactor(collect_secret_values(environ))
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(redactor))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    return redactor

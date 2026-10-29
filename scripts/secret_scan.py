#!/usr/bin/env python3
"""Scan the repository for committed secrets. Standard library only.

    python scripts/secret_scan.py            # scan every git-tracked file
    python scripts/secret_scan.py --history  # also scan every line ever added in git history

Looks for Telegram bot tokens, API keys (sk_live_/sk_test_), AWS access key ids, GitHub
tokens, private key blocks, MongoDB URIs that contain credentials, Fernet keys and bearer
tokens, and flags tracked files that should never be committed (.env, key files, the old
orders.json/prefs.json).

A match is NEVER printed in full: findings show file, line, rule and a masked value. A line
that is a deliberate fake (a test fixture) can carry the marker `secret-scan: allow`.
Fakes that were committed before they carried the marker stay in git history, so they are
listed in scripts/secret_scan_baseline.txt (`path|rule|masked value`, one per line). Anything
not on that list is a finding. Placeholders such as `mongodb+srv://<user>:<password>@...` do
not match by construction.

Exit status: 0 = clean, 1 = findings.
"""

import argparse
import os
import re
import subprocess
import sys

ALLOW_MARKER = "secret-scan: allow"
MAX_FILE_BYTES = 1_000_000

RULES = [
    ("telegram-bot-token", re.compile(r"(?<![\w:])\d{8,10}:[A-Za-z0-9_-]{35}(?![\w-])")),
    ("api-key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("mongodb-uri-with-credentials", re.compile(r"mongodb(?:\+srv)?://[^\s/:@<>{}'\"]+:[^\s/@<>{}'\"]+@")),
    ("fernet-key", re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9=])")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
]

FORBIDDEN_FILES = [
    (re.compile(r"(^|/)\.env(\.(?!example$)[^/]*)?$"), "environment file"),
    (re.compile(r"\.(pem|key|p12|pfx)$"), "key or certificate file"),
    (re.compile(r"(^|/)id_(rsa|ed25519|ecdsa)(\.|$)"), "SSH private key"),
    (re.compile(r"(^|/)(orders|prefs)\.json$"), "legacy data file (orders/preferences)"),
]


def mask(value: str) -> str:
    return f"{value[:4]}****{value[-2:]}" if len(value) >= 12 else "****"


def scan_line(line: str):
    """Yield (rule, masked match) for every secret-looking thing on a line."""
    if ALLOW_MARKER in line:
        return
    for name, pattern in RULES:
        for match in pattern.finditer(line):
            yield name, mask(match.group(0))


def git(root, *args):
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def scan_tree(root):
    findings = []
    for path in git(root, "ls-files", "-z").split("\0"):
        if not path:
            continue
        for pattern, why in FORBIDDEN_FILES:
            if pattern.search(path):
                findings.append((path, 0, "forbidden-file", why))
        full = os.path.join(root, path)
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data:  # binary
            continue
        for number, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
            for rule, masked in scan_line(line):
                findings.append((path, number, rule, masked))
    return findings


def scan_history(root):
    """Every line ever ADDED in any commit, so a secret removed later is still found."""
    findings, commit, path = [], "?", "?"
    log = git(root, "log", "--all", "-p", "--no-color", "--format=commit %H")
    for line in log.splitlines():
        if line.startswith("commit ") and re.fullmatch(r"commit [0-9a-f]{40}", line):
            commit = line[7:15]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            for rule, masked in scan_line(line[1:]):
                findings.append((f"{path} @ {commit}", 0, rule, masked))
        elif line.startswith("diff --git"):
            for pattern, why in FORBIDDEN_FILES:
                target = line.split(" b/", 1)[-1]
                if pattern.search(target):
                    findings.append((f"{target} @ {commit}", 0, "forbidden-file", why))
    return findings


def load_baseline(path):
    entries = set()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    entries.add(line)
    return entries


def finding_key(where, rule, detail):
    return f"{where.split(' @ ')[0]}|{rule}|{detail}"


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--history", action="store_true", help="also scan every line added in git history")
    parser.add_argument("--baseline", help="known fakes to ignore (default: scripts/secret_scan_baseline.txt)")
    args = parser.parse_args(argv)

    findings = scan_tree(args.root)
    if args.history:
        findings += scan_history(args.root)
    baseline = load_baseline(args.baseline or os.path.join(args.root, "scripts", "secret_scan_baseline.txt"))
    findings = [f for f in findings if finding_key(f[0], f[2], f[3]) not in baseline]
    for where, number, rule, detail in findings:
        location = f"{where}:{number}" if number else where
        print(f"{location}: [{rule}] {detail}", file=out)
    scope = "tree + history" if args.history else "tree"
    print(f"secret scan ({scope}): {len(findings)} finding(s)", file=out)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

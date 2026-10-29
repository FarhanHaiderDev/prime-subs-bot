"""Item 8: the secret scanner, and a guard that this repository itself is clean."""

import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts" / "secret_scan.py")
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

# Real-looking secrets, assembled at run time so this file does not itself contain them.
TELEGRAM_TOKEN = "123456789" + ":" + "AbCdEfGhIj" * 3 + "AbCde"
API_KEY = "sk_" + "live_" + "Z9y8X7w6V5u4T3s2R1q0"
MONGO_URI = "mongodb+srv://" + "dbuser:Sup3rS3cretPw" + "@cluster0.example.mongodb.net/db"
AWS_KEY = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
GITHUB_TOKEN = "gh" + "p_" + "a" * 36
FERNET_KEY = "A" * 43 + "="
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


def commit_files(repo, files, message="c"):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def run(repo, *flags):
    out = io.StringIO()
    code = scan.main(["--root", str(repo), *flags], out=out)
    return code, out.getvalue()


# ---- detection ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secret,rule",
    [
        (TELEGRAM_TOKEN, "telegram-bot-token"),
        (API_KEY, "api-key"),
        (MONGO_URI, "mongodb-uri-with-credentials"),
        (AWS_KEY, "aws-access-key-id"),
        (GITHUB_TOKEN, "github-token"),
        (FERNET_KEY, "fernet-key"),
        (PEM, "private-key-block"),
        ("Authorization: Bearer abcdefghij0123456789xyz", "bearer-token"),  # secret-scan: allow (deliberate fake)
    ],
)
def test_each_kind_of_secret_is_detected_and_never_printed(repo, secret, rule):
    commit_files(repo, {"app.py": f'VALUE = "{secret}"\n'})

    code, output = run(repo)

    assert code == 1
    assert f"[{rule}]" in output and "app.py:1" in output
    assert secret not in output  # only a masked form is shown


def test_a_clean_repository_passes(repo):
    commit_files(repo, {"app.py": "print('hello')\n", "README.md": "no secrets here\n"})

    code, output = run(repo)

    assert code == 0 and "0 finding(s)" in output


def test_placeholders_and_env_example_do_not_trigger(repo):
    commit_files(
        repo,
        {
            ".env.example": "BOT_TOKEN=your-telegram-bot-token\nMONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/\n"
            "ENCRYPTION_KEY=your-generated-fernet-key\n",
            "docs.md": "Use `mongodb+srv://...` and Bearer <token> as shown.\n",
        },
    )

    assert run(repo)[0] == 0


def test_the_allow_marker_exempts_a_deliberate_fake(repo):
    commit_files(repo, {"test_x.py": f'FAKE = "{API_KEY}"  # secret-scan: allow\nREAL = "{TELEGRAM_TOKEN}"\n'})

    code, output = run(repo)

    assert code == 1
    assert "test_x.py:2" in output and "test_x.py:1:" not in output


# ---- files that must never be committed ----------------------------------------------------------

@pytest.mark.parametrize("name", [".env", ".env.production", "server.pem", "deploy.key", "orders.json", "prefs.json", "id_rsa"])
def test_forbidden_files_are_flagged_even_if_their_content_looks_harmless(repo, name):
    commit_files(repo, {name: "nothing sensitive-looking\n"})

    code, output = run(repo)

    assert code == 1 and "[forbidden-file]" in output


def test_env_example_is_allowed(repo):
    commit_files(repo, {".env.example": "PORT=8080\n"})
    assert run(repo)[0] == 0


# ---- history -------------------------------------------------------------------------------------

def test_a_secret_committed_then_removed_is_found_only_by_the_history_scan(repo):
    commit_files(repo, {"config.py": f'TOKEN = "{TELEGRAM_TOKEN}"\n'}, "add token")
    commit_files(repo, {"config.py": "TOKEN = None\n"}, "remove token")

    tree_code, _ = run(repo)
    history_code, output = run(repo, "--history")

    assert tree_code == 0  # the working tree is clean...
    assert history_code == 1  # ...but the history still holds it
    assert "[telegram-bot-token]" in output and "config.py @" in output
    assert TELEGRAM_TOKEN not in output


def test_a_forbidden_file_that_was_committed_and_deleted_is_found_in_history(repo):
    commit_files(repo, {".env": "X=1\n"}, "oops")
    git(repo, "rm", "-q", ".env")
    git(repo, "commit", "-q", "-m", "remove it")

    assert run(repo)[0] == 0
    code, output = run(repo, "--history")
    assert code == 1 and "[forbidden-file]" in output


# ---- this repository -----------------------------------------------------------------------------------

def test_this_repository_has_no_committed_secrets():
    """The same check CI runs. If it fails: remove the secret AND rotate it (it is in git history)."""
    code, output = run(ROOT)
    assert code == 0, output


def test_the_whole_history_of_this_repository_is_clean_apart_from_the_baseline():
    code, output = run(ROOT, "--history")
    assert code == 0, output


# ---- the baseline -------------------------------------------------------------------------------------

def test_a_baseline_entry_hides_exactly_that_finding_and_nothing_else(repo):
    commit_files(repo, {"fixture.py": f'A = "{API_KEY}"\nB = "{TELEGRAM_TOKEN}"\n'})
    code, output = run(repo)
    assert code == 1 and output.count("[") >= 2
    api_line = next(line for line in output.splitlines() if "[api-key]" in line)
    masked = api_line.split("] ", 1)[1]
    (repo / "scripts").mkdir()
    (repo / "scripts" / "secret_scan_baseline.txt").write_text(f"fixture.py|api-key|{masked}   # a known fake\n")

    code, output = run(repo)

    assert code == 1
    assert "[api-key]" not in output  # baselined
    assert "[telegram-bot-token]" in output  # a different secret in the same file is still reported


def test_a_baseline_entry_for_a_different_file_does_not_hide_a_finding(repo):
    commit_files(repo, {"real.py": f'A = "{API_KEY}"\n'})
    (repo / "scripts").mkdir()
    (repo / "scripts" / "secret_scan_baseline.txt").write_text("other.py|api-key|sk_l****q0\n")

    assert run(repo)[0] == 1

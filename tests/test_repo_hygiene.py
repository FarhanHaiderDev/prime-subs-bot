"""#25: editor settings are not tracked, and .gitignore carries no stale entries for the removed
JSON storage."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout"
)


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def ignore_lines():
    return [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]


def test_no_editor_settings_are_tracked():
    assert git("ls-files", ".vscode").stdout.strip() == ""


def test_the_editor_directory_is_ignored_so_it_cannot_come_back():
    assert ".vscode/" in ignore_lines()
    assert git("check-ignore", "-q", ".vscode/settings.json").returncode == 0


def test_no_stale_entries_for_the_removed_json_storage():
    lines = ignore_lines()
    assert "orders.json" not in lines and "prefs.json" not in lines


def test_the_essential_ignores_are_still_there():
    lines = ignore_lines()
    for needed in (".env", "__pycache__/", ".venv/", "backups/", ".pytest_cache/"):
        assert needed in lines


def test_the_removed_json_files_would_be_caught_by_the_secret_scan_if_committed():
    # they are no longer ignored, so the scan (run in CI) is what keeps them out of the repo
    scanner = (ROOT / "scripts" / "secret_scan.py").read_text(encoding="utf-8")
    assert "orders|prefs" in scanner

"""Item 7: scripts/backup_mongo.sh, exercised with a stub mongodump (no database involved)."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_mongo.sh"
BASH = shutil.which("bash")
URI = "mongodb+srv://dbuser:Sup3rS3cretPw@cluster0.example.mongodb.net/?retryWrites=true"  # secret-scan: allow (deliberate fake)

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

# A fake mongodump: records how it was called and what config it got, writes an "archive".
STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_DIR/args.txt"
for arg in "$@"; do
  case "$arg" in
    --config=*) cp "${arg#--config=}" "$STUB_DIR/config_seen.txt" ;;
    --archive=*) archive="${arg#--archive=}" ;;
  esac
done
[ -n "$STUB_FAIL" ] && { echo "mongodump: simulated failure" >&2; exit 1; }
[ -n "$STUB_EMPTY" ] && : > "$archive" || echo "fake archive data" > "$archive"
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "mongodump"
    stub.write_text(STUB, newline="\n")
    stub.chmod(0o755)
    backups = tmp_path / "backups"
    return SimpleSandbox(tmp_path, stub_dir, backups)


class SimpleSandbox:
    def __init__(self, tmp_path, stub_dir, backups):
        self.tmp_path, self.stub_dir, self.backups = tmp_path, stub_dir, backups

    def run(self, extra_env=None, with_stub=True, uri=URI):
        env = {k: v for k, v in os.environ.items() if k not in ("MONGODB_URI", "MONGODB_DB", "BACKUP_RETENTION_DAYS")}
        env["BACKUP_DIR"] = self.backups.as_posix()
        env["STUB_DIR"] = self.stub_dir.as_posix()
        if with_stub:
            env["PATH"] = self.stub_dir.as_posix() + os.pathsep + env.get("PATH", "")
        if uri is not None:
            env["MONGODB_URI"] = uri
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, SCRIPT.as_posix()], env=env, capture_output=True, text=True, timeout=60
        )

    def archives(self):
        return sorted(p.name for p in self.backups.glob("*.archive.gz")) if self.backups.exists() else []


def test_script_has_valid_bash_syntax():
    result = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_writes_a_timestamped_compressed_archive_with_a_checksum(sandbox):
    result = sandbox.run()

    assert result.returncode == 0, result.stderr
    (name,) = sandbox.archives()
    assert re.fullmatch(r"prime_subs_bot_\d{8}T\d{6}Z\.archive\.gz", name)
    assert (sandbox.backups / name).read_text().strip() == "fake archive data"
    assert (sandbox.backups / (name + ".sha256")).exists()
    assert "Backup written" in result.stdout
    assert not list(sandbox.backups.glob("*.partial"))


def test_dumps_only_the_configured_database_compressed(sandbox):
    sandbox.run({"MONGODB_DB": "other_db"})

    args = (sandbox.stub_dir / "args.txt").read_text().split()
    assert "--nsInclude=other_db.*" in args and "--gzip" in args
    assert any(a.startswith("--archive=") for a in args)
    assert sandbox.archives()[0].startswith("other_db_")


def test_the_uri_never_appears_in_arguments_or_output_only_in_the_private_config(sandbox):
    result = sandbox.run()

    args = (sandbox.stub_dir / "args.txt").read_text()
    assert "Sup3rS3cretPw" not in args and "mongodb" not in args
    assert "Sup3rS3cretPw" not in result.stdout + result.stderr
    # it is handed over through the config file...
    assert URI in (sandbox.stub_dir / "config_seen.txt").read_text()
    # ...and that file is gone afterwards
    config_arg = next(a for a in args.split() if a.startswith("--config="))
    assert not Path(config_arg.split("=", 1)[1]).exists()


def test_a_uri_containing_a_single_quote_is_written_as_valid_yaml(sandbox):
    sandbox.run(uri="mongodb://user:pa'ss@host/db")

    assert "uri: 'mongodb://user:pa''ss@host/db'" in (sandbox.stub_dir / "config_seen.txt").read_text()


def test_missing_uri_fails_and_does_not_call_mongodump(sandbox):
    result = sandbox.run(uri=None)

    assert result.returncode != 0 and "MONGODB_URI is not set" in result.stderr
    assert not (sandbox.stub_dir / "args.txt").exists()


def test_missing_mongodump_is_reported(sandbox):
    # an empty PATH directory only: mongodump cannot be found
    empty = sandbox.tmp_path / "empty"
    empty.mkdir()
    result = sandbox.run(with_stub=False, extra_env={"PATH": empty.as_posix() + os.pathsep + "/usr/bin" + os.pathsep + "/bin"})

    assert result.returncode == 127 and "mongodump not found" in result.stderr


def test_a_failing_mongodump_keeps_no_archive_and_fails(sandbox):
    result = sandbox.run({"STUB_FAIL": "1"})

    assert result.returncode != 0
    assert sandbox.archives() == []
    assert not list(sandbox.backups.glob("*.partial"))


def test_an_empty_archive_is_rejected(sandbox):
    result = sandbox.run({"STUB_EMPTY": "1"})

    assert result.returncode == 1 and "empty archive" in result.stderr
    assert sandbox.archives() == []


def test_retention_prunes_only_this_scripts_old_backups(sandbox):
    sandbox.backups.mkdir()
    old = sandbox.backups / "prime_subs_bot_20200101T000000Z.archive.gz"
    unrelated_old = sandbox.backups / "notes.txt"
    for path in (old, unrelated_old):
        path.write_text("x")
        long_ago = time.time() - 40 * 86400
        os.utime(path, (long_ago, long_ago))

    result = sandbox.run({"BACKUP_RETENTION_DAYS": "30"})

    assert result.returncode == 0, result.stderr
    assert not old.exists() and unrelated_old.exists()
    assert len(sandbox.archives()) == 1  # the new one survives


def test_a_non_numeric_retention_is_rejected(sandbox):
    result = sandbox.run({"BACKUP_RETENTION_DAYS": "abc"})

    assert result.returncode == 2 and "whole number" in result.stderr
    assert not (sandbox.stub_dir / "args.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_backups_are_private_to_the_owner(sandbox):
    sandbox.run()

    (name,) = sandbox.archives()
    assert (sandbox.backups / name).stat().st_mode & 0o077 == 0

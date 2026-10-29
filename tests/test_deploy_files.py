"""Item 6: the deployment files are consistent with the code and safe by construction."""

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def env_var_names_read_by_the_code():
    names = set()
    for path in glob.glob(os.path.join(ROOT, "*.py")):
        source = open(path, encoding="utf-8").read()
        names.update(re.findall(r"os\.getenv\(\s*\"([A-Z0-9_]+)\"", source))
        names.update(re.findall(r"environ\.get\(\s*\"([A-Z0-9_]+)\"", source))
    return names


def test_the_code_reads_the_variables_we_expect():
    # a tripwire: if this set changes, DEPLOY.md and .env.example must be reviewed
    assert env_var_names_read_by_the_code() >= {
        "BOT_TOKEN", "ADMIN_CHAT_ID", "SUPPLIER_2_API_KEY", "MONGODB_URI", "ENCRYPTION_KEY", "PORT",
    }


def test_deploy_md_lists_every_environment_variable_the_code_reads():
    deploy = read("DEPLOY.md")
    missing = [name for name in sorted(env_var_names_read_by_the_code()) if f"`{name}`" not in deploy]
    assert missing == []


def test_env_example_lists_every_environment_variable_the_code_reads():
    example = read(".env.example")
    missing = [name for name in sorted(env_var_names_read_by_the_code()) if not re.search(rf"^{name}=", example, re.M)]
    assert missing == []


def test_dockerfile_runs_as_a_non_root_user():
    dockerfile = read("Dockerfile")
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)

    assert users, "the Dockerfile must switch to a non-root user"
    assert users[-1] not in ("root", "0")
    assert re.search(r"useradd .*--uid\s+\d+", dockerfile)


def test_dockerfile_bakes_in_no_secret_and_copies_no_env_file():
    dockerfile = read("Dockerfile")

    assert not re.search(r"^\s*(ENV|ARG)\s+\S*(TOKEN|KEY|URI|PASSWORD|SECRET)\S*\s*=", dockerfile, re.M | re.I)
    assert not re.search(r"COPY[^\n]*\.env", dockerfile)
    assert re.search(r"^COPY\s+(--\S+\s+)*\*\.py\s", dockerfile, re.M)  # modules only, nothing else


def test_dockerignore_keeps_secrets_and_junk_out_of_the_image():
    lines = read(".dockerignore").split()
    for needed in (".env", ".git", ".venv", "tests", "backups"):
        assert needed in lines


def test_compose_bot_service_is_locked_down_and_has_no_inline_secrets():
    compose = read("docker-compose.yml")

    assert "env_file: .env" in compose
    for hardening in ("read_only: true", "cap_drop:", "no-new-privileges:true", "127.0.0.1:8080:8080"):
        assert hardening in compose
    assert not re.search(r"(TOKEN|KEY|PASSWORD|SECRET|MONGODB_URI)\s*[:=]\s*\S", compose.replace("MONGODB_TEST_URI", ""))


def test_gitignore_covers_secrets_and_backups():
    ignored = read(".gitignore").split()
    assert ".env" in ignored


def test_backups_are_git_ignored_and_the_restore_docs_cover_the_key():
    assert "backups/" in read(".gitignore").split()
    deploy = read("DEPLOY.md")
    assert "## Backup and restore" in deploy
    assert "ENCRYPTION_KEY must be backed up separately" in deploy
    assert "mongorestore" in deploy and "scripts/backup_mongo.sh" in deploy

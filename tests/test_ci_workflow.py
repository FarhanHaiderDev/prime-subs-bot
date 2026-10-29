"""Item 8: the CI workflow runs the tests (with a real MongoDB) and the secret scan on every push."""

import re
from pathlib import Path

WORKFLOW = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_it_runs_on_every_push_and_pull_request():
    assert re.search(r"^on:\s*\n\s+push:\s*\n\s+pull_request:", WORKFLOW, re.M)
    assert "branches:" not in WORKFLOW.split("jobs:")[0]  # no branch filter: every push


def test_it_runs_the_tests_against_a_real_mongo_and_cannot_silently_skip_them():
    assert "image: mongo:7" in WORKFLOW
    assert "MONGODB_TEST_URI: mongodb://127.0.0.1:27017" in WORKFLOW
    assert 'REQUIRE_REAL_MONGO: "1"' in WORKFLOW
    assert "pip install -r requirements-dev.txt" in WORKFLOW
    assert re.search(r"run: pytest\b", WORKFLOW)


def test_it_scans_the_tree_and_the_whole_history_for_secrets():
    assert "python scripts/secret_scan.py --history" in WORKFLOW
    assert "fetch-depth: 0" in WORKFLOW  # without it the history scan would only see one commit


def test_it_is_least_privilege_and_uses_no_secrets():
    assert re.search(r"^permissions:\s*\n\s+contents: read", WORKFLOW, re.M)
    assert "${{ secrets." not in WORKFLOW and "${{secrets." not in WORKFLOW  # no secret is referenced
    assert "pull_request_target" not in WORKFLOW


def test_actions_are_pinned_to_a_major_version():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW)
    assert uses and all(re.fullmatch(r"[\w./-]+@v\d+", u) for u in uses), uses

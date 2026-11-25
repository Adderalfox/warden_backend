"""
Exercises the real git_server.py tools against a real throwaway repo.
Covers the failure modes seen in practice: several incidents in a row,
LLM-style diffs (bare headers, wrong hunk counts, no final newline, code
fences), CRLF working files, and a guarantee that the user's own checkout
is never switched or modified.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BUGGY = "def add(a, b):\n    return a - b  # bug\n"
TESTS = "from app import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"

# What Gemini actually produced: no a/ b/ prefixes, wrong counts, no final newline.
LLM_DIFF = "--- app.py\n+++ app.py\n@@ -1,9 +1,9 @@\n def add(a, b):\n-    return a - b  # bug\n+    return a + b  # fixed"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "git_workdir", str(tmp_path / "workdir"))
    monkeypatch.setattr(settings, "github_token", None)

    p = tmp_path / "sample-repo"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.email", "t@warden.local")
    git(p, "config", "user.name", "Warden Test")
    (p / "app.py").write_bytes(BUGGY.encode())
    (p / "test_app.py").write_bytes(TESTS.encode())
    git(p, "add", ".")
    git(p, "commit", "-q", "-m", "initial commit")
    return p


def test_get_recent_commits(repo):
    from mcp_servers.git_server import get_recent_commits

    commits = get_recent_commits(repo=str(repo), filepath="app.py", limit=5)
    assert len(commits) == 1 and "initial commit" in commits[0]["message"]


def test_get_file_content_keeps_trailing_newline(repo):
    from mcp_servers.git_server import get_file_content

    res = get_file_content(repo=str(repo), filepath="app.py")
    assert res["found"] is True
    assert res["content"] == BUGGY


def test_get_file_content_missing_file(repo):
    from mcp_servers.git_server import get_file_content

    assert get_file_content(repo=str(repo), filepath="nope.py")["found"] is False


def test_full_flow_leaves_users_checkout_untouched(repo):
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch, open_pull_request

    assert create_isolated_branch(repo=str(repo), base_branch="main", new_branch="warden/fix-1") is True
    res = apply_patch_and_test(repo=str(repo), diff=LLM_DIFF, test_cmd=f'"{sys.executable}" -m pytest -q', branch="warden/fix-1")
    assert res["applied"] is True and res["passed"] is True

    # The user's checkout: still on main, clean, still buggy.
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == ""
    assert (repo / "app.py").read_text() == BUGGY

    # The fix is committed on the branch.
    assert "a + b" in git(repo, "show", "warden/fix-1:app.py")

    pr = open_pull_request(repo=str(repo), title="t", body="b", head="warden/fix-1", base="main")
    assert pr["dry_run"] is True


def test_many_incidents_in_a_row_do_not_poison_each_other(repo):
    """Regression: the old code left the patch applied, so incident #2 failed."""
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch

    for i in range(3):
        b = f"warden/fix-{i}"
        create_isolated_branch(repo=str(repo), base_branch="main", new_branch=b)
        res = apply_patch_and_test(repo=str(repo), diff=LLM_DIFF, test_cmd=f'"{sys.executable}" -m pytest -q', branch=b)
        assert res["applied"] is True and res["passed"] is True, f"incident {i}: {res}"
    assert git(repo, "status", "--porcelain").strip() == ""


def test_fenced_diff_is_accepted(repo):
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch

    create_isolated_branch(repo=str(repo), base_branch="main", new_branch="warden/fix-f")
    res = apply_patch_and_test(repo=str(repo), diff=f"```diff\n{LLM_DIFF}\n```", test_cmd=f'"{sys.executable}" -m pytest -q', branch="warden/fix-f")
    assert res["applied"] is True and res["passed"] is True


def test_crlf_working_file_still_applies(repo):
    """A CRLF file committed to the repo (VS Code on Windows) must still patch."""
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch

    (repo / "app.py").write_bytes(BUGGY.replace("\n", "\r\n").encode())
    git(repo, "-c", "core.autocrlf=false", "add", "app.py")
    git(repo, "commit", "-q", "-m", "crlf")
    create_isolated_branch(repo=str(repo), base_branch="main", new_branch="warden/fix-crlf")
    res = apply_patch_and_test(repo=str(repo), diff=LLM_DIFF, test_cmd=f'"{sys.executable}" -m pytest -q', branch="warden/fix-crlf")
    assert res["applied"] is True and res["passed"] is True, res


def test_bad_patch_reports_apply_error(repo):
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch

    create_isolated_branch(repo=str(repo), base_branch="main", new_branch="warden/fix-bad")
    res = apply_patch_and_test(repo=str(repo), diff="not a real diff", test_cmd="true", branch="warden/fix-bad")
    assert res["applied"] is False and res["apply_error"]


def test_failing_tests_are_reported_and_not_committed(repo):
    from mcp_servers.git_server import apply_patch_and_test, create_isolated_branch

    wrong = LLM_DIFF.replace("a + b  # fixed", "0  # wrong")
    create_isolated_branch(repo=str(repo), base_branch="main", new_branch="warden/fix-w")
    res = apply_patch_and_test(repo=str(repo), diff=wrong, test_cmd=f'"{sys.executable}" -m pytest -q', branch="warden/fix-w")
    assert res["applied"] is True and res["passed"] is False
    assert git(repo, "rev-list", "--count", "warden/fix-w").strip() == "1"  # no fix commit


def test_ephemeral_mode_without_branch(repo):
    from mcp_servers.git_server import apply_patch_and_test

    res = apply_patch_and_test(repo=str(repo), diff=LLM_DIFF, test_cmd=f'"{sys.executable}" -m pytest -q')
    assert res["applied"] is True and res["passed"] is True
    assert git(repo, "status", "--porcelain").strip() == ""
    assert len(git(repo, "worktree", "list").strip().splitlines()) == 1  # temp worktree cleaned up
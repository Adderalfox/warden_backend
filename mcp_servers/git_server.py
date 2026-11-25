"""
mcp-git: standalone MCP server for git/GitHub operations. Every tool does
real work, and none of them touch the user's own checkout:

- get_recent_commits     -> real `git log` for a path.
- get_file_content       -> real file contents at a ref (default: main).
- create_isolated_branch -> creates the fix branch in its OWN git worktree
                            (a separate directory), so the repo's main
                            working tree is never switched or modified.
- apply_patch_and_test   -> applies the diff inside that worktree, runs the
                            test command there, and commits on success.
- open_pull_request      -> pushes the branch and opens a PR (PyGithub), or
                            returns a clearly-labelled dry_run result.

Repos are cloned/cached under WARDEN_GIT_WORKDIR; worktrees live under
WARDEN_GIT_WORKDIR/worktrees.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP

from app.core.config import settings

from git import Repo, GitCommandError

mcp = FastMCP("warden-mcp-git")

_COMMIT_ID = ["-c", "user.name=Warden", "-c", "user.email=warden@localhost"]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class Commit(BaseModel):
    sha: str
    author: str
    date: str
    message: str


class TestResult(BaseModel):
    applied: bool
    passed: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    apply_error: Optional[str] = None


class PRUrl(BaseModel):
    url: str
    number: Optional[int] = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _repo_local_path(repo: str) -> Path:
    """`repo` may be a local path or a git URL. Returns the local path,
    cloning if it's a remote URL we haven't cached yet."""
    workdir = Path(settings.git_workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    p = Path(repo)
    if p.exists() and (p / ".git").exists():
        return p

    name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
    local_path = workdir / name
    if not local_path.exists():
        Repo.clone_from(repo, local_path)
    else:
        try:
            Repo(local_path).remotes.origin.fetch()
        except Exception:
            pass  # offline / no remote; work with what's cached locally
    return local_path


def _open_repo(repo: str) -> Repo:
    r = Repo(_repo_local_path(repo))
    r.git.update_environment(GIT_TERMINAL_PROMPT="0")  # never block on a credential prompt
    return r


def _worktree_path(local_path: Path, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    return Path(settings.git_workdir) / "worktrees" / f"{local_path.name}__{safe}"


def _run(args: list, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    # stdin=DEVNULL: this process speaks MCP over stdin/stdout, so children
    # must never inherit those pipes (this caused hangs on Windows).
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, stdin=subprocess.DEVNULL,
    )


def _normalize_diff(diff: str) -> str:
    """LLMs often wrap diffs in markdown fences and omit the final newline."""
    lines = [ln for ln in diff.strip("\r\n").splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines) + "\n"


def _remove_worktree(r: Repo, wt: Path) -> None:
    try:
        r.git.worktree("remove", "--force", str(wt))
    except GitCommandError:
        pass
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    try:
        r.git.worktree("prune")
    except GitCommandError:
        pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_recent_commits(repo: str, filepath: str, limit: int = 10) -> list[dict]:
    """Return real commit history (via `git log`) for `filepath` in `repo`."""
    r = _open_repo(repo)
    commits = []
    try:
        for c in r.iter_commits(paths=filepath, max_count=limit):
            commits.append(
                Commit(
                    sha=c.hexsha,
                    author=f"{c.author.name} <{c.author.email}>",
                    date=c.committed_datetime.isoformat(),
                    message=c.message.strip(),
                )
            )
    except GitCommandError as e:
        raise RuntimeError(f"git log failed for {filepath}: {e}") from e
    return [c.model_dump() for c in commits]


@mcp.tool()
def get_file_content(repo: str, filepath: str, ref: str = "main") -> dict:
    """Return the exact contents of `filepath` at `ref` (default `main`) via
    `git show`, including the trailing newline. `found=False` if the path
    doesn't exist at that ref."""
    r = _open_repo(repo)
    try:
        content = r.git.show(f"{ref}:{filepath}", strip_newline_in_stdout=False)
        return {"filepath": filepath, "content": content, "found": True}
    except GitCommandError as e:
        return {"filepath": filepath, "content": "", "found": False, "error": str(e)}


@mcp.tool()
def create_isolated_branch(repo: str, base_branch: str, new_branch: str) -> bool:
    """Create `new_branch` off `base_branch` in its own git worktree. The
    repo's main working tree is left exactly as it was."""
    r = _open_repo(repo)
    local_path = Path(r.working_tree_dir)

    base_ref = base_branch
    if r.remotes:  # only talk to a remote if one exists
        try:
            r.git.fetch("origin", base_branch)
            base_ref = f"origin/{base_branch}"
        except GitCommandError:
            pass

    wt = _worktree_path(local_path, new_branch)
    _remove_worktree(r, wt)  # leftovers from an earlier run of this incident
    try:
        if new_branch in [h.name for h in r.heads]:
            r.git.branch("-D", new_branch)
        wt.parent.mkdir(parents=True, exist_ok=True)
        r.git.worktree("add", "-b", new_branch, str(wt), base_ref)
        return True
    except GitCommandError as e:
        raise RuntimeError(f"Failed to create isolated branch {new_branch}: {e}") from e


@mcp.tool()
def apply_patch_and_test(repo: str, diff: str, test_cmd: str, branch: str = "") -> dict:
    """Apply `diff` and run `test_cmd`, never touching the repo's own checkout.

    - `branch` given: work in that branch's worktree (from
      create_isolated_branch) and COMMIT the patch there if tests pass.
    - `branch` empty: use a throwaway worktree at HEAD, deleted afterwards.
    """
    r = _open_repo(repo)
    local_path = Path(r.working_tree_dir)

    ephemeral = not branch
    if ephemeral:
        wt = Path(tempfile.mkdtemp(prefix="warden-eph-"))
        shutil.rmtree(wt)  # git worktree add wants a non-existent path
        r.git.worktree("add", "--detach", str(wt), "HEAD")
    else:
        wt = _worktree_path(local_path, branch)
        if not wt.exists():
            return TestResult(
                applied=False, passed=False, exit_code=None, stdout="", stderr="",
                apply_error=f"No worktree for branch {branch}; call create_isolated_branch first",
            ).model_dump()

    patch_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, newline="\n") as fh:
            fh.write(_normalize_diff(diff))
            patch_path = Path(fh.name)

        # --recount: LLMs routinely miscount hunk line numbers.
        # --ignore-whitespace: tolerate CRLF/LF differences on Windows.
        # --index: stage the change so it can be committed as-is.
        flags = ["--recount", "--ignore-whitespace", "--index"]
        check = _run(["git", "apply", "--check", *flags, str(patch_path)], wt)
        if check.returncode != 0:
            return TestResult(
                applied=False, passed=False, exit_code=None, stdout="", stderr="",
                apply_error=f"Patch failed dry-run check: {check.stderr.strip()}",
            ).model_dump()
        apply = _run(["git", "apply", *flags, str(patch_path)], wt)
        if apply.returncode != 0:
            return TestResult(
                applied=False, passed=False, exit_code=None, stdout="", stderr="",
                apply_error=f"Patch failed to apply: {apply.stderr.strip()}",
            ).model_dump()

        try:
            proc = subprocess.run(
                test_cmd, shell=True, cwd=str(wt), capture_output=True,
                text=True, timeout=600, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                applied=True, passed=False, exit_code=None, stdout="",
                stderr="test command timed out after 600s",
            ).model_dump()

        passed = proc.returncode == 0
        if passed and not ephemeral:
            commit = _run(["git", *_COMMIT_ID, "commit", "-m", f"Warden auto-fix ({branch})"], wt)
            if commit.returncode != 0:
                return TestResult(
                    applied=True, passed=False, exit_code=proc.returncode,
                    stdout=proc.stdout[-8000:], stderr=f"commit failed: {commit.stderr.strip()}",
                ).model_dump()

        return TestResult(
            applied=True, passed=passed, exit_code=proc.returncode,
            stdout=proc.stdout[-8000:], stderr=proc.stderr[-8000:],
        ).model_dump()
    finally:
        if patch_path:
            patch_path.unlink(missing_ok=True)
        if ephemeral:
            _remove_worktree(r, wt)


@mcp.tool()
def open_pull_request(repo: str, title: str, body: str, head: str, base: str) -> dict:
    """Push `head` and open a real GitHub PR via PyGithub. Without a token,
    a remote, or an `owner/name` repo, returns a labelled dry_run result
    instead of a fabricated URL."""
    r = _open_repo(repo)
    local_path = Path(r.working_tree_dir)

    pushed = False
    try:
        r.remotes.origin.push(refspec=f"{head}:{head}", force=True)
        pushed = True
    except Exception:
        pushed = False  # no remote / no push access / offline

    _remove_worktree(r, _worktree_path(local_path, head))  # branch + commit remain

    if not settings.github_token or "/" not in repo or repo.startswith("http") or Path(repo).exists():
        return PRUrl(
            url=f"file://{local_path.as_posix()}#branch={head} (dry-run: no GitHub token or repo isn't owner/name)",
            number=None,
            dry_run=True,
        ).model_dump()

    from github import Github, GithubException

    gh = Github(settings.github_token)
    try:
        gh_repo = gh.get_repo(repo)
        pr = gh_repo.create_pull(title=title, body=body, head=head, base=base)
        return PRUrl(url=pr.html_url, number=pr.number, dry_run=False).model_dump()
    except GithubException as e:
        return PRUrl(
            url=f"error creating PR: {e.data.get('message', str(e))} (pushed={pushed})",
            number=None,
            dry_run=True,
        ).model_dump()


if __name__ == "__main__":
    mcp.run()
"""Pre-pipeline git sync — fetch and pull before DPE starts.

Skips silently:
  - Not a git repo
  - No remote (local-only)
  - Already up-to-date

Pulls when fast-forward safe.  Fails with a clear, user-readable message
when the remote has diverged (merge conflict).
"""

import subprocess
from pathlib import Path


def git_sync_pre(project_root: str, policy: str = "skip") -> dict:
    """Fetch origin and fast-forward pull, when a config asks for it.

    Returns:
        {"synced": true/false, "action": "skip"|"up-to-date"|"pulled",
         "pulled": N, "error": "..."}

    ``policy`` is explicit and defaults to ``"skip"`` — no network. This tool
    reaches the network and moves a working tree, and until per-run isolation
    landed it was reaching neither: the ``$PROJECT_ROOT`` token every config
    passes it expanded to ``projects_base/<project_id>``, which for an
    existing-repo project is not the repository, so it took the "not a git
    repository" branch below and did nothing. Routing it at the real tree is a
    correctness fix; inheriting a fetch and a pull of somebody's repository as a
    side effect of that fix is not. So the effect is opted into by name
    (``policy: "pull"``), and a config that wants the old syncing behaviour still
    gets it, deliberately.

    A LINKED WORKTREE is refused whatever the policy says. A run worktree is
    pinned to the base SHA the run was created at; fetching and fast-forwarding
    it would move the run's own base underneath it mid-run, which is the one
    thing the pin exists to prevent. Detected structurally (``.git`` is a gitfile
    pointer, not a directory), not from a path convention.
    """
    if not project_root or not Path(project_root).is_absolute():
        # `Path("").resolve()` is the process CWD — for a hosted engine, the
        # server's own checkout, which `git fetch`/`git pull` below would then
        # operate on. Refuse instead of guessing.
        return {"synced": False, "action": "error",
                "error": f"git_sync_pre: project_root must be an absolute path "
                         f"(got {project_root!r}) — refusing to resolve against "
                         f"the process CWD"}
    root = Path(project_root).resolve()

    # ── Not a git repo → silent skip ──────────────────────────────────
    if not (root / ".git").exists():
        return {"synced": True, "action": "skip",
                "detail": "not a git repository"}

    # ── A pinned run worktree is never synced ─────────────────────────
    # `.git` is a FILE in a linked worktree (a gitfile pointing into the source
    # repository's admin dir). Checked before the policy, because this refusal
    # is not a preference.
    if (root / ".git").is_file():
        return {"synced": True, "action": "skip",
                "detail": "pinned run worktree — refusing to fetch or pull a "
                          "tree that is deliberately held at its base commit"}

    # ── Policy ────────────────────────────────────────────────────────
    if policy != "pull":
        return {"synced": True, "action": "skip",
                "detail": f"sync policy {policy!r}: no fetch or pull "
                          f"(set policy: \"pull\" to enable)"}

    # ── No remote → silent skip ───────────────────────────────────────
    r = _git(root, "remote")
    if not r.stdout.strip():
        return {"synced": True, "action": "skip",
                "detail": "no remote configured (local-only)"}

    # ── Fetch ─────────────────────────────────────────────────────────
    r = _git(root, "fetch", "origin")
    if r.returncode != 0:
        return {"synced": False, "action": "error",
                "error": "git fetch origin failed.  Check network and remote URL."}

    # ── Compare HEAD vs origin ────────────────────────────────────────
    branch = _current_branch(root)
    if not branch:
        return {"synced": True, "action": "skip",
                "detail": "detached HEAD — skipping sync"}

    remote_ref = f"origin/{branch}"

    # Does the remote branch exist?
    r = _git(root, "rev-parse", "--verify", remote_ref)
    if r.returncode != 0:
        return {"synced": True, "action": "skip",
                "detail": f"no remote tracking branch '{remote_ref}'"}

    local_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    remote_sha = _git(root, "rev-parse", remote_ref).stdout.strip()

    if local_sha == remote_sha:
        return {"synced": True, "action": "up-to-date"}

    # ── Check if fast-forward is safe ─────────────────────────────────
    r = _git(root, "merge-base", "--is-ancestor", "HEAD", remote_ref)
    can_ff = (r.returncode == 0)

    if not can_ff:
        # Remote has diverged — explicit failure with actionable message
        short_local = _git(root, "log", "--oneline", "-3", "HEAD").stdout.strip()
        short_remote = _git(root, "log", "--oneline", "-3", remote_ref).stdout.strip()

        return {
            "synced": False,
            "action": "conflict",
            "error": (
                "Remote has diverged from local — merge conflict would occur.\n"
                "Resolve manually before retrying the pipeline.\n"
                f"  Branch: {branch}\n"
                f"  Local HEAD:\n{_indent(short_local, '    ')}\n"
                f"  Remote origin/{branch}:\n{_indent(short_remote, '    ')}\n"
                "To fix:\n"
                "  git pull --rebase   # or\n"
                "  git merge origin/<branch>"
            ),
        }

    # ── Fast-forward safe → pull ──────────────────────────────────────
    r = _git(root, "pull", "--ff-only", "origin", branch)
    if r.returncode != 0:
        return {"synced": False, "action": "error",
                "error": f"git pull --ff-only failed:\n{r.stderr.strip()}"}

    # Count pulled commits
    count_r = _git(root, "rev-list", "--count", f"{local_sha}..HEAD")
    pulled = 0
    try:
        pulled = int(count_r.stdout.strip())
    except (ValueError, TypeError):
        pass

    return {"synced": True, "action": "pulled", "pulled": pulled}


# ── Helpers ──────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def _current_branch(repo: Path) -> str | None:
    r = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = r.stdout.strip()
    if branch == "HEAD":
        return None  # detached
    return branch


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())

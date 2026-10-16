"""Pre-pipeline git sync policy check.

Skips silently:
  - Not a git repo
  - Linked (pinned run) worktree
  - Policy other than "pull" (the default)
  - No remote (local-only)

``policy: "pull"`` against a repository with a remote is reported as
unavailable. Fetch and fast-forward run repository-configured transport,
credential, hook and checkout-filter programs, and the engine does not run
repository-selected programs in its own process (see closed_git). There is
no raw equivalent here and no fallback to native fetch/pull.
"""

from pathlib import Path

from skillflow import closed_git


def git_sync_pre(project_root: str, policy: str = "skip") -> dict:
    """Decide whether a config's pre-pipeline sync can run in the engine.

    Returns:
        {"synced": true/false, "action": "skip"|"unavailable", "error": "..."}

    ``policy`` is explicit and defaults to ``"skip"`` — no network. This tool
    reaches the network and moves a working tree, and until per-run isolation
    landed it was reaching neither: the ``$PROJECT_ROOT`` token every config
    passes it expanded to ``projects_base/<project_id>``, which for an
    existing-repo project is not the repository, so it took the "not a git
    repository" branch below and did nothing. Routing it at the real tree is a
    correctness fix; inheriting a fetch and a pull of somebody's repository as a
    side effect of that fix is not. So the effect is opted into by name
    (``policy: "pull"``). That opt-in no longer fetches or pulls inside the
    engine: it reports ``unavailable`` with ``synced: false`` so the config's
    failure route is taken visibly instead of silently skipping the sync.

    A LINKED WORKTREE is refused whatever the policy says. A run worktree is
    pinned to the base SHA the run was created at; fetching and fast-forwarding
    it would move the run's own base underneath it mid-run, which is the one
    thing the pin exists to prevent. Detected structurally (``.git`` is a gitfile
    pointer, not a directory), not from a path convention.
    """
    if not project_root or not Path(project_root).is_absolute():
        # `Path("").resolve()` is the process CWD — for a hosted engine, the
        # server's own checkout. Refuse instead of guessing.
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
    r = closed_git.run(root, "remote", check=False)
    if r.returncode == 0 and not r.stdout.strip():
        return {"synced": True, "action": "skip",
                "detail": "no remote configured (local-only)"}

    return {"synced": False, "action": "unavailable",
            "error": ("git_sync_pre policy \"pull\" is unavailable in the engine: git fetch/pull "
                      "would run repository-configured transport, credential, hook and "
                      "checkout-filter programs in the engine process, which the closed Git "
                      "policy does not allow. Nothing was fetched or changed. Sync this "
                      "checkout outside the engine before starting the pipeline.")}

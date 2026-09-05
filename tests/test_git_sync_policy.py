"""`git_sync_pre` must never move a tree out from under a pinned run, and must
not acquire a network effect as a side effect of being routed correctly.

Before per-run isolation the token `$PROJECT_ROOT` expanded to
`projects_base/<project_id>`, which for an existing-repo project is not the
repository at all — so this tool has been silently taking its "not a git
repository" skip. Routing it at the real tree is a correctness fix that would,
on its own, hand it a repository with a remote. The policy is therefore explicit
here rather than implied by the routing: no fetch/pull unless a config asks for
one, and never on a linked worktree, whose whole purpose is to stay at the base
SHA the run was pinned to.
"""

import subprocess
from pathlib import Path

from skillflow.tools.git_sync_pre.impl import git_sync_pre


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def test_default_policy_performs_no_network_call(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, check=True)

    calls = []
    import skillflow.tools.git_sync_pre.impl as impl
    real = subprocess.run

    def spy(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(impl.subprocess, "run", spy)
    res = git_sync_pre(project_root=str(repo))
    assert res["action"] == "skip"
    assert not any("fetch" in c for c in calls), f"fetched by default: {calls}"
    assert not any("pull" in c for c in calls), f"pulled by default: {calls}"


def test_explicit_policy_still_syncs_a_direct_checkout(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                   cwd=repo, check=True)
    res = git_sync_pre(project_root=str(repo), policy="pull")
    assert res["synced"] is True
    assert res["action"] in ("up-to-date", "pulled")


def test_a_linked_worktree_is_never_synced_even_when_asked(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                   cwd=repo, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "codex/run/X",
                    str(wt), base], cwd=repo, check=True)

    res = git_sync_pre(project_root=str(wt), policy="pull")
    assert res["synced"] is True
    assert res["action"] == "skip"
    assert "pinned" in res.get("detail", "").lower()
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                          capture_output=True, text=True).stdout.strip() == base

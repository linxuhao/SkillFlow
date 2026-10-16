"""Explicit output destinations. Code lives in one tree; artifacts keep publication.

The code journal holds names/commit ids, never source copies. It is durable across
reclaims and makes commits path-scoped. Git's index is not our staging directory:
we only use it at the final commit, never as an agent read/write overlay.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from skillflow import closed_git


def target_for(node, slot: str | None = None) -> str:
    target = getattr(node, "output_target", "artifact")
    if slot is not None:
        entry = (getattr(node, "output_fixed", {}) or {}).get(slot)
        if isinstance(entry, dict):
            target = entry.get("target", target)
    return target


def has_target(node, target: str) -> bool:
    fixed = getattr(node, "output_fixed", {}) or {}
    return (any(target_for(node, slot) == target for slot in fixed)
            if fixed else target_for(node) == target)


def uses_legacy_code_delivery(node) -> bool:
    """Detect copy-based agent delivery, not artifact publication itself."""
    def tools(value):
        if isinstance(value, dict):
            yield value.get("tool")
            for child in value.values():
                yield from tools(child)
        elif isinstance(value, list):
            for child in value:
                yield from tools(child)
    return (getattr(node, "step_type", "agent") == "agent"
            and bool({"repo_apply", "repo_delete"} & set(tools(getattr(node, "lifecycle", {})))))


def validate_output_targets(node) -> None:
    values = [target_for(node)] + [target_for(node, s) for s in node.output_fixed]
    if any(t not in ("artifact", "code") for t in values):
        raise ValueError(f"Step {node.id!r}: output.target must be artifact or code")
    if target_for(node) == "code" and node.output_fixed and has_target(node, "artifact"):
        raise ValueError("Mixed outputs must use default target=artifact with explicit code slots")
    if has_target(node, "code"):
        if node.step_type != "agent":
            raise ValueError("output.target=code is an agent output contract; tool steps own their effects explicitly")
        for slot, entry in node.output_fixed.items():
            if (target_for(node, slot) == "artifact" and
                    (entry if isinstance(entry, str) else entry.get("file")) == "code_changes.json"):
                raise ValueError("code_changes.json is reserved for the code output receipt")
            if target_for(node, slot) == "code" and isinstance(entry, dict):
                if entry.get("on_exists", "replace") != "replace":
                    raise ValueError(f"Code output {slot!r} cannot archive/append by on_exists")
        def tools(value):
            if isinstance(value, dict):
                if "tool" in value:
                    yield value["tool"]
                for v in value.values():
                    yield from tools(v)
            elif isinstance(value, list):
                for v in value:
                    yield from tools(v)
        if {"repo_apply", "repo_delete", "draft_promote"} & set(tools(node.lifecycle)):
            raise ValueError(f"Step {node.id!r}: code outputs are direct; remove copy/delete/promotion hooks")
        if any(marker in json.dumps(node.lifecycle) for marker in ("$STEP_TMP_DIR", "$STEP_DRAFT_DIR")):
            raise ValueError("Code lifecycle cannot refer to a staging directory")
        if target_for(node) == "code" and node.output_carry_forward:
            raise ValueError("Code output has no staging to carry_forward")


def code_path(root: Path, relative: str) -> Path:
    """Strict repo-relative jail; never normalize an escape into another write."""
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("A nonempty repo-relative file path is required")
    if "\\" in relative:
        raise ValueError("Code paths use forward slashes; backslashes are not canonical repo paths")
    rel = relative
    p = Path(rel)
    if not p.parts or "\x00" in rel or p.is_absolute() or ".." in p.parts or ".git" in p.parts or ":" in p.parts[0]:
        raise ValueError(f"Unsafe code output path: {relative!r}")
    root = root.resolve()
    result = (root / p).resolve()
    if result == root or root not in result.parents:
        raise ValueError(f"Code output escapes its worktree: {relative!r}")
    # A symlink, including an in-tree one, is not an independent output file.
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Code output traverses a symlink: {relative!r}")
    if result.is_dir():
        raise ValueError("Code mutations operate on files, not directories")
    return result


def code_write_path(root: Path, relative: str) -> Path:
    """Refuse undeliverable writes before content or ownership can change.

    Let Git consult its index and normal exclude sources: a tracked file stays
    writable even when an ignore rule matches its name. Verbose output also
    reports negated rules, which explicitly allow the path.
    """
    path = code_path(root, relative)
    relative = path.relative_to(root.resolve()).as_posix()
    try:
        probe = closed_git.run(root, "check-ignore", "--verbose", "-z", "--stdin",
                               input=relative + "\0", check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not check Git ignore status for code output {relative!r}: {exc}") from exc
    if probe.returncode == 1:
        return path
    if probe.returncode != 0:
        raise RuntimeError("Could not check Git ignore status for code output "
                           f"{relative!r}: {(probe.stderr or probe.stdout).strip()}")
    fields = probe.stdout.split("\0")
    if len(fields) != 5 or fields[-1] != "":
        raise RuntimeError(f"Invalid Git ignore response for code output {relative!r}")
    source, line, pattern, _ = fields[:4]
    if pattern.startswith("!"):
        return path
    raise ValueError(f"Code output is git-ignored and would not be delivered: {relative!r} "
                     f"(matched {source}:{line}: {pattern!r}). "
                     "Choose a non-ignored output path and retry in this step.")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".output-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """One-file replacement for direct binary outputs, not a staging tree."""
    import stat
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".code-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def git(root: Path, *args: str) -> str:
    """One Git command under the closed profile (see closed_git).

    The profile closes configuration, not commands: delivery itself uses only
    metadata commands here and raw-file publication in closed_git.
    """
    return closed_git.run(root, *args).stdout


class CodeOutput:
    """Metadata for the sole writer of a code step. No code staging or rollback.

    Git is reached only through closed_git: working bytes are read and
    committed raw, so a repository hook, filter, fsmonitor or signing program
    never runs in the engine while preparing, publishing or checking a step.
    """
    def __init__(self, root: Path, journal: Path):
        if not root or not root.is_absolute() or not root.is_dir():
            raise ValueError("output.target=code requires a resolver-owned code directory")
        self.root, self.journal = root.resolve(), journal

    def load(self) -> dict:
        data = json.loads(self.journal.read_text(encoding="utf-8"))
        if data.get("root") != str(self.root):
            raise RuntimeError("Code output journal belongs to another worktree")
        return data

    def _repo(self):
        return closed_git.open_repo(self.root)

    def _head(self, repo, data: dict | None) -> str:
        head = repo.head()
        if head is None:
            raise RuntimeError("git rev-parse failed: code worktree has no HEAD commit")
        if repo.top != self.root:
            raise RuntimeError(f"Code directory {self.root} is not its Git worktree top level ({repo.top})")
        # A commit recorded just before HEAD moved: adopt it, never re-commit.
        if data and data.get("pending_commit") and head == data["pending_commit"]:
            data["head_commit"] = head
            data.pop("pending_commit")
            atomic_json(self.journal, data)
        return head

    def prepare(self, run_id: str, instance: int, item: str | None, *, inherited: dict | None = None) -> None:
        previous = self.load() if self.journal.exists() else None
        with self._repo() as repo:
            head = self._head(repo, previous)
            if previous and previous.get("run_id") == run_id and previous.get("instance") == instance:
                if head not in (previous["base_commit"], previous.get("head_commit")):
                    raise RuntimeError("Code worktree HEAD changed outside this step")
                return
            # Never inherit another task's unfinished changes. Failure retains them.
            if repo.dirty(head):
                raise RuntimeError("New code step requires a clean worktree; pending changes were retained. "
                                   "Resume their owning step or explicitly recover them first.")
            if (previous and previous.get("run_id") == run_id
                    and previous.get("item") == item and head == previous["head_commit"]):
                # An immediate review revision keeps A -> C2, not just C1 -> C2.
                previous["instance"] = instance
                atomic_json(self.journal, previous)
                return
            # A NEW execution after intervening accepted code steps starts at the
            # current clean HEAD. Final verification and graph-level replans revisit
            # the same step id legitimately. Only a resumed SAME instance must stay
            # pinned to its old HEAD (guarded above); no dirty work is ever adopted.
            base, paths = head, []
            if inherited and not previous:
                recovery = inherited["recovery_commit"]
                base = inherited["base_commit"]
                repo.git(self.root, "merge-base", "--is-ancestor", recovery, head)
                repo.git(self.root, "merge-base", "--is-ancestor", base, recovery)
                paths = list(inherited.get("paths", []))
                for path in paths:
                    code_path(self.root, path)
        atomic_json(self.journal, {"run_id": run_id, "instance": instance,
                    "item": item, "root": str(self.root), "base_commit": base,
                    "head_commit": head, "paths": paths})

    def record(self, paths) -> None:
        data = self.load()
        for path in paths:
            resolved = code_path(self.root, path)
            rel = resolved.relative_to(self.root).as_posix()
            if rel not in data["paths"]:
                data["paths"].append(rel)
        atomic_json(self.journal, data)

    def assert_published(self, receipt: Path) -> None:
        """A later hook cannot change the candidate after its commit/receipt."""
        data = self.load()
        with self._repo() as repo:
            head = self._head(repo, data)
            if head != data["head_commit"]:
                raise RuntimeError("Code HEAD changed after delivery; candidate is not accepted")
            if repo.dirty(head):
                raise RuntimeError("Code changed after delivery; pending changes retained. "
                                   "Run mutating checks before code commit, in validation.")
        if not receipt.is_file():
            raise RuntimeError("Code delivery did not publish its change receipt")
        report = json.loads(receipt.read_text(encoding="utf-8"))
        if (report.get("commit") != data["head_commit"]
                or report.get("step_instance_id") != data["instance"]):
            raise RuntimeError("Code receipt does not identify this candidate")

    def commit(self, receipt: Path, message: str) -> dict:
        data = self.load()
        paths = sorted(set(data["paths"]))
        for path in paths:
            code_path(self.root, path)
        selected = set(paths)

        def admit(dirty, head):
            if head != data["head_commit"]:
                raise RuntimeError("Code worktree HEAD changed outside this step; refusing commit")
            unexpected = dirty - selected
            if unexpected:
                raise RuntimeError("Unreported code changes retained, not committed: " + ", ".join(sorted(unexpected)))
            for path in sorted(repo.ignored([p for p in paths if (self.root / p).exists()])):
                raise RuntimeError(f"Code output is git-ignored and would not be delivered: {path}")

        def pending(commit):
            data["pending_commit"] = commit
            atomic_json(self.journal, data)

        with self._repo() as repo:
            self._head(repo, data)
            result = repo.publish(selected.__contains__, message, before=admit, on_commit=pending)
            if result["committed"]:
                data["head_commit"] = result["commit"]
                data.pop("pending_commit", None)
                atomic_json(self.journal, data)
            # A metadata artifact replaces the old duplicate source tree.
            all_changed = list(filter(None, repo.git(
                self.root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z",
                data["base_commit"], data["head_commit"], "--", *paths).split("\0"))) if paths else []
        report = {"target": "code", "run_id": data["run_id"], "step_instance_id": data["instance"],
                  "base_commit": data["base_commit"], "commit": data["head_commit"],
                  "files": all_changed, "note": "Source files live in the run worktree."}
        atomic_json(receipt, report)
        return {"passed": True, "files": all_changed, "committed": result["committed"],
                "commit": data["head_commit"]}

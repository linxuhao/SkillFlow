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
    result = subprocess.run(["git", "--literal-pathspecs", *args], cwd=root, capture_output=True,
                            text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


class CodeOutput:
    """Metadata for the sole writer of a code step. No code staging or rollback."""
    def __init__(self, root: Path, journal: Path):
        if not root or not root.is_absolute() or not root.is_dir():
            raise ValueError("output.target=code requires a resolver-owned code directory")
        self.root, self.journal = root.resolve(), journal

    def load(self) -> dict:
        data = json.loads(self.journal.read_text(encoding="utf-8"))
        if data.get("root") != str(self.root):
            raise RuntimeError("Code output journal belongs to another worktree")
        return data

    def prepare(self, run_id: str, instance: int, item: str | None, *, inherited: dict | None = None) -> None:
        head = git(self.root, "rev-parse", "HEAD").strip()
        previous = self.load() if self.journal.exists() else None
        if previous and previous.get("run_id") == run_id and previous.get("instance") == instance:
            if head not in (previous["base_commit"], previous.get("head_commit")):
                raise RuntimeError("Code worktree HEAD changed outside this step")
            return
        # Never inherit another task's unfinished changes. Failure retains them.
        if git(self.root, "status", "--porcelain", "--untracked-files=all").strip():
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
            git(self.root, "merge-base", "--is-ancestor", recovery, head)
            git(self.root, "merge-base", "--is-ancestor", base, recovery)
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
        if git(self.root, "rev-parse", "HEAD").strip() != data["head_commit"]:
            raise RuntimeError("Code HEAD changed after delivery; candidate is not accepted")
        if git(self.root, "status", "--porcelain", "--untracked-files=all").strip():
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
        if git(self.root, "rev-parse", "HEAD").strip() != data["head_commit"]:
            raise RuntimeError("Code worktree HEAD changed outside this step; refusing commit")
        paths = sorted(set(data["paths"]))
        for path in paths:
            code_path(self.root, path)
        # NUL-delimited names, not porcelain slicing: spaces/unicode/renames work.
        dirty = set(filter(None, git(self.root, "diff", "--name-only", "-z", "HEAD").split("\0")))
        dirty.update(filter(None, git(self.root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
        unexpected = dirty - set(paths)
        if unexpected:
            raise RuntimeError("Unreported code changes retained, not committed: " + ", ".join(sorted(unexpected)))
        for path in paths:
            if (self.root / path).exists():
                probe = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=self.root)
                if probe.returncode == 0:
                    raise RuntimeError(f"Code output is git-ignored and would not be delivered: {path}")
                if probe.returncode not in (0, 1):
                    raise RuntimeError(f"Could not check ignore status for code output: {path}")
        changed = sorted(dirty & set(paths))
        if changed:
            git(self.root, "add", "-A", "--", *changed)
            git(self.root, "commit", "--only", "-m", message, "--", *changed)
            data["head_commit"] = git(self.root, "rev-parse", "HEAD").strip()
            atomic_json(self.journal, data)
        # A metadata artifact replaces the old duplicate source tree.
        all_changed = list(filter(None, git(self.root, "diff", "--name-only", "-z",
                             data["base_commit"], data["head_commit"], "--", *paths).split("\0"))) if paths else []
        report = {"target": "code", "run_id": data["run_id"], "step_instance_id": data["instance"],
                  "base_commit": data["base_commit"], "commit": data["head_commit"],
                  "files": all_changed, "note": "Source files live in the run worktree."}
        atomic_json(receipt, report)
        return {"passed": True, "files": all_changed, "committed": bool(changed),
                "commit": data["head_commit"]}

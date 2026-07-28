"""Check that the paths a step declared it would produce are actually there."""

from pathlib import Path


def _listing(parent: Path) -> str:
    """What IS in `parent`, directories included.

    Listing only `is_file()` entries hid the very thing that existed: a step wrote
    `pyproject.toml` and `src/word_freq/*.py`, and the failure message read
    "File not found: src ... Files present: pyproject.toml". The agent was told its
    directory was missing while looking straight at it, and rewrote the same tree
    four times before the run gave up.
    """
    if not (parent.exists() and parent.is_dir()):
        return f"Directory is empty or missing: {parent}"
    entries = sorted(
        (p.name + "/" if p.is_dir() else p.name) for p in parent.iterdir())
    return (f"Present: {', '.join(entries)}" if entries
            else f"Directory is empty: {parent}")


def _satisfied(p: Path) -> tuple[bool, str]:
    """Does `p` satisfy "the step produced this"? Returns (ok, why-not).

    A DIRECTORY counts. A step told to produce `src/word_freq` produces a
    directory, and rejecting it for not being a regular file failed correct steps —
    the check's job is "did the declared output appear", not "is it a file".
    An EMPTY directory does not count: that is the silent-wrote-nothing case this
    validation exists to catch, wearing a directory as a disguise.
    """
    if not p.exists():
        return False, "not found"
    if p.is_dir():
        return (True, "") if any(p.iterdir()) else (False, "is an empty directory")
    return True, ""


def file_exists(files: list[str], *, workspace_root: str = "") -> dict:
    root = Path(workspace_root)
    results = []
    all_passed = True

    for pattern in files:
        if "*" in pattern:
            # A glob asks "did anything matching this get written". `rglob` yields
            # directories too, and counting each one as a required FILE turned
            # `files: ["*"]` — the canonical "assert the step wrote something"
            # validation — into a guaranteed failure for any step that creates a
            # subdirectory. Match files only, and judge the pattern as a whole.
            matched = [f for f in root.rglob(pattern) if f.is_file()]
            if matched:
                results += [{"file": str(f.relative_to(root)), "passed": True,
                             "error_message": ""} for f in matched]
            else:
                all_passed = False
                results.append({
                    "file": pattern, "passed": False,
                    "error_message": (f"Nothing matching '{pattern}' was written. "
                                      f"{_listing(root)}")})
            continue

        f = root / pattern
        ok, why = _satisfied(f)
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        if ok:
            results.append({"file": rel, "passed": True, "error_message": ""})
        else:
            all_passed = False
            head = (f"Not found: {pattern}" if why == "not found"
                    else f"'{pattern}' {why}")
            results.append({
                "file": rel, "passed": False,
                "error_message": f"{head} (expected under {root}). {_listing(f.parent)}"})

    return {"all_passed": all_passed, "results": results}

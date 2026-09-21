"""Strict parser and exact-context transformation checks."""

import pytest

from skillflow.strict_patch import PatchError, parse_patch, updated_bytes


def patch(body):
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


def test_multiple_files_and_hunks():
    ops = parse_patch(
        patch(
            "*** Update File: x.py\n@@\n def a():\n-    return 1\n+    return 10\n@@\n def b():\n-    return 2\n+    return 20\n*** Add File: new.py\n+x = 3\n*** Delete File: old.py"
        )
    )
    assert [op.kind for op in ops] == ["Update", "Add", "Delete"]
    assert ops[1].content == "x = 3\n"
    old = b"def a():\n    return 1\n\n# untouched\ndef b():\n    return 2\n"
    assert (
        updated_bytes(old, ops[0])
        == b"def a():\n    return 10\n\n# untouched\ndef b():\n    return 20\n"
    )


@pytest.mark.parametrize(
    "old,new", [(b"x\n", b"y\n"), (b"x\r\n", b"y\r\n"), (b"x", b"y")]
)
def test_newline_preservation(old, new):
    (op,) = parse_patch(patch("*** Update File: x\n@@\n-x\n+y"))
    assert updated_bytes(old, op) == new


@pytest.mark.parametrize(
    "old,reason", [(b"z\n", "stale"), (b"x\nx\n", "ambiguous"), (b" x\n", "stale")]
)
def test_strict_matching(old, reason):
    (op,) = parse_patch(patch("*** Update File: x\n@@\n-x\n+y"))
    with pytest.raises(PatchError, match=reason):
        updated_bytes(old, op)


def test_failure_guidance_sends_the_caller_to_a_citation_not_to_more_copying():
    """Both dead ends used to be answered with "copy more of the original".

    That is the instruction that produced 13 reads and 0 writes: a caller told
    to widen its copied context must first go and re-read enough of the file to
    widen it. Reference mode is the answer to both failures, so the guidance
    has to point there — an agent does what the message tells it to do.
    """
    (op,) = parse_patch(patch("*** Update File: x\n@@\n-x\n+y"))
    with pytest.raises(PatchError) as stale:
        updated_bytes(b"z\n", op)
    assert "cite its sha in references" in str(stale.value)
    assert "copy it exactly" not in str(stale.value)

    with pytest.raises(PatchError) as ambiguous:
        updated_bytes(b"x\nx\n", op)
    assert "cite the range you mean in references" in str(ambiguous.value)
    assert "add unchanged surrounding lines" not in str(ambiguous.value)
    assert "shrink" not in str(ambiguous.value)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/config",
        "../escape",
        ".git/config",
        "sub/.git/config",
        "a/../b",
        "a//b",
        "C:/file",
        "a\\b",
    ],
)
def test_unsafe_paths(path):
    with pytest.raises(PatchError):
        parse_patch(patch(f"*** Add File: {path}\n+x"))


@pytest.mark.parametrize(
    "body",
    [
        "*** Move File: x",
        "*** Update File: x\n@@ -1,1 +1,1 @@\n-x\n+y",
        "*** Add File: a\n+x\n*** Add File: a\n+y",
        "*** Add File: a\n+x\n*** Add File: a/b\n+y",
        "*** Update File: x\n@@\n x",
    ],
)
def test_unsupported_or_invalid_syntax(body):
    with pytest.raises(PatchError):
        parse_patch(patch(body))


def test_line_matcher_agrees_with_reference_including_blanks_and_overlaps():
    import random

    from skillflow.strict_patch import Hunk, Operation

    rng = random.Random(12092026)
    for _ in range(500):
        original = [rng.choice(["", "a", "bb", " a"]) for _ in range(rng.randrange(15))]
        old = tuple(
            rng.choice(["", "a", "bb", " a"]) for _ in range(rng.randrange(1, 5))
        )
        matches = [
            i
            for i in range(len(original) - len(old) + 1)
            if tuple(original[i : i + len(old)]) == old
        ]
        op = Operation("Update", "x", (Hunk(old, ("replacement",)),))
        before = ("".join(line + "\n" for line in original)).encode()
        if len(matches) != 1:
            with pytest.raises(PatchError, match="stale|ambiguous"):
                updated_bytes(before, op)
        else:
            at = matches[0]
            expected = original[:at] + ["replacement"] + original[at + len(old) :]
            assert (
                updated_bytes(before, op)
                == "".join(line + "\n" for line in expected).encode()
            )

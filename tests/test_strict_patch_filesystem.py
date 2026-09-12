"""Real files, complete preflight, and explicitly reported partial I/O errors."""

import os
import stat

import pytest

from skillflow.strict_patch import apply_code_patch


def patch(body):
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


def test_batch_add_update_delete_preserves_unrelated_content_and_mode(tmp_path):
    (tmp_path / "old").write_bytes(b"old binary\x00\xff")
    source = tmp_path / "script"
    source.write_bytes(b"head\r\nx\r\ntail")
    source.chmod(0o755)
    got = apply_code_patch(
        patch(
            "*** Update File: script\n@@\n head\n-x\n+y\n*** Add File: nested/new\n+hello\n*** Delete File: old"
        ),
        tmp_path,
    )
    assert (
        got["applied"]
        and got["written"] == ["script", "nested/new"]
        and got["deleted"] == ["old"]
    )
    assert source.read_bytes() == b"head\r\ny\r\ntail"
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    assert (tmp_path / "nested/new").read_bytes() == b"hello\n"
    assert not (tmp_path / "old").exists()


@pytest.mark.parametrize(
    "body",
    [
        "*** Update File: x\n@@\n-stale\n+new",
        "*** Update File: missing\n@@\n-x\n+y",
        "*** Add File: x\n+collision",
        "*** Delete File: missing",
        "*** Add File: ../outside\n+unsafe",
        "*** Add File: .git/config\n+unsafe",
    ],
)
def test_later_invalid_operation_does_not_publish_earlier_add(tmp_path, body):
    (tmp_path / "x").write_bytes(b"x\n")
    got = apply_code_patch(patch("*** Add File: first\n+new\n" + body), tmp_path)
    assert got["error"] and got["phase"] == "preflight" and not got["partial"]
    assert got["written"] == [] and got["deleted"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x"]
    assert (tmp_path / "x").read_bytes() == b"x\n"


@pytest.mark.parametrize("kind", ["Add", "Update", "Delete"])
@pytest.mark.parametrize("parent", [False, True])
def test_symlinks_even_inside_the_root_are_refused(tmp_path, kind, parent):
    real = tmp_path / "real"
    real.mkdir()
    (real / "x").write_text("x\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real if parent else real / "x")
    name = "alias/x" if parent else "alias"
    body = f"*** {kind} File: {name}"
    if kind == "Add":
        body += "\n+y"
    if kind == "Update":
        body += "\n@@\n-x\n+y"
    got = apply_code_patch(patch("*** Add File: first\n+new\n" + body), tmp_path)
    assert got["error"] and not got["partial"] and not (tmp_path / "first").exists()
    assert (real / "x").read_text() == "x\n" and alias.is_symlink()


def test_empty_files_and_unambiguous_insertion(tmp_path):
    assert apply_code_patch(patch("*** Add File: empty"), tmp_path)["applied"]
    assert (tmp_path / "empty").read_bytes() == b""
    assert apply_code_patch(patch("*** Add File: empty\n+x"), tmp_path)["error"]
    assert apply_code_patch(patch("*** Update File: empty\n@@\n+hello"), tmp_path)[
        "applied"
    ]
    assert (tmp_path / "empty").read_bytes() == b"hello\n"
    assert apply_code_patch(patch("*** Update File: empty\n@@\n+guess"), tmp_path)[
        "error"
    ]


@pytest.mark.parametrize("content", [b"x\x00", b"\xff", b"x\r\ny\n", b"x\r"])
def test_unsupported_text_is_not_normalized_or_overwritten(tmp_path, content):
    (tmp_path / "x").write_bytes(content)
    got = apply_code_patch(patch("*** Update File: x\n@@\n-x\n+y"), tmp_path)
    assert got["error"] and (tmp_path / "x").read_bytes() == content


def test_partial_write_failure_reports_only_files_that_landed(tmp_path, monkeypatch):
    from skillflow import write_tools

    original = write_tools._write_output_text

    def fail_second(path, *args, **kwargs):
        if path.name == "second":
            raise OSError("injected disk error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(write_tools, "_write_output_text", fail_second)
    got = apply_code_patch(
        patch("*** Add File: first\n+one\n*** Add File: second\n+two"), tmp_path
    )
    assert got["error"] and got["partial"] and not got["applied"]
    assert (
        got["written"] == ["first"]
        and got["deleted"] == []
        and got["phase"] == "publish"
    )
    assert (tmp_path / "first").read_text() == "one\n" and not (
        tmp_path / "second"
    ).exists()


def test_partial_delete_failure_remains_visible(tmp_path, monkeypatch):
    original = os.unlink
    for name in ("first", "second"):
        (tmp_path / name).write_text(name)

    def fail_second(name, *args, **kwargs):
        if name == "second":
            raise OSError("injected deletion failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_second)
    got = apply_code_patch(
        patch("*** Delete File: first\n*** Delete File: second"), tmp_path
    )
    assert got["error"] and got["partial"] and got["deleted"] == ["first"]
    assert (
        not (tmp_path / "first").exists()
        and (tmp_path / "second").read_text() == "second"
    )


def test_special_file_is_refused_before_reading(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    got = apply_code_patch(patch("*** Delete File: pipe"), tmp_path)
    assert got["error"] and stat.S_ISFIFO((tmp_path / "pipe").stat().st_mode)


def test_hunks_are_based_on_original_and_may_not_overlap(tmp_path):
    (tmp_path / "x").write_text("a\nb\nc\n")
    got = apply_code_patch(
        patch("*** Update File: x\n@@\n-a\n+A\n b\n@@\n b\n-c\n+C"), tmp_path
    )
    assert "overlap" in got["error"] and (tmp_path / "x").read_text() == "a\nb\nc\n"


def test_changed_snapshot_does_not_overwrite_newer_file(tmp_path, monkeypatch):
    from skillflow import strict_patch as m

    (tmp_path / "x").write_text("old\n")
    original = m._snapshot
    calls = []

    def concurrent(root_fd, name):
        calls.append(name)
        if len(calls) == 2:
            (tmp_path / "x").write_text("newer\n")
        return original(root_fd, name)

    monkeypatch.setattr(m, "_snapshot", concurrent)
    got = m.apply_code_patch(patch("*** Update File: x\n@@\n-old\n+mine"), tmp_path)
    assert (
        got["error"]
        and not got["partial"]
        and (tmp_path / "x").read_text() == "newer\n"
    )


@pytest.mark.parametrize(
    "target,code,out",
    [
        ("artifact", "ABS", "ABS"),
        ("code", "", "ABS"),
        ("code", "relative", "ABS"),
        ("code", "ABS", ""),
        ("code", "ABS", "OTHER"),
    ],
)
def test_tool_refuses_wrong_or_missing_host_roots(tmp_path, target, code, out):
    from skillflow.tools.apply_patch.impl import apply_patch

    roots = {"ABS": str(tmp_path), "OTHER": str(tmp_path / "other")}
    got = apply_patch(
        patch("*** Add File: x\n+x"),
        project_root=roots.get(code, code),
        output_dir=roots.get(out, out),
        output_target=target,
    )
    assert got["error"] and not (tmp_path / "x").exists()

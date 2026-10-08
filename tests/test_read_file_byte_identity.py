"""Raw byte identity must come from the snapshot supplying the text."""
import hashlib
import io
from pathlib import Path

import pytest

from skillflow import citations
from skillflow.read_tools import make_read_tool_fns
from skillflow.strict_patch import apply_code_patch
from skillflow.tools.read_file.impl import read_file

RUN = "read-file-byte-identity"

def reader(root, native=False):
    if native:
        return lambda path, **kw: read_file(path, workspace_root=str(root), **kw)
    return make_read_tool_fns(
        [{"source_type": "repository", "mode": "tool"}],
        str(root), code_root=str(root), run_id=RUN)["read"]

def identity(result, data):
    assert result["file_byte_sha256"] == hashlib.sha256(data).hexdigest()
    assert result["byte_size"] == len(data)

@pytest.mark.parametrize("native", [False, True], ids=["unified", "native"])
@pytest.mark.parametrize("data", [
    b"first\nsecond\n", b"first\r\nsecond\r\n",
    "武虾 café\r\n".encode(), b"", b"no-final-newline",
    b"old-mac\rline\r", b"bad-utf8\xff\r\n",
], ids=["lf", "crlf", "unicode", "empty", "no-final-lf", "cr", "replacement"])
def test_literal_bytes_and_existing_text_behavior(tmp_path, native, data):
    path = tmp_path / "text.txt"
    path.write_bytes(data)
    expected = path.read_text(encoding="utf-8", errors="replace").splitlines()
    result = reader(tmp_path, native)("text.txt")
    identity(result, data)
    assert result["content"] == "\n".join(
        f"{i + 1}\t{line}" for i, line in enumerate(expected))
    assert result["total_lines"] == len(expected)

def test_partial_clipped_raw_outline_and_recovered_reads_name_complete_file(tmp_path):
    data = b"".join(b"x" * 100 + b"\r\n" for _ in range(3000))
    (tmp_path / "text.txt").write_bytes(data)
    read = reader(tmp_path)
    for options in ({}, {"start_line": 2, "end_line": 4},
                    {"raw": True, "start_line": 2, "end_line": 4},
                    {"outline": True}):
        identity(read("text.txt", **options), data)
    assert read("text.txt")["truncated_by"] == "characters"
    raw = read("text.txt", raw=True, start_line=2, end_line=4)
    assert raw["content"] == (b"x" * 100 + b"\r\n").decode() * 2
    recovered = read("wrong/text.txt")
    identity(recovered, data)
    assert recovered["resolved_from"] == "wrong/text.txt"
    identity(read("wrong/text.txt", outline=True), data)
    identity(read_file("text.txt", start_line=2, end_line=4,
                       workspace_root=str(tmp_path)), data)

def test_staging_snapshot_shadows_repo_and_explicit_repo_keeps_its_identity(tmp_path):
    repo, staging = tmp_path / "repo", tmp_path / "staging"
    repo.mkdir()
    staging.mkdir()
    (repo / "text.txt").write_bytes(b"repo\n")
    staged = "staged 武虾\r\n".encode()
    (staging / "text.txt").write_bytes(staged)
    read = make_read_tool_fns(
        [{"source_type": "repository", "mode": "tool"}],
        str(tmp_path), code_root=str(repo), step_tmp_dir=str(staging))["read"]
    identity(read("text.txt"), staged)
    assert read("text.txt")["source"] == "staging"
    identity(read("text.txt", source="repo"), b"repo\n")

@pytest.mark.parametrize("native", [False, True], ids=["unified", "native"])
def test_one_buffer_survives_file_replacement_after_read(tmp_path, monkeypatch, native):
    path = tmp_path / "text.txt"
    original, replacement = b"snapshot\r\n", b"later-and-longer\n"
    path.write_bytes(original)
    actual_open, opens = Path.open, []
    class ChangingBuffer(io.BytesIO):
        def read(self, *args):
            value = super().read(*args)
            with actual_open(path, "wb") as dest:
                dest.write(replacement)
            return value
    def open_once(self, mode="r", *args, **kwargs):
        if self == path:
            opens.append(mode)
            assert opens == ["rb"], "content and identity must share one byte read"
            return ChangingBuffer(original)
        return actual_open(self, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_once)
        result = reader(tmp_path, native)("text.txt")
    identity(result, original)
    assert result["content"] == "1\tsnapshot"
    assert path.read_bytes() == replacement

def test_errors_and_unreadable_files_have_no_byte_identity(tmp_path, monkeypatch):
    path = tmp_path / "text.txt"
    path.write_bytes(b"private")
    read = reader(tmp_path)
    for result in (read("missing.txt"), read("text.txt", source="unknown"),
                   read("../outside.txt")):
        assert "error" in result
        assert "file_byte_sha256" not in result and "byte_size" not in result
    actual_open = Path.open
    def refuse(self, *args, **kwargs):
        if self == path:
            raise PermissionError("unreadable snapshot")
        return actual_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", refuse)
    result = read("text.txt")
    assert "unreadable snapshot" in result["error"]
    assert "file_byte_sha256" not in result and "byte_size" not in result
    with pytest.raises(PermissionError):
        read_file("text.txt", workspace_root=str(tmp_path))

def test_byte_digest_never_authorizes_edit_and_stale_citation_still_refuses(tmp_path):
    citations.forget_run(RUN)
    path = tmp_path / "text.txt"
    original = b"first\r\nsecond\r\n"
    path.write_bytes(original)
    result = reader(tmp_path)("text.txt")
    identity(result, original)
    assert result["file_byte_sha256"] != result["citation"]["sha"]
    denied = apply_code_patch("", tmp_path, references=[{
        "file": "text.txt", "sha": result["file_byte_sha256"],
        "from_line": 1, "to_line": 1, "new_text": "forged"
    }], run_id=RUN)
    assert denied["applied"] is False
    assert path.read_bytes() == original
    replacement = b"external\nsecond\n"
    path.write_bytes(replacement)
    denied = apply_code_patch("", tmp_path, references=[{
        "file": "text.txt", "sha": result["citation"]["sha"], "new_text": "stale"
    }], run_id=RUN)
    assert denied["applied"] is False
    assert path.read_bytes() == replacement
    citations.forget_run(RUN)

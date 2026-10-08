"""Owned synthetic files exercise the generic read grant, before file bytes."""
import hashlib
import json
from pathlib import Path

import pytest

from skillflow.read_tools import make_read_tool_fns

SECRET_NAMES = [".env", ".env.local", ".git-credentials", ".netrc",
                ".aws/credentials", ".ssh/id_rsa", ".ssh/id_ed25519"]
PUBLIC_NAMES = ["ordinary.txt", ".gitignore", ".github/workflows/check.yml",
                ".env.example", ".env.sample", ".env.template",
                ".ssh/id_ed25519.pub", ".aws/config"]


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in SECRET_NAMES + PUBLIC_NAMES:
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"owned synthetic fixture\r\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("owned outside fixture")
    (repo / "escape.txt").symlink_to(outside)
    (repo / "ordinary-link.txt").symlink_to(repo / "ordinary.txt")
    (repo / "secret-alias.txt").symlink_to(repo / ".env")
    (repo / ".env.hidden").symlink_to(repo / "ordinary.txt")
    read = make_read_tool_fns(
        [{"source_type": "repository", "mode": "tool"}],
        str(tmp_path), code_root=str(repo), run_id="source-boundary")
    return repo, read


def refused(result):
    assert "error" in result
    assert not {"content", "outline", "citation", "file_byte_sha256",
                "byte_size"}.intersection(result)


@pytest.mark.parametrize("name", [".env", "escape.txt"])
def test_original_inherited_boundary(source, name):
    # Run these exact two controls against the frozen 1.5.86 source first.
    # Both fixtures are populated; an absent file cannot satisfy this check.
    _, tools = source
    refused(tools["read"](name))


@pytest.mark.parametrize("name", SECRET_NAMES + ["escape.txt", "secret-alias.txt",
                                                ".env.hidden"])
@pytest.mark.parametrize("options", [{}, {"raw": True}, {"outline": True}])
def test_refusal_precedes_byte_open_even_on_recovery(source, monkeypatch, name, options):
    repo, tools = source
    original_open = Path.open
    forbidden = {(repo / file).resolve() for file in SECRET_NAMES}
    forbidden.add((repo / "escape.txt").resolve())
    def guarded_open(self, *args, **kwargs):
        assert self.resolve() not in forbidden, "refused file bytes were opened"
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    refused(tools["read"](name, **options))
    refused(tools["read"]("wrong/" + Path(name).name, **options))


@pytest.mark.parametrize("name", PUBLIC_NAMES + ["ordinary-link.txt"])
def test_permitted_paths_and_recovery_keep_byte_identity_and_citation(source, name):
    repo, tools = source
    expected = (repo / name).read_bytes()
    before = {str(file): file.read_bytes() for file in repo.rglob("*") if file.is_file()}
    for path in (name, "wrong/" + Path(name).name):
        result = tools["read"](path, raw=True)
        assert result["content"] == expected.decode()
        assert result["file_byte_sha256"] == hashlib.sha256(expected).hexdigest()
        assert result["byte_size"] == len(expected)
        assert result["citation"]["sha"] != result["file_byte_sha256"]
        if path != name:
            assert result["resolved_from"] == path
        outline = tools["read"](path, outline=True)
        assert "outline" in outline and outline["byte_size"] == len(expected)
    assert before == {str(file): file.read_bytes() for file in repo.rglob("*") if file.is_file()}


def test_search_list_and_missing_path_hints_share_the_boundary(source, monkeypatch):
    repo, tools = source
    original_open = Path.open
    forbidden = {(repo / file).resolve() for file in SECRET_NAMES}
    forbidden.add((repo / "escape.txt").resolve())
    def guarded_open(self, *args, **kwargs):
        assert self.resolve() not in forbidden
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    expected = set(PUBLIC_NAMES + ["ordinary-link.txt"])
    assert {file["name"] for file in json.loads(tools["list"]())["files"]} == expected
    assert {match["file"] for match in tools["search"]("fixture")["matches"]} == expected
    for name in SECRET_NAMES + ["escape.txt", "secret-alias.txt", ".env.hidden"]:
        assert "error" in tools["search"]("fixture", path=name)
    missing = tools["read"]("missing.txt")
    assert all(set(layer["files"]) <= expected for layer in missing["available"])
    assert not tools["read"](".env", source="self").get("available_sources")


@pytest.mark.parametrize("path", ["../ordinary.txt", "wrong/../ordinary.txt",
                                  "/wrong/ordinary.txt"])
def test_unsupported_recovery_does_not_turn_traversal_into_a_read(source, path):
    _, tools = source
    refused(tools["read"](path))
    assert "error" in tools["search"]("fixture", path=path)


def test_unknown_and_all_inline_sources_stay_closed(source):
    repo, tools = source
    refused(tools["read"]("ordinary.txt", source="unknown"))
    assert not make_read_tool_fns(
        [{"source_type": "repository", "mode": "inline"}],
        str(repo), code_root=str(repo))


def test_staging_and_named_step_preserve_their_grants(tmp_path):
    repo, staging, step = (tmp_path / name for name in ("repo", "staging", "cfg/producer"))
    for root in (repo, staging, step):
        root.mkdir(parents=True)
        (root / "ordinary.txt").write_text(root.name)
    (staging / "in-root.txt").symlink_to(staging / "ordinary.txt")
    (step / "escape.txt").symlink_to(repo / "ordinary.txt")
    (staging / ".env").write_text("owned staging synthetic credential")
    tools = make_read_tool_fns(
        [{"source_type": "repository", "mode": "tool"},
         {"source_type": "step", "step_id": "producer", "mode": "tool"}],
        str(tmp_path), "cfg", code_root=str(repo), step_tmp_dir=str(staging))
    assert tools["read"]("ordinary.txt")["source"] == "staging"
    assert tools["read"]("ordinary.txt", source="repo")["content"] == "1\trepo"
    assert tools["read"]("in-root.txt")["content"] == "1\tstaging"
    assert tools["read"]("wrong/in-root.txt")["source"] == "staging"
    assert tools["read"]("ordinary.txt", source="step:producer")["content"] == "1\tproducer"
    refused(tools["read"]("escape.txt", source="step:producer"))
    refused(tools["read"](".env"))

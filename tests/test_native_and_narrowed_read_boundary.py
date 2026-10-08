"""Actual granted native route and narrowed source identity, with owned data."""
import hashlib
import json
from pathlib import Path

import pytest

import skillflow
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.read_tools import build_source_map, make_read_tool_fns
from skillflow.tool_loader import ToolLoader
from skillflow.tools.read_file.impl import read_file

BODY = b"OWNED_SYNTHETIC\r\n"


def refused(result):
    assert "error" in result, result
    assert not {"content", "outline", "citation", "file_byte_sha256", "byte_size"} & result.keys()


@pytest.fixture
def owned(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    for name in (".env", ".env.example", "ordinary.txt", ".aws/credentials", ".aws/config",
                 ".aws/.env.example", ".ssh/id_rsa", ".ssh/config", ".ssh/id_rsa.pub"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(BODY)
    (root / "secret-alias.txt").symlink_to(root / ".env")
    (root / ".env.alias").symlink_to(root / "ordinary.txt")
    (root / "ordinary-link.txt").symlink_to(root / "ordinary.txt")
    (root / ".aws/secret-alias.txt").symlink_to(root / ".aws/credentials")
    (root / ".ssh/secret-alias.txt").symlink_to(root / ".ssh/id_rsa")
    return root


@pytest.mark.parametrize("name", [".env", "secret-alias.txt"])
def test_actual_granted_native_route_refuses_secret(owned, tmp_path, name):
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"),
                   workspace_base=str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
                   code_path_resolver=lambda pid: str(owned))
    sf.register_agent_config("reader", tools=["read_file"])
    sf.register_graph(PipelineGraph(name="actual-native", begin="read", steps=[
        StepNode(id="read", agent_config="reader", context=[{"from": "repository", "mode": "tool"}],
                 transitions=[Transition(to=None)])]))
    run = sf.create_run("actual-native", project_id="owned-read-boundary")
    sf.start_run(run)
    sf.advance_run(run)
    claim = sf.claim_next_step(run)
    assert claim is not None
    assert {"read", "search", "list", "read_file"} <= claim.inputs["_tool_schemas"].keys()
    arguments = dict(run_id=run, step_id=claim.step_id,
                     step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
    refused(sf.execute_tool("read", {"path": name}, **arguments))
    for tool in ("read", "read_file"):
        public = sf.execute_tool(tool, {"path": ".env.example"}, **arguments)
        assert public["byte_size"] == len(BODY)
        assert public["file_byte_sha256"] == hashlib.sha256(BODY).hexdigest()
    refused(sf.execute_tool("read_file", {"path": name}, **arguments))


@pytest.mark.parametrize("directory,name", [(".aws", "credentials"), (".ssh", "id_rsa")])
@pytest.mark.parametrize("exact", [False, True], ids=["directory", "exact-file"])
def test_narrowed_source_still_knows_protected_parent(owned, directory, name, exact):
    selector = directory + "/" + name if exact else directory
    specs = [{"source_type": "workspace", "path": selector, "mode": "tool"}]
    smap = build_source_map(specs, str(owned))
    tools = make_read_tool_fns(specs, _smap=smap)
    source = "workspace:" + selector
    # The grant stays the selected directory (or exact file's existing parent).
    assert smap["named"][source] == [(source, str(owned / directory))]
    for path in (name, "wrong/" + name, "secret-alias.txt", "wrong/secret-alias.txt"):
        for options in ({}, {"outline": True}, {"raw": True}):
            refused(tools["read"](path, source=source, **options))
    assert "error" in tools["search"]("SYNTHETIC", source=source, path=name)
    assert name not in {f["name"] for f in json.loads(tools["list"](source=source))["files"]}
    assert name not in {f["file"] for f in tools["search"]("SYNTHETIC", source=source)["matches"]}
    assert "secret-alias.txt" not in {f["name"] for f in json.loads(tools["list"](source=source))["files"]}


@pytest.mark.parametrize("directory,name", [(".aws", "config"), (".ssh", "config"),
                                           (".ssh", "id_rsa.pub"), (".aws", ".env.example")])
@pytest.mark.parametrize("exact", [False, True], ids=["directory", "exact-file"])
def test_narrowed_neighbors_keep_metadata_citations_and_no_write(owned, directory, name, exact):
    selector = directory + "/" + name if exact else directory
    tools = make_read_tool_fns(
        [{"source_type": "workspace", "path": selector, "mode": "tool"}], str(owned),
        run_id="narrowed-positive")
    source = "workspace:" + selector
    for path in (name, "wrong/" + name):
        result = tools["read"](path, source=source, raw=True)
        assert result["content"] == BODY.decode()
        assert result["byte_size"] == len(BODY)
        assert result["file_byte_sha256"] == hashlib.sha256(BODY).hexdigest()
        assert result["citation"]["citable"] is True
        assert result["citation"]["sha"] != result["file_byte_sha256"]
    native = read_file(name, workspace_root=str(owned / directory))
    assert native["byte_size"] == len(BODY)
    assert native["file_byte_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert (owned / directory / name).read_bytes() == BODY


@pytest.mark.parametrize("root_name,name", [("", ".env"), ("", "secret-alias.txt"),
                                          ("", ".env.alias"), (".aws", "credentials"),
                                          (".ssh", "id_rsa")])
@pytest.mark.parametrize("root_argument", ["workspace_root", "step_tmp_dir", "step_dir"])
def test_native_policy_checks_each_root_before_open(owned, monkeypatch, root_name, name, root_argument):
    root = owned / root_name
    original = Path.open
    forbidden = {(owned / ".env").resolve(), (owned / ".aws/credentials").resolve(),
                 (owned / ".ssh/id_rsa").resolve()}
    def guard(self, *args, **kwargs):
        assert self.resolve() not in forbidden, "protected bytes opened"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guard)
    refused(read_file(name, **{root_argument: str(root)}))


def test_native_ordinary_in_root_link_and_staging_precedence_remain(owned, tmp_path):
    for name in ("ordinary.txt", "ordinary-link.txt", ".env.example"):
        assert read_file(name, workspace_root=str(owned))["byte_size"] == len(BODY)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ordinary.txt").write_bytes(b"staged\r\n")
    assert read_file("ordinary.txt", workspace_root=str(owned), step_tmp_dir=str(staging))["found_in"] == "project"
    candidate = read_file("ordinary.txt", workspace_root=str(owned), step_tmp_dir=str(staging), artifact_candidate=True)
    assert candidate["content"] == "1\tstaged"
    assert candidate["found_in"] == "artifact candidate"


@pytest.mark.parametrize("directory,name", [(".aws", "credentials"), (".ssh", "id_rsa")])
def test_alias_of_protected_parent_preserves_resolved_identity(owned, directory, name):
    alias = owned / (directory[1:] + "-alias")
    alias.symlink_to(owned / directory, target_is_directory=True)
    tools = make_read_tool_fns(
        [{"source_type": "workspace", "path": alias.name, "mode": "tool"}], str(owned))
    refused(tools["read"](name, source="workspace:" + alias.name))
    refused(read_file(name, workspace_root=str(alias)))

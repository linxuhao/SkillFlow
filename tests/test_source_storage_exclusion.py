import json

from skillflow.read_tools import make_read_tool_fns
from skillflow.source_visibility import iter_visible_source_paths
from skillflow.tools.dir_tree.impl import dir_tree
from skillflow.tools.list_tree.impl import list_tree


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".zvec-grep/index.zvec").mkdir(parents=True)
    (repo / ".zvec-grep/index.zvec/CURRENT").write_text("INTERNAL_ONLY_NEEDLE\n")
    (repo / ".zvec-grep/files.zvec").mkdir()
    (repo / ".zvec-grep/files.zvec/LOG").write_bytes(b"BIN\0INTERNAL_ONLY_NEEDLE")
    (repo / "nested/.zvec-grep/index.zvec").mkdir(parents=True)
    (repo / "nested/.zvec-grep/index.zvec/MANIFEST").write_text("INTERNAL_ONLY_NEEDLE\n")
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("REAL_SOURCE_NEEDLE = 1\n")
    (repo / ".github").mkdir()
    (repo / ".github/workflow.yml").write_text("VISIBLE_HIDDEN_SOURCE_NEEDLE\n")
    return repo


def test_internal_index_storage_is_pruned_but_dot_source_remains(tmp_path):
    repo = _repo(tmp_path)
    paths = [str(p.relative_to(repo)) for p in iter_visible_source_paths(repo)]
    assert not any(".zvec-grep" in p for p in paths)
    assert ".github/workflow.yml" in paths
    assert "src/main.py" in paths
    assert list(iter_visible_source_paths(repo / ".zvec-grep")) == []


def test_unified_lexical_search_and_list_exclude_nested_index_storage(tmp_path):
    repo = _repo(tmp_path)
    fns = make_read_tool_fns(
        [{"source_type": "repository", "mode": "tool"}],
        str(tmp_path), code_root=str(repo),
    )
    assert fns["search"]("INTERNAL_ONLY_NEEDLE")["matches"] == []
    assert [m["file"] for m in fns["search"]("REAL_SOURCE_NEEDLE")["matches"]] == ["src/main.py"]
    assert [m["file"] for m in fns["search"]("VISIBLE_HIDDEN_SOURCE_NEEDLE")["matches"]] == [".github/workflow.yml"]
    names = [f["name"] for f in json.loads(fns["list"]())["files"]]
    assert not any(".zvec-grep" in name for name in names)
    assert ".github/workflow.yml" in names


def test_native_tree_surfaces_exclude_internal_storage(tmp_path):
    repo = _repo(tmp_path)
    outputs = [
        list_tree(workspace_root=str(repo))["tree"],
        dir_tree(project_root=str(repo))["tree"],
    ]
    for output in outputs:
        assert ".zvec-grep" not in output
        assert ".github" in output
        assert "main.py" in output

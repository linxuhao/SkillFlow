"""carry_forward: a re-run must not silently delete what it did not rewrite.

Promotion is a REPLACE — `rmtree(step_dir)` then rename staging over it — while
the agent's workspace briefing describes staging and step output as LAYERED for
reading ("step output — files from previous retries", searched after staging)
and says nothing about the destructive write. An agent that has just been
rejected therefore re-emits only the files it changed, which is the reasonable
reading of what it was told, and the rest are deleted.

Live, 2026-08-26 (AItelier / jinyong-hud): a re-planned PM wrote 2 of 9 task
cards; promotion deleted the other 8 while tasks_manifest.json still named them.

carry_forward makes the two models agree: what you do not touch survives, what
you write is replaced, and DROPPING a file becomes an explicit `delete_{slot}`
call instead of an accident of omission.
"""
import pytest

from skillflow.graph import GraphResolver, PipelineGraph
from skillflow.write_tools import execute_delete, generate_write_tool_schemas

FIXED = {"task_card": {"file": "tasks/*.json"},
         "manifest": {"file": "tasks_manifest.json"}}


def _graph(carry: bool):
    return PipelineGraph._from_dict({
        "name": "g",
        "steps": [{
            "id": "3", "step_type": "agent", "agent_config": "pm",
            "output": {"mode": "content", "fixed": FIXED,
                       **({"carry_forward": True} if carry else {})},
            "transitions": [{"to": None}],
        }],
    })


# ── the flag ────────────────────────────────────────────────────────────────

def test_the_flag_is_off_unless_the_step_asks_for_it():
    """Blast radius: every existing content step must keep its exact semantics."""
    n = GraphResolver(_graph(False)).get_node("3")
    assert n.output_carry_forward is False


def test_the_flag_parses_from_the_output_block():
    n = GraphResolver(_graph(True)).get_node("3")
    assert n.output_carry_forward is True


def test_the_flag_survives_a_serialise_round_trip():
    """A composed/overlaid graph is re-serialised; a dropped flag would silently
    restore the destructive behaviour."""
    once = _graph(True)
    twice = PipelineGraph._from_dict(once.to_dict())
    assert GraphResolver(twice).get_node("3").output_carry_forward is True


# ── the delete verb ─────────────────────────────────────────────────────────

def _names(carry):
    return {t["name"] for t in
            generate_write_tool_schemas("content", FIXED, carry_forward=carry)}


def test_delete_is_not_offered_without_carry_forward():
    """Staging then holds only what this run wrote — there is nothing to drop,
    and offering the verb would invite deleting files that were never there."""
    assert "delete_task_card" not in _names(False)


def test_delete_is_offered_for_a_glob_slot_under_carry_forward():
    assert "delete_task_card" in _names(True)


def test_a_required_single_output_is_never_deletable():
    """tasks_manifest.json is the step's contract, not one of many items."""
    assert "delete_manifest" not in _names(True)


def test_the_other_verbs_are_unchanged_by_the_flag():
    assert _names(False) <= _names(True)
    assert _names(True) - _names(False) == {"delete_task_card"}


# ── the executor ────────────────────────────────────────────────────────────

def test_delete_removes_the_file_from_staging(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "dropped.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tasks" / "kept.json").write_text("{}", encoding="utf-8")
    res = execute_delete("task_card", FIXED, {"id": "dropped"}, str(tmp_path))
    assert res.get("deleted")
    assert not (tmp_path / "tasks" / "dropped.json").exists()
    assert (tmp_path / "tasks" / "kept.json").exists()


def test_deleting_something_that_is_not_there_is_an_error_not_a_noop(tmp_path):
    """A silent success would let an agent believe it dropped a task it did not."""
    (tmp_path / "tasks").mkdir()
    res = execute_delete("task_card", FIXED, {"id": "ghost"}, str(tmp_path))
    assert "error" in res


def test_a_single_output_slot_refuses_deletion(tmp_path):
    (tmp_path / "tasks_manifest.json").write_text("{}", encoding="utf-8")
    res = execute_delete("manifest", FIXED, {"id": "x"}, str(tmp_path))
    assert "error" in res
    assert (tmp_path / "tasks_manifest.json").exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", ""])
def test_delete_refuses_a_path_that_leaves_the_slot(tmp_path, bad):
    (tmp_path / "tasks").mkdir()
    res = execute_delete("task_card", FIXED, {"id": bad}, str(tmp_path))
    assert "error" in res

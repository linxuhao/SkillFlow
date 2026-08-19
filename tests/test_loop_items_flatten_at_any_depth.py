"""A loop manifest is flattened at ANY nesting depth, not exactly one level.

`execution_order` is normally a list of parallel waves — one level of nesting —
and the flatten unwrapped exactly one level. An LLM-authored manifest wrapped
every wave once more (`[[["a", "b"]], [["c"]]]`); the single unwrap left lists
in place and `set(items)` in `_resolve_loop` raised `TypeError: unhashable type:
'list'` on every scheduler tick, wedging the worker for four hours. These tests
pin the shapes that must all iterate, and pin that the sequence for shapes that
already worked is unchanged.
"""

import json

import pytest

from skillflow.core import _flatten_loop_items

from test_integration_configs import _drive_loop, _loop_prepare


# ── The shapes ────────────────────────────────────────────────────────────

def test_a_flat_list_is_left_exactly_as_it_is():
    assert _flatten_loop_items(["a", "b", "c"]) == ["a", "b", "c"]


def test_one_level_of_waves_still_flattens_as_it_always_did():
    # Today's normal case: groups sequential, items within a group parallel.
    assert _flatten_loop_items([["a", "b"], ["c"]]) == ["a", "b", "c"]


def test_two_levels_no_longer_leave_a_list_behind():
    # The production manifest that crashed advance_run.
    assert _flatten_loop_items(
        [[["fix_psycopg_adapters", "fix_databaseurl_bytes"]],
         [["fix_sqlite_pool_wiring"]],
         [["fix_suite_final_verification"]]]
    ) == ["fix_psycopg_adapters", "fix_databaseurl_bytes",
          "fix_sqlite_pool_wiring", "fix_suite_final_verification"]
    # Flat means hashable — the exact operation that raised.
    assert set(_flatten_loop_items([[["a"]], [["b"]]])) == {"a", "b"}


def test_mixed_depths_in_one_manifest_all_land_in_manifest_order():
    assert _flatten_loop_items(
        ["a", ["b"], [["c", "d"]], [[["e"]]]]) == ["a", "b", "c", "d", "e"]


def test_empty_inner_lists_contribute_nothing_and_never_route_wrong():
    assert _flatten_loop_items([]) == []
    assert _flatten_loop_items([[], [[]], []]) == []
    assert _flatten_loop_items([[], ["a"], [[]], [["b"]]]) == ["a", "b"]


def test_duplicates_survive_across_nesting_levels_exactly_as_when_flat():
    # De-duplication is the completed-set's job, not the flatten's: the item
    # sequence must be the manifest's, duplicates included.
    assert _flatten_loop_items(["a", "a"]) == ["a", "a"]
    assert _flatten_loop_items([["a"], [["a"], "b"], "b"]) == ["a", "a", "b", "b"]


def test_a_non_string_leaf_is_named_not_dropped():
    # Items name directories, so a number/dict/null leaf is a malformed manifest
    # rather than a nesting problem. Dropping it would run less work than the
    # manifest declares (silent under-run); raising would re-create the crash
    # loop. It is coerced to stable, hashable text and fails in its own body step.
    items = _flatten_loop_items(["a", 3, None, {"id": "x"}])
    assert len(items) == 4                       # nothing dropped
    assert items[0] == "a"
    assert all(isinstance(i, str) for i in items)
    assert set(items)                            # hashable
    assert items == _flatten_loop_items(["a", 3, None, {"id": "x"}])  # stable


def test_a_self_referential_list_terminates():
    inner: list = ["a"]
    inner.append(inner)
    assert _flatten_loop_items([inner, "b"]) == ["a", "b"]
    # An alias that is not a cycle is still visited both times.
    shared = ["x"]
    assert _flatten_loop_items([shared, shared]) == ["x", "x"]


# ── End to end: the run that used to crash every tick ─────────────────────

def test_a_doubly_nested_manifest_drives_the_loop_instead_of_crashing(
        sf_with_workspace):
    sf = sf_with_workspace
    run_id = _loop_prepare(sf, [[["alpha", "beta"]], [["gamma"]]])
    count, items = _drive_loop(sf, run_id)
    assert sf.get_run(run_id)["status"] == "completed"
    assert count == 3
    assert items == ["alpha", "beta", "gamma"]


def test_a_nested_items_cache_from_an_older_engine_does_not_crash(
        sf_with_workspace):
    """The cached manifest is flattened on read too.

    When the source file is unavailable the loop falls back to `items_json`,
    which a pre-fix engine wrote with the lists still in it — the fallback path
    fed `set(items)` the same unhashable shape. A wedged run must heal on the
    new engine, not re-raise.
    """
    sf = sf_with_workspace
    run_id = _loop_prepare(sf, [["alpha"], ["beta"]])
    sf.advance_run(run_id)                      # creates the loop_state row
    for f in sf._workspace.base_path.rglob("tasks_manifest.json"):
        f.unlink()                              # force the cache fallback
    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_loop_state SET items_json = ? WHERE run_id = ?",
            (json.dumps([[["alpha"]], [["beta"]]]), run_id))
    count, items = _drive_loop(sf, run_id)
    assert sf.get_run(run_id)["status"] == "completed"
    assert count == 2
    assert items == ["alpha", "beta"]

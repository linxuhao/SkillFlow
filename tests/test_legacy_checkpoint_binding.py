"""Synthetic retained legacy rows exercised through the real checkpoint API.

The positive API test is intentionally also the 1.5.97 baseline regression:
that release cannot read the exact historical prefixed-only pause schema.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import SkillFlowError
from test_independent_checkpoint_boundaries import graph, ready


LEGACY_KEYS = {"_run_id", "_step_id", "_timestamp", "graph_name", "label",
               "next_node", "project_id", "step_id"}


def legacy_pause(tmp_path):
    sf, run = ready(tmp_path)
    sf.advance_run(run)
    with sf._tx() as conn:
        event = conn.execute(
            "SELECT id,payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused'"
        ).fetchone()
        modern = json.loads(event['payload_json'])
        raw = json.dumps({key: modern[key] for key in LEGACY_KEYS}, indent=2)
        conn.execute('UPDATE skillflow_outbox SET payload_json=? WHERE id=?', (raw, event['id']))
    return sf, run, event['id'], raw


def phases(sf):
    return sf._conn.execute(
        "SELECT id,payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused' ORDER BY id"
    ).fetchall()


def assert_no_decision(sf, run):
    assert sf.get_run(run)['status'] == 'paused'
    assert not sf._conn.execute(
        "SELECT 1 FROM skillflow_outbox WHERE event_type='checkpoint_approved'"
    ).fetchone()


def test_legacy_ordinary_api_restores_review_then_explicit_approval_and_replay(tmp_path):
    sf, run, origin, raw = legacy_pause(tmp_path)
    before = sf.get_run(run)
    steps_before = [tuple(row) for row in sf._conn.execute('SELECT * FROM skillflow_steps')]
    checkpoint = sf.get_checkpoint(run)  # genuine baseline 1.5.97 RED
    token = checkpoint['expected_checkpoint']
    assert set(token) == {'step_instance_id', 'version', 'completion_seq', 'outputs_sha256'}
    assert checkpoint['outputs'] == {'plan': 'exact retained content'}
    assert checkpoint['next_node'] == 'implement'
    assert sf.get_run(run) == before
    assert [tuple(row) for row in sf._conn.execute('SELECT * FROM skillflow_steps')] == steps_before
    assert_no_decision(sf, run)
    events = phases(sf)
    assert len(events) == 2 and events[0]['id'] == origin and events[0]['payload_json'] == raw
    binding = json.loads(events[-1]['payload_json'])
    assert binding['expected_checkpoint'] == token
    assert binding['compatibility_binding'] == {
        'kind': 'legacy_pause_derived_binding_v1', 'source_event_id': origin,
        'source_payload_sha256': hashlib.sha256(raw.encode()).hexdigest(),
    }
    assert sf.get_checkpoint(run) == checkpoint and len(phases(sf)) == 2
    reopened = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
    assert reopened.get_checkpoint(run) == checkpoint and len(phases(reopened)) == 2
    assert reopened.approve_checkpoint(run, expected_checkpoint=token, decision_id='legacy-human') == 'implement'
    after = reopened.get_run(run)
    assert after['status'] == 'running'
    assert reopened.approve_checkpoint(run, expected_checkpoint=token, decision_id='legacy-human') == 'implement'
    assert reopened.get_run(run) == after
    with pytest.raises(SkillFlowError, match='collision'):
        reopened.approve_checkpoint(run, expected_checkpoint=dict(token, version=token['version'] + 1), decision_id='legacy-human')


@pytest.mark.parametrize('damage', ['missing', 'wrong-run', 'wrong-project', 'wrong-step', 'wrong-private-step',
                                    'wrong-next', 'wrong-graph', 'partial-modern', 'extra-key', 'bad-time',
                                    'missing-history', 'corrupt-history', 'newer-execution'])
def test_legacy_inconsistent_route_never_gets_a_binding(tmp_path, damage):
    sf, run, origin, raw = legacy_pause(tmp_path)
    with sf._tx() as conn:
        payload = json.loads(raw)
        if damage == 'missing':
            conn.execute('DELETE FROM skillflow_outbox WHERE id=?', (origin,))
        elif damage == 'missing-history':
            conn.execute('DELETE FROM skillflow_graph_versions')
        elif damage == 'corrupt-history':
            conn.execute("UPDATE skillflow_graph_versions SET digest='sha256:corrupt'")
        elif damage == 'newer-execution':
            conn.execute("INSERT INTO skillflow_steps(run_id,step_id,status) VALUES(?, 'plan', 'pending')", (run,))
        else:
            changes = {
                'wrong-run': {'_run_id': 'other-run'}, 'wrong-project': {'project_id': 'other-project'},
                'wrong-step': {'step_id': 'implement'}, 'wrong-private-step': {'_step_id': 'implement'},
                'wrong-next': {'next_node': 'plan'}, 'wrong-graph': {'graph_name': 'other-graph'},
                'partial-modern': {'graph_version': 1}, 'extra-key': {'unrecognized': True},
                'bad-time': {'_timestamp': 'yesterday'},
            }
            payload.update(changes[damage])
            conn.execute('UPDATE skillflow_outbox SET payload_json=? WHERE id=?', (json.dumps(payload), origin))
    count = len(phases(sf))
    with pytest.raises(SkillFlowError):
        sf.get_checkpoint(run)
    assert len(phases(sf)) == count
    assert_no_decision(sf, run)


@pytest.mark.parametrize('modern_first', [False, True])
def test_latest_logical_run_pause_governs_across_both_schemas(tmp_path, modern_first):
    sf, run, origin, raw = legacy_pause(tmp_path)
    if modern_first:
        sf.get_checkpoint(run)  # older modern binding must not conceal a newer legacy pause
        payload = json.loads(raw)
    else:
        payload = json.loads(raw)
        payload.update(run_id=run, graph_version=1)  # newer partial modern must not fall back
    payload['next_node'] = 'wrong-route'
    with sf._tx() as conn:
        conn.execute("INSERT INTO skillflow_outbox(event_type,payload_json,stream_target) VALUES('checkpoint_paused',?,'ui')", (json.dumps(payload),))
    with pytest.raises(SkillFlowError, match='routed'):
        sf.get_checkpoint(run)
    assert_no_decision(sf, run)


@pytest.mark.parametrize('before_binding', [False, True])
@pytest.mark.parametrize('same_version', [False, True])
def test_supported_repin_even_back_to_same_pin_cannot_rebind_legacy(tmp_path, before_binding, same_version):
    sf, run, _, _ = legacy_pause(tmp_path)
    token = None if before_binding else sf.get_checkpoint(run)['expected_checkpoint']
    old_pin = sf.get_run(run)['graph_version']
    if not same_version:
        sf.register_graph(graph('new graph'))
        sf.repin_run(run)
    sf.repin_run(run, old_pin)
    with pytest.raises(SkillFlowError, match='repin'):
        sf.get_checkpoint(run)
    if token:
        with pytest.raises(SkillFlowError, match='repin'):
            sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='stale-pin')
    assert_no_decision(sf, run)


def test_async_repin_is_durable_before_scheduled_notifications(tmp_path):
    async def exercise():
        sf, run, _, _ = legacy_pause(tmp_path)
        sf.repin_run(run)
        # No await: the subscriber task cannot have persisted an event yet.
        with pytest.raises(SkillFlowError, match='repin'):
            sf.get_checkpoint(run)
        assert sf._conn.execute("SELECT count(*) FROM skillflow_outbox WHERE event_type='run_repinned'").fetchone()[0] == 1
        if sf.notifications._bg_tasks:
            await asyncio.gather(*sf.notifications._bg_tasks)
        assert sf._conn.execute("SELECT count(*) FROM skillflow_outbox WHERE event_type='run_repinned'").fetchone()[0] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('damage', ['outputs', 'version', 'original-event'])
def test_changed_retained_work_or_provenance_refuses_issued_identity(tmp_path, damage):
    sf, run, origin, _ = legacy_pause(tmp_path)
    token = sf.get_checkpoint(run)['expected_checkpoint']
    with sf._tx() as conn:
        if damage == 'outputs':
            conn.execute('UPDATE skillflow_steps SET outputs_json=? WHERE id=?', ('{"plan":"changed"}', token['step_instance_id']))
        elif damage == 'version':
            conn.execute('UPDATE skillflow_steps SET version=version+1 WHERE id=?', (token['step_instance_id'],))
        else:
            conn.execute('UPDATE skillflow_outbox SET payload_json=payload_json || ? WHERE id=?', (' ', origin))
    with pytest.raises(SkillFlowError):
        sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='stale-work')
    assert_no_decision(sf, run)


def test_rejection_and_new_instance_do_not_accept_the_old_identity(tmp_path):
    sf, run, _, _ = legacy_pause(tmp_path)
    token = sf.get_checkpoint(run)['expected_checkpoint']
    sf.reject_checkpoint(run, 'plan', 'Revise the plan')
    with pytest.raises(SkillFlowError):
        sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='old-rejected')
    claim = sf.claim_next_step(run)
    sf.confirm_step(claim.token, StepResult(outputs={'plan': 'revised'}))
    sf.advance_run(run)
    fresh = sf.get_checkpoint(run)['expected_checkpoint']
    assert fresh['step_instance_id'] != token['step_instance_id']
    with pytest.raises(SkillFlowError, match='Stale'):
        sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='old-revision')
    assert sf.approve_checkpoint(run, expected_checkpoint=fresh, decision_id='new-revision') == 'implement'


def test_two_first_readers_share_one_durable_binding(tmp_path):
    sf, run, _, _ = legacy_pause(tmp_path)
    barrier = threading.Barrier(2)
    def read():
        reader = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
        barrier.wait(timeout=10)
        return reader.get_checkpoint(run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(read) for _ in range(2)]
        first, second = [future.result(timeout=20) for future in futures]
    assert first == second
    assert len(phases(sf)) == 2
    assert_no_decision(sf, run)


def test_failed_binding_persistence_returns_no_identity_and_can_retry(tmp_path):
    sf, run, _, raw = legacy_pause(tmp_path)
    with sf._tx() as conn:
        conn.execute("CREATE TRIGGER refuse_binding BEFORE INSERT ON skillflow_outbox WHEN NEW.event_type='checkpoint_paused' BEGIN SELECT RAISE(ABORT, 'synthetic persistence failure'); END")
    with pytest.raises(sqlite3.DatabaseError, match='synthetic persistence failure'):
        sf.get_checkpoint(run)
    assert len(phases(sf)) == 1 and phases(sf)[0]['payload_json'] == raw
    assert_no_decision(sf, run)
    with sf._tx() as conn:
        conn.execute('DROP TRIGGER refuse_binding')
    assert sf.get_checkpoint(run)['outputs'] == {'plan': 'exact retained content'}
    assert len(phases(sf)) == 2


def test_modern_checkpoint_read_stays_read_only(tmp_path):
    sf, run = ready(tmp_path)
    sf.advance_run(run)
    sf._conn.execute('PRAGMA query_only=ON')
    try:
        checkpoint = sf.get_checkpoint(run)
        assert checkpoint['outputs'] == {'plan': 'exact retained content'}
        assert len(phases(sf)) == 1
    finally:
        sf._conn.execute('PRAGMA query_only=OFF')
    assert sf.approve_checkpoint(run, expected_checkpoint=checkpoint['expected_checkpoint'], decision_id='modern') == 'implement'


def test_changes_during_legacy_read_are_rechecked_before_binding(tmp_path, monkeypatch):
    sf, run, _, _ = legacy_pause(tmp_path)
    original_tx = sf._tx
    from contextlib import contextmanager
    @contextmanager
    def racing_tx():
        # The read-only pass has ended; a different engine repins before the
        # binding transaction starts. The second pass must refuse fresh state.
        other = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
        other.repin_run(run)
        with original_tx() as conn:
            yield conn
    monkeypatch.setattr(sf, '_tx', racing_tx)
    with pytest.raises(SkillFlowError, match='repin'):
        sf.get_checkpoint(run)
    assert len(phases(sf)) == 1
    assert_no_decision(sf, run)


def test_legacy_read_does_not_derive_authority_from_a_partial_modern_pause(tmp_path):
    sf, run, origin, raw = legacy_pause(tmp_path)
    with sf._tx() as conn:
        payload = json.loads(raw)
        payload['run_id'] = run  # only one modern key is still modern, never legacy
        conn.execute('UPDATE skillflow_outbox SET payload_json=? WHERE id=?', (json.dumps(payload), origin))
    with pytest.raises(SkillFlowError, match='routed'):
        sf.get_checkpoint(run)
    assert len(phases(sf)) == 1
    assert_no_decision(sf, run)

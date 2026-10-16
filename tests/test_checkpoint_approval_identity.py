"""Actual retained checkpoint identity, transaction and runner protocol controls."""
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import SkillFlowError
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.plugins.skill_runner.service import RunnerService


def make_flow(tmp_path, *, fixed=None, **kwargs):
    sf = SkillFlow(str(tmp_path / 'sf.db'), workspace_base=str(tmp_path / 'ws'),
                   artifact_history=False, **kwargs)
    sf.register_graph(PipelineGraph(name='gated', begin='plan', steps=[
        StepNode(id='plan', step_type='agent', checkpoint=True,
                 output_mode='content' if fixed else '', output_fixed=fixed or {},
                 transitions=[Transition(to='implement')]),
        StepNode(id='implement', step_type='agent')]))
    service = RunnerService(sf)
    first = service.start('gated', project_id='p')
    assert first['step'] == 'plan'
    return sf, service, first['run_id']


def completed(sf, run):
    return dict(sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=? AND status='completed' ORDER BY completion_seq DESC,id DESC LIMIT 1", (run,)).fetchone())


def expected(sf, run):
    row = completed(sf, run)
    out = json.loads(row['outputs_json'])
    return {'step_instance_id': row['id'], 'version': row['version'],
            'completion_seq': row['completion_seq'],
            'outputs_sha256': hashlib.sha256(json.dumps(out, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False).encode()).hexdigest()}


def pause(tmp_path, **kwargs):
    sf, svc, run = make_flow(tmp_path, **kwargs)
    answer = svc.submit(run, 'plan', {'plan': 'retained plan'})
    assert answer['status'] == 'paused'
    return sf, svc, run, expected(sf, run)


def approval_rows(sf):
    return [json.loads(x[0]) for x in sf._conn.execute("SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_approved' AND json_extract(payload_json,'$.decision_id') IS NOT NULL")]


def approved_events(sf):
    return [json.loads(x[0]) for x in sf._conn.execute("SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_approved'")]


def test_proxy_empty_result_retains_fixed_plan_before_confirm(tmp_path):
    sf, svc, run = make_flow(tmp_path, fixed={'plan': 'plan.md'})
    assert 'error' not in svc.execute_step_tool(run, 'plan', 'write_plan', {'content': '# exact plan\n'})
    answer = svc.submit(run, 'plan', {})
    assert answer['status'] == 'paused'
    assert json.loads(completed(sf, run)['outputs_json']) == {'plan': '# exact plan\n'}
    assert answer['outputs'] == {'plan': '# exact plan\n'}
    assert answer['expected_checkpoint'] == expected(sf, run)
    checkpoint = sf.get_checkpoint(run)
    assert checkpoint['outputs'] == answer['outputs']
    assert checkpoint['expected_checkpoint'] == answer['expected_checkpoint']
    published = sf._workspace.get_step_dir('p', 'gated', 'plan') / 'plan.md'
    published.write_text('later mutable file')
    assert sf.get_checkpoint(run)['outputs']['plan'] == '# exact plan\n'


@pytest.mark.parametrize('content', [{'accepted': ['α', False, None]}, [1, {'ok': True}]])
def test_json_fixed_slot_stays_structured(tmp_path, content):
    sf, svc, run = make_flow(tmp_path, fixed={'plan': 'plan.json'})
    svc.execute_step_tool(run, 'plan', 'write_plan', {'content': content})
    answer = svc.submit(run, 'plan', {})
    assert answer['status'] == 'paused'
    assert json.loads(completed(sf, run)['outputs_json'])['plan'] == content


@pytest.mark.parametrize('bad', ['missing', 'json', 'utf8', 'symlink'])
def test_missing_or_malformed_declared_slot_never_completes(tmp_path, bad):
    fixed = {'plan': 'plan.json' if bad == 'json' else 'plan.md'}
    sf, svc, run = make_flow(tmp_path, fixed=fixed)
    root = sf.output_directory(run, 'plan', 'plan'); root.mkdir(parents=True, exist_ok=True)
    path = root / fixed['plan']
    if bad == 'json': path.write_text('{broken')
    elif bad == 'utf8': path.write_bytes(b'\xff')
    elif bad == 'symlink':
        private = tmp_path / 'not-a-slot'; private.write_text('not retained')
        path.symlink_to(private)
    answer = svc.submit(run, 'plan', {})
    assert answer['status'] == 'in_progress'
    assert answer.get('validation_error')
    assert not sf._conn.execute("SELECT 1 FROM skillflow_steps WHERE run_id=? AND status='completed'", (run,)).fetchone()


def test_durable_exact_replay_and_collision_across_runs(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    assert sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='decision-a') == 'implement'
    assert len(approval_rows(sf)) == len(approved_events(sf)) == 1
    reopened = SkillFlow(str(tmp_path / 'sf.db'))
    assert reopened.approve_checkpoint(run, expected_checkpoint=dict(token), decision_id='decision-a') == 'implement'
    assert len(approval_rows(reopened)) == len(approved_events(reopened)) == 1
    changed = dict(token, outputs_sha256='0'*64)
    with pytest.raises(SkillFlowError, match='decision'):
        reopened.approve_checkpoint(run, expected_checkpoint=changed, decision_id='decision-a')
    with pytest.raises(SkillFlowError, match='decision'):
        reopened.approve_checkpoint('other-run', expected_checkpoint=token, decision_id='decision-a')
    assert approval_rows(reopened)[0]['expected_checkpoint'] == token
    event = approved_events(reopened)[0]
    assert event['decision_id'] == 'decision-a' and event['expected_checkpoint'] == token


@pytest.mark.parametrize('field', ['step_instance_id', 'version', 'completion_seq', 'outputs_sha256'])
def test_stale_expected_fields_refuse_without_effect(tmp_path, field):
    sf, svc, run, token = pause(tmp_path)
    token[field] = '0'*64 if field == 'outputs_sha256' else token[field] + 1
    before = sf.get_run(run)
    with pytest.raises(SkillFlowError):
        sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='stale')
    assert sf.get_run(run) == before and not approval_rows(sf) and not approved_events(sf)


def test_outputs_drift_refuses_old_digest(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    with sf._tx() as conn:
        conn.execute('UPDATE skillflow_steps SET outputs_json=? WHERE id=?', ('{"plan":"changed"}', token['step_instance_id']))
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='drift')
    assert sf.get_run(run)['status'] == 'paused' and not approval_rows(sf)


def test_reject_replan_cannot_approve_old_instance(tmp_path):
    sf, svc, run, old = pause(tmp_path)
    svc.reject(run, 'replan')
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=old, decision_id='old-running')
    svc.submit(run, 'plan', {'plan': 'new retained plan'})
    fresh = expected(sf, run)
    assert fresh['step_instance_id'] != old['step_instance_id']
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=old, decision_id='old-paused')
    assert sf.approve_checkpoint(run, expected_checkpoint=fresh, decision_id='fresh') == 'implement'
    assert len(approval_rows(sf)) == 1


def test_two_connections_race_one_actual_approval(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    peer = SkillFlow(str(tmp_path / 'sf.db'))
    ready = threading.Barrier(2)
    def approve(engine, name):
        ready.wait(timeout=10)
        try: return engine.approve_checkpoint(run, expected_checkpoint=token, decision_id=name)
        except SkillFlowError: return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: approve(*p), [(sf, 'a'), (peer, 'b')]))
    assert sorted(results) == ['implement', 'refused']
    assert len(approval_rows(sf)) == len(approved_events(sf)) == 1


def test_approval_and_rejection_race_cannot_both_apply(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    peer = SkillFlow(str(tmp_path / 'sf.db'))
    ready = threading.Barrier(2)
    def act(approve):
        ready.wait(timeout=10)
        try:
            if approve: sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='race')
            else: peer.reject_checkpoint(run, 'plan', 'redo')
            return True
        except SkillFlowError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes = list(pool.map(act, [True, False]))
    assert sum(outcomes) == 1
    assert len(approval_rows(sf)) == int(outcomes[0])


def test_transaction_rollback_includes_decision_and_event(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    sf._conn.execute("CREATE TRIGGER fail_approval BEFORE UPDATE ON skillflow_runs BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='retry')
    assert sf.get_run(run)['status'] == 'paused'
    assert not approval_rows(sf) and not approved_events(sf)
    sf._conn.execute('DROP TRIGGER fail_approval')
    sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='retry')
    assert len(approval_rows(sf)) == len(approved_events(sf)) == 1


def test_actual_pinned_graph_not_latest_registration(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    sf.register_graph(PipelineGraph(name='gated', begin='plan', steps=[StepNode(id='plan', step_type='agent')]))
    assert sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='pinned') == 'implement'


def test_runner_approve_requires_explicit_token_and_never_resume(tmp_path, monkeypatch):
    sf, svc, run, token = pause(tmp_path)
    def forbidden(*a, **k): raise AssertionError('generic resume called')
    monkeypatch.setattr(sf, 'resume_run', forbidden)
    before = sf.get_run(run)
    assert 'error' in svc.approve(run)
    assert sf.get_run(run) == before
    assert svc.approve(run, expected_checkpoint=token, decision_id='host-decision')['step'] == 'implement'
    assert len(approval_rows(sf)) == 1


def test_canonical_digest_ignores_object_key_order(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    with sf._tx() as conn: conn.execute('UPDATE skillflow_steps SET outputs_json=? WHERE id=?', (' { "plan" : "retained plan" } ', token['step_instance_id']))
    assert sf.get_checkpoint(run)['expected_checkpoint'] == token
    assert sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='canonical') == 'implement'


@pytest.mark.parametrize('token', [None, {}, {'step_instance_id': True, 'version': 1, 'completion_seq': 1, 'outputs_sha256': '0'*64}])
def test_missing_malformed_identity_is_not_approval(tmp_path, token):
    sf, svc, run, good = pause(tmp_path)
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='invalid')
    assert sf.get_run(run)['status'] == 'paused' and not approval_rows(sf)


def test_source_import_identity_same_pytest_process():
    import skillflow.core as core
    import skillflow.plugins.skill_runner.runner as runner
    import skillflow.plugins.skill_runner.service as service
    identities = {'core': core.__file__, 'runner': runner.__file__, 'service': service.__file__,
                  'approve_code': core.SkillFlow.approve_checkpoint.__code__.co_filename,
                  'confirm_code': core.SkillFlow.confirm_step.__code__.co_filename,
                  'service_approve_code': service.RunnerService.approve.__code__.co_filename}
    expected_root = Path(__file__).parents[1] / 'src' / 'skillflow'
    assert all(Path(v).resolve().is_relative_to(expected_root.resolve()) for v in identities.values())
    destination = Path('/evidence/import-identity.json')
    if destination.parent.is_dir(): destination.write_text(json.dumps(identities, indent=2)+'\n')


def test_service_exact_replay_preserves_next_live_claim(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    first = svc.approve(run, expected_checkpoint=token, decision_id='replay')
    svc.next(run)
    before = [tuple(x) for x in sf._conn.execute('SELECT * FROM skillflow_steps')]
    assert svc.approve(run, expected_checkpoint=token, decision_id='replay') == first
    assert [tuple(x) for x in sf._conn.execute('SELECT * FROM skillflow_steps')] == before


def test_outbox_ack_and_disabled_external_trace_preserve_decision(tmp_path):
    sf, svc, run, token = pause(tmp_path, trace_enabled=False, trace_db_path=str(tmp_path/'traces'))
    sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='ack')
    sf.ack_outbox([event.id for event in sf.drain_outbox()])
    sf.prune_trace(run)
    assert sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='ack') == 'implement'
    assert len(approval_rows(sf)) == 1


def test_already_approved_instance_cannot_be_reapproved_after_generic_pause(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='first')
    sf.pause_run(run)
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='second')
    assert len(approval_rows(sf)) == 1


def test_missing_decision_id_does_not_sample_or_approve(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=token)
    assert not approval_rows(sf) and sf.get_run(run)['status'] == 'paused'


def test_pending_newer_checkpoint_execution_refuses_old_retained_row(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    svc.reject(run, 'new plan')
    sf.pause_run(run)
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='superseded')
    assert not approval_rows(sf)


def test_same_decision_concurrent_replay_applies_once(tmp_path):
    sf, svc, run, token = pause(tmp_path)
    peer = SkillFlow(str(tmp_path/'sf.db'))
    ready = threading.Barrier(2)
    def approve(engine):
        ready.wait(timeout=10)
        return engine.approve_checkpoint(run, expected_checkpoint=token, decision_id='same')
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(approve, [sf, peer]))
    assert results == ['implement', 'implement'] and len(approval_rows(sf)) == 1


def test_carried_fixed_plan_is_captured_from_current_claim(tmp_path):
    from test_artifact_revision import engine, call, revise
    sf, run, claim = engine(tmp_path, fixed={'plan': 'plan.md', 'metadata': 'metadata.json'})
    assert 'error' not in call(sf, run, claim, 'write_plan', content='first')
    assert 'error' not in call(sf, run, claim, 'write_metadata', content={'keep': 7})
    claim = revise(sf, run, claim)
    assert 'error' not in call(sf, run, claim, 'edit_plan', old_str='first', new_str='second')
    sf.confirm_step(claim.token, StepResult()); sf.advance_run(run)
    assert sf.get_checkpoint(run)['outputs'] == {'plan': 'second', 'metadata': {'keep': 7}}


def test_checkpoint_mixed_code_slots_preserve_receipt_and_structured_values(tmp_path):
    from test_output_targets import init_repo, call, git, TOOLS
    from skillflow.tool_loader import ToolLoader
    root = init_repo(tmp_path/'repo')
    sf = SkillFlow(str(tmp_path/'mixed.db'), workspace_base=str(tmp_path/'artifacts'),
                   code_path_resolver=lambda pid, run_id=None: root, tool_loader=ToolLoader(TOOLS))
    node = StepNode(id='design', checkpoint=True, output_mode='content',
        output_fixed={'plan': 'plan.md', 'manifest': {'file': 'manifest.json', 'target': 'code'}},
        transitions=[Transition(to='done')])
    sf.register_graph(PipelineGraph(name='mixed', begin='design', steps=[node, StepNode(id='done',step_type='agent')]))
    run = sf.create_run('mixed', project_id='p'); sf.start_run(run); sf.advance_run(run)
    claim = sf.claim_next_step(run)
    assert 'error' not in call(sf, run, claim, 'write_plan', content='canonical plan')
    assert 'error' not in call(sf, run, claim, 'write_manifest', content={'safe': True})
    sf.confirm_step(claim.token, StepResult()); sf.advance_run(run)
    assert sf.get_checkpoint(run)['outputs'] == {'plan':'canonical plan', 'manifest':{'safe':True}}
    artifact = Path(claim.inputs['_artifact_dir'])
    assert (artifact/'code_changes.json').is_file()
    assert not (artifact/'manifest.json').exists()
    assert git(root, 'status', '--porcelain').strip() == ''


def test_subscriber_observes_committed_exact_event_once(tmp_path):
    import asyncio
    sf, svc, run, token = pause(tmp_path)
    seen = []
    async def subscriber(event):
        if event.event_type == 'checkpoint_approved':
            seen.append((sf.get_run(run)['status'], len(approval_rows(sf))))
    sf.notifications.subscribe(subscriber)
    async def approve():
        sf.approve_checkpoint(run, expected_checkpoint=token, decision_id='notify')
        await asyncio.gather(*sf.notifications._bg_tasks)
    asyncio.run(approve())
    assert seen == [('running', 1)] and len(approved_events(sf)) == 1


def test_ordinary_manual_pause_remains_visible_without_approval_token(tmp_path):
    sf, svc, run = make_flow(tmp_path)
    sf.pause_run(run)
    answer = svc.next(run)
    assert answer['status'] == 'paused' and not answer.get('expected_checkpoint')
    assert 'error' in svc.approve(run)
    assert sf.get_run(run)['status'] == 'paused'


def test_actual_cli_exposes_token_and_checked_replay_without_reclaim(tmp_path):
    import os, subprocess, sys
    sf, svc, run, token = pause(tmp_path, fixed={'plan': 'plan.md'})
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src'))
    program = ("import pathlib,runpy; import skillflow.core as c; "
               "assert pathlib.Path(c.__file__).resolve().is_relative_to(pathlib.Path(" +
               repr(str(Path(__file__).parents[1]/'src'/'skillflow')) + ").resolve()); "
               "runpy.run_module('skillflow.run_cli',run_name='__main__')")
    def cli(action, *extra):
        reply = subprocess.run([sys.executable, '-c', program, '--db', str(tmp_path/'sf.db'),
            '--workspace', str(tmp_path/'ws'), '--action', action, '--run-id', run, *extra],
            env=environment, text=True, capture_output=True, timeout=20)
        assert reply.returncode == 0, reply.stderr + reply.stdout
        return json.loads(reply.stdout)
    paused = cli('next')
    assert paused['expected_checkpoint'] == token and paused['outputs'] == {'plan': 'retained plan'}
    args = ['--expected-checkpoint', json.dumps(token), '--decision-id', 'cli-decision']
    receipt = cli('approve', *args)
    assert receipt['status'] == 'approved' and receipt['decision_id'] == 'cli-decision'
    assert cli('next')['step'] == 'implement'
    before = [tuple(x) for x in sf._conn.execute('SELECT * FROM skillflow_steps')]
    assert cli('approve', *args)['status'] == 'approved'
    assert [tuple(x) for x in sf._conn.execute('SELECT * FROM skillflow_steps')] == before

"""Approval-specific graph authority and actual routed phase, with real SQLite."""
import asyncio
import json
import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.exceptions import SkillFlowError
from test_independent_checkpoint_boundaries import ready, graph


@pytest.mark.parametrize('damage', ['missing', 'invalid_json', 'not_object', 'body', 'stored_digest', 'run_digest', 'unpinned'])
def test_unavailable_or_inconsistent_history_refuses_even_warm_cache(tmp_path, damage):
    sf, run = ready(tmp_path); sf.advance_run(run)
    token = sf.get_checkpoint(run)['expected_checkpoint']
    pin = sf.get_run(run)['graph_version']
    assert sf._get_resolver_for_run(run).get_node('plan').checkpoint
    before = sf.get_run(run)
    with sf._tx() as conn:
        if damage == 'missing': conn.execute('DELETE FROM skillflow_graph_versions WHERE name=? AND version=?', ('g',pin))
        elif damage in ('invalid_json', 'not_object', 'body'):
            text = '{' if damage == 'invalid_json' else '[]' if damage == 'not_object' else json.dumps(graph('altered').to_dict())
            conn.execute('UPDATE skillflow_graph_versions SET yaml_text=? WHERE name=? AND version=?',(text,'g',pin))
        elif damage == 'stored_digest': conn.execute('UPDATE skillflow_graph_versions SET digest=? WHERE name=? AND version=?',('sha256:'+'0'*64,'g',pin))
        elif damage == 'run_digest': conn.execute('UPDATE skillflow_runs SET graph_digest=? WHERE id=?',('sha256:'+'0'*64,run))
        else: conn.execute('UPDATE skillflow_runs SET graph_version=NULL,graph_digest=NULL WHERE id=?',(run,))
    for action in [lambda:sf.get_checkpoint(run), lambda:sf.approve_checkpoint(run,expected_checkpoint=token,decision_id='new')]:
        with pytest.raises(SkillFlowError,match='[Pp]inned'): action()
    assert sf.get_run(run)['status']=='paused'
    assert not sf._conn.execute("SELECT 1 FROM skillflow_outbox WHERE event_type='checkpoint_approved'").fetchone()


def test_committed_replay_survives_history_unavailability_and_event_ack(tmp_path):
    sf, run = ready(tmp_path); sf.advance_run(run)
    token = sf.get_checkpoint(run)['expected_checkpoint']
    assert sf.approve_checkpoint(run,expected_checkpoint=token,decision_id='committed')=='implement'
    with sf._tx() as conn: conn.execute('DELETE FROM skillflow_graph_versions WHERE name=?',('g',))
    sf.ack_outbox([event.id for event in sf.drain_outbox()])
    reopened=SkillFlow(str(tmp_path/'own.db'))
    before=reopened.get_run(run)
    assert reopened.approve_checkpoint(run,expected_checkpoint=token,decision_id='committed')=='implement'
    assert reopened.get_run(run)==before
    with pytest.raises(SkillFlowError,match='collision'):
        reopened.approve_checkpoint(run,expected_checkpoint=dict(token,version=token['version']+1),decision_id='committed')
    assert reopened._conn.execute("SELECT count(*) FROM skillflow_outbox WHERE event_type='checkpoint_approved'").fetchone()[0]==1


def test_routing_event_exists_before_async_notification_can_run(tmp_path):
    async def exercise():
        sf, run=ready(tmp_path)
        sf.advance_run(run)
        # No await has given NotificationBus scheduled tasks a turn yet.
        token=sf.get_checkpoint(run)['expected_checkpoint']
        phase=json.loads(sf._conn.execute("SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused'").fetchone()[0])
        assert phase['expected_checkpoint']==token and phase['next_node']=='implement'
        assert sf.approve_checkpoint(run,expected_checkpoint=token,decision_id='async')=='implement'
        if sf.notifications._bg_tasks: await asyncio.gather(*sf.notifications._bg_tasks)
        assert sf._conn.execute("SELECT count(*) FROM skillflow_outbox WHERE event_type='checkpoint_paused'").fetchone()[0]==1
    asyncio.run(exercise())


def test_manual_confirm_pause_never_creates_routing_event(tmp_path):
    sf, run=ready(tmp_path); sf.pause_run(run)
    with pytest.raises(SkillFlowError,match='routed'): sf.get_checkpoint(run)
    assert sf.get_run(run)['status']=='paused' and sf.get_run(run)['current_node'] is None
    assert not sf._conn.execute("SELECT 1 FROM skillflow_outbox WHERE event_type='checkpoint_paused'").fetchone()
    sf.resume_run(run); sf.advance_run(run)
    token=sf.get_checkpoint(run)['expected_checkpoint']
    assert sf.approve_checkpoint(run,expected_checkpoint=token,decision_id='after-routing')=='implement'


@pytest.mark.parametrize('field,value', [('next_node','other'),('graph_digest','wrong'),('expected_checkpoint',{})])
def test_phase_must_match_current_route_pin_and_execution(tmp_path,field,value):
    sf, run=ready(tmp_path); sf.advance_run(run)
    token=sf.get_checkpoint(run)['expected_checkpoint']
    with sf._tx() as conn:
        row=conn.execute("SELECT id,payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused'").fetchone()
        payload=json.loads(row['payload_json']); payload[field]=value
        conn.execute('UPDATE skillflow_outbox SET payload_json=? WHERE id=?',(json.dumps(payload),row['id']))
    with pytest.raises(SkillFlowError,match='routed'):
        sf.approve_checkpoint(run,expected_checkpoint=token,decision_id='inconsistent-phase')
    assert sf.get_run(run)['status']=='paused'


@pytest.mark.parametrize('transitions', [[], [Transition(to=None)]])
def test_agent_terminal_checkpoint_keeps_existing_completion_semantics(tmp_path,transitions):
    sf=SkillFlow(str(tmp_path/'terminal.db'))
    sf.register_graph(PipelineGraph(name='terminal',begin='final',steps=[StepNode(id='final',step_type='agent',checkpoint=True,transitions=transitions)]))
    run=sf.create_run('terminal'); sf.start_run(run); sf.advance_run(run)
    claim=sf.claim_next_step(run); sf.confirm_step(claim.token,StepResult(outputs={'plan':'end'})); sf.advance_run(run)
    assert sf.get_run(run)['status']=='completed'
    with pytest.raises(SkillFlowError): sf.get_checkpoint(run)
    assert not sf._conn.execute("SELECT 1 FROM skillflow_outbox WHERE event_type='checkpoint_paused'").fetchone()


def test_inline_tool_checkpoint_has_actual_durable_phase(tmp_path):
    from test_core import _stage_tool_loader, _tool_checkpoint_graph
    sf=SkillFlow(str(tmp_path/'tool.db')); _stage_tool_loader(sf)
    sf.register_graph(_tool_checkpoint_graph('inline'))
    run=sf.create_run('inline'); sf.start_run(run)
    claim=sf.claim_next_step(run); sf.confirm_step(claim.token,StepResult())
    sf.advance_run(run); sf.advance_run(run)
    checkpoint=sf.get_checkpoint(run)
    assert checkpoint['step_id']=='gate' and checkpoint['next_node']=='done'
    assert sf.approve_checkpoint(run,expected_checkpoint=checkpoint['expected_checkpoint'],decision_id='tool')=='done'
    sf.advance_run(run)
    assert sf.get_run(run)['status']=='running'


def test_supported_tool_terminal_checkpoint_keeps_end_condition(tmp_path):
    from test_core import _stage_tool_loader
    sf=SkillFlow(str(tmp_path/'tool-terminal.db')); _stage_tool_loader(sf)
    graph=PipelineGraph._from_dict({'name':'tool-end','begin':'final','steps':[{'id':'final','step_type':'tool','tool_name':'stage','checkpoint':True}],
        'end_conditions':{'combinator':'or','conditions':[{'type':'node_reached','node':'final','result':'completed','require_completed':True}]}})
    sf.register_graph(graph); run=sf.create_run('tool-end'); sf.start_run(run); sf.advance_run(run); sf.advance_run(run)
    assert sf.get_run(run)['status']=='completed'
    with pytest.raises(SkillFlowError): sf.get_checkpoint(run)


def test_fallback_route_cannot_become_pinned_by_restoring_history_later(tmp_path):
    from test_checkpoint_approval_identity import expected
    sf, run=ready(tmp_path)
    pin=sf.get_run(run)['graph_version']
    original=dict(sf._conn.execute('SELECT * FROM skillflow_graph_versions WHERE name=? AND version=?',('g',pin)).fetchone())
    sf.register_graph(graph('current fallback'))
    with sf._tx() as conn: conn.execute('DELETE FROM skillflow_graph_versions WHERE name=? AND version=?',('g',pin))
    reopened=SkillFlow(str(tmp_path/'own.db'))
    reopened.advance_run(run)  # existing execution fallback remains unchanged
    token=expected(reopened,run)
    phase=json.loads(reopened._conn.execute("SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert phase['graph_digest']!=original['digest']
    with reopened._tx() as conn:
        conn.execute('INSERT INTO skillflow_graph_versions(name,version,yaml_text,digest) VALUES(?,?,?,?)',
                     (original['name'],original['version'],original['yaml_text'],original['digest']))
    with pytest.raises(SkillFlowError,match='routed'):
        reopened.approve_checkpoint(run,expected_checkpoint=token,decision_id='restored')
    assert reopened.get_run(run)['status']=='paused'

import json
from pathlib import Path
import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.exceptions import SkillFlowError

def graph(label='original'):
    return PipelineGraph(name='g', begin='plan', steps=[
        StepNode(id='plan', step_type='agent', checkpoint=True, checkpoint_label=label,
                 transitions=[Transition(to='implement')]),
        StepNode(id='implement', step_type='agent')])

def ready(tmp_path):
    sf=SkillFlow(str(tmp_path/'own.db'), workspace_base=str(tmp_path/'own-ws'), artifact_history=False)
    sf.register_graph(graph())
    rid=sf.create_run('g', project_id='synthetic-owned-project')
    sf.start_run(rid); sf.advance_run(rid)
    claim=sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'plan':'exact retained content'}))
    return sf,rid

def test_supported_automatic_checkpoint_control(tmp_path):
    sf,rid=ready(tmp_path); sf.advance_run(rid)
    token=sf.get_checkpoint(rid)['expected_checkpoint']
    assert sf.approve_checkpoint(rid, expected_checkpoint=token, decision_id='normal')=='implement'
    assert sf.get_run(rid)['status']=='running'
    assert sf.approve_checkpoint(rid, expected_checkpoint=token, decision_id='normal')=='implement'

def test_missing_pinned_history_must_not_approve_using_current_graph(tmp_path):
    sf,rid=ready(tmp_path); sf.advance_run(rid)
    original=sf.get_checkpoint(rid); token=original['expected_checkpoint']; oldpin=sf.get_run(rid)['graph_version']
    sf.register_graph(graph('DIFFERENT CURRENT GRAPH'))
    # Owned fixture simulates missing pinned history (restore/history-loss), not production corruption.
    with sf._tx() as conn:
        conn.execute('DELETE FROM skillflow_graph_versions WHERE name=? AND version=?',('g',oldpin))
    # Fresh engine below has no cached pinned resolver.
    reopened=SkillFlow(str(tmp_path/'own.db'),workspace_base=str(tmp_path/'own-ws'),artifact_history=False)
    try:
        answer=reopened.approve_checkpoint(rid,expected_checkpoint=token,decision_id='missing-pin')
    except SkillFlowError:
        return
    print('COUNTEREXAMPLE',json.dumps({'original_pin':oldpin,'returned_next':answer,'after':reopened.get_run(rid),'event':[dict(row) for row in reopened._conn.execute("SELECT event_type,payload_json FROM skillflow_outbox WHERE event_type='checkpoint_approved'")]}))
    pytest.fail('approval accepted after exact pinned graph history was missing')

def test_manual_pause_before_checkpoint_routing_must_not_be_approval(tmp_path):
    sf,rid=ready(tmp_path)
    # Public core confirm completed the checkpoint; its advance_run has not routed/paused yet.
    assert sf.get_run(rid)['status']=='running'
    sf.pause_run(rid)
    try:
        token=sf.get_checkpoint(rid)['expected_checkpoint']
        answer=sf.approve_checkpoint(rid,expected_checkpoint=token,decision_id='manual')
    except SkillFlowError:
        return
    sf.advance_run(rid)
    print('COUNTEREXAMPLE',json.dumps({'approval_next':answer,'after_advance':sf.get_run(rid),'token':token}))
    pytest.fail('manual pause minted checkpoint approval before actual checkpoint routing')

def test_actual_source_identity():
    import skillflow.core as c
    for p in [c.__file__,c.SkillFlow.approve_checkpoint.__code__.co_filename,c.SkillFlow.confirm_step.__code__.co_filename]:
        assert Path(p).resolve().is_relative_to(Path(__file__).parents[1] / 'src' / 'skillflow')

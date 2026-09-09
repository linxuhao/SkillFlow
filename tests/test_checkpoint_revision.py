import json
import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.exceptions import SkillFlowError

@pytest.fixture
def flow(tmp_path):
 s=SkillFlow(str(tmp_path/'state.db'),workspace_base=str(tmp_path/'ws'))
 s.register_graph(PipelineGraph(name='revision',begin='a',steps=[StepNode(id='a',step_type='agent',checkpoint=True,transitions=[Transition(to='b')]),StepNode(id='b',step_type='agent')]))
 r=s.create_run('revision',project_id='p');s.start_run(r);s.advance_run(r)
 return s,r

def complete(s,r,c=None):
 c=c or s.claim_next_step(r);s.trace(r,'agent','prompt_delta',{'turn':1,'index':0,'role':'user','content':'old'},step_id='a',step_instance_id=c.token.step_instance_id)
 s.confirm_step(c.token,StepResult(outputs={'old':'retained'}));s.advance_run(r);return c

def test_new_identity_and_immutable_history(flow):
 s,r=flow;c=complete(s,r);old=dict(s._conn.execute('SELECT * FROM skillflow_steps WHERE id=?',(c.token.step_instance_id,)).fetchone());trace=s.get_trace(r,step_instance_id=c.token.step_instance_id)
 s.reject_checkpoint(r,'a','same');n=s.claim_next_step(r)
 assert n.token.step_instance_id!=c.token.step_instance_id
 assert dict(s._conn.execute('SELECT * FROM skillflow_steps WHERE id=?',(c.token.step_instance_id,)).fetchone())==old
 assert s.get_trace(r,step_instance_id=c.token.step_instance_id)==trace
 assert not any(x["event"]=="prompt_delta" for x in s.get_trace(r,step_instance_id=n.token.step_instance_id))
 assert 'same' in n.inputs['_feedback']
 complete(s,r,n);s.reject_checkpoint(r,'a','same');third=s.claim_next_step(r)
 assert len({c.token.step_instance_id,n.token.step_instance_id,third.token.step_instance_id})==3

def test_unstarted_redirect_is_adopted_atomically(flow):
 s,r=flow;complete(s,r)
 pending=s._conn.execute("SELECT id FROM skillflow_steps WHERE step_id='b'").fetchone()[0]
 s.reject_checkpoint(r,'a','start b',redirect_to='b');n=s.claim_next_step(r)
 assert n.step_id=='b' and n.token.step_instance_id==pending
 assert n.token.claim_epoch==1 and n.inputs['_rejection']=='start b'
 assert s._conn.execute("SELECT COUNT(*) FROM skillflow_steps WHERE step_id='b'").fetchone()[0]==1

@pytest.mark.parametrize('attempt', ['claimed','released','trace'])
def test_attempted_redirect_rolls_back(flow,attempt):
 s,r=flow;complete(s,r)
 if attempt=='trace':
  iid=s._conn.execute("SELECT id FROM skillflow_steps WHERE step_id='b'").fetchone()[0]
  s.trace(r,'agent','prompt_delta',{'index':0,'role':'user','content':'attempt'},step_id='b',step_instance_id=iid)
 else:
  s.approve_checkpoint(r);s.advance_run(r);c=s.claim_next_step(r)
  if attempt=='released':s.release_claim(c.token,'interrupted')
  s._conn.execute("UPDATE skillflow_runs SET status='failed' WHERE id=?",(r,));s._conn.commit()
 before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]
 run=dict(s.get_run(r));trace=s.get_trace(r)
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','new',redirect_to='b')
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]==before
 assert s.get_run(r)==run and s.get_trace(r)==trace

def test_invalid_redirect_rolls_back(flow):
 s,r=flow;complete(s,r)
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','new',redirect_to='missing')
 assert s.get_run(r)['status']=='paused'
 assert s._conn.execute("SELECT COUNT(*) FROM skillflow_steps WHERE step_id='a'").fetchone()[0]==1

def test_latest_feedback_survives_log_lag_and_reopen(flow,monkeypatch):
 s,r=flow;complete(s,r)
 monkeypatch.setattr(s,'_append_feedback_log',lambda *a:None)
 monkeypatch.setattr(s,'_read_feedback_log',lambda *a:'Previous instruction: do not remove X')
 s.reject_checkpoint(r,'a','remove X');n=s.claim_next_step(r)
 assert 'remove X' in n.inputs['_feedback']
 assert n.inputs['_resolved_context']['Latest checkpoint rejection']=='remove X'
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','remove X')

def test_redirect_new_target(flow):
 s,r=flow;c=complete(s,r);s.approve_checkpoint(r);s.advance_run(r)
 # A completed earlier maker is a valid redirect from a later checkpoint.
 b=s.claim_next_step(r);s.confirm_step(b.token,StepResult(outputs={}))
 s._conn.execute("UPDATE skillflow_runs SET status='failed' WHERE id=?",(r,));s._conn.commit()
 s.reject_checkpoint(r,'a','redo maker',redirect_to='a');n=s.claim_next_step(r)
 assert n.token.step_instance_id!=c.token.step_instance_id
 assert 'redo maker' in n.inputs['_feedback']


def test_redirect_earlier_maker_and_reopen(tmp_path):
 db=str(tmp_path/'redirect.db');s=SkillFlow(db)
 s.register_graph(PipelineGraph(name='redirect',begin='maker',steps=[StepNode(id='maker',step_type='agent',transitions=[Transition(to='gate')]),StepNode(id='gate',step_type='agent',checkpoint=True,transitions=[Transition(to='done')]),StepNode(id='done',step_type='agent')]))
 r=s.create_run('redirect');s.start_run(r);s.advance_run(r)
 old=s.claim_next_step(r);s.confirm_step(old.token,StepResult(outputs={'kept':True}));s.advance_run(r)
 gate=s.claim_next_step(r);s.confirm_step(gate.token,StepResult(outputs={}));s.advance_run(r)
 s.reject_checkpoint(r,'gate','same correction',redirect_to='maker')
 reopened=SkillFlow(db);n=reopened.claim_next_step(r)
 assert n.step_id=='maker' and n.token.step_instance_id!=old.token.step_instance_id
 assert 'same correction' in n.inputs['_feedback']
 iid=n.token.step_instance_id
 reopened.release_claim(n.token,'simulated process interruption')
 reclaimed=SkillFlow(db).claim_next_step(r)
 assert reclaimed.token.step_instance_id==iid
 assert reclaimed.token.claim_epoch>n.token.claim_epoch
 assert 'same correction' in reclaimed.inputs['_feedback']


@pytest.mark.parametrize('historical', [False, True])
def test_loop_revision_is_explicitly_refused_without_rewriting_item(tmp_path,historical):
 from pathlib import Path
 s=SkillFlow(str(tmp_path/'loop.db'),workspace_base=str(tmp_path/'ws'))
 s.register_agent_config('echo_agent',model='mock',tools=[])
 g=PipelineGraph.from_yaml(str(Path(__file__).parent/'fixtures/loop_step.yaml'))
 for node in g.steps:
  if node.id=='process_task':node.checkpoint=not historical
 s.register_graph(g);r=s.create_run(g.name,project_id='loop');s.start_run(r);s.advance_run(r)
 c=s.claim_next_step(r)
 d=s._workspace.get_step_tmp_dir('loop',g.name,'prepare');d.mkdir(parents=True,exist_ok=True)
 (d/'tasks_manifest.json').write_text(json.dumps({'execution_order':[['alpha','beta']]}))
 s.confirm_step(c.token,StepResult());s.advance_run(r)
 first=s.claim_next_step(r);assert first.step_id=='process_task'
 s.confirm_step(first.token,StepResult(outputs={'item':'alpha'}));s.advance_run(r)
 if historical:
  second=s.claim_next_step(r)
  assert s._conn.execute('SELECT loop_item FROM skillflow_steps WHERE id=?',(second.token.step_instance_id,)).fetchone()[0]=='beta'
  s.release_claim(second.token,'interruption on beta')
  s._conn.execute("UPDATE skillflow_runs SET status='failed' WHERE id=?",(r,));s._conn.commit()
 before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]
 loops=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_loop_state')]
 run=s.get_run(r);trace=s.get_trace(r)
 with pytest.raises(SkillFlowError,match='loop-body steps is not supported'):
  s.reject_checkpoint(r,'process_task','revise alpha')
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]==before
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_loop_state')]==loops
 assert s.get_run(r)==run and s.get_trace(r)==trace


def test_unstarted_redirect_transaction_failure_preserves_every_row(flow):
 s,r=flow;complete(s,r)
 before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]
 run=s.get_run(r);trace=s.get_trace(r)
 s._conn.execute("CREATE TRIGGER fail_revision BEFORE UPDATE ON skillflow_runs BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END")
 import sqlite3
 with pytest.raises(sqlite3.IntegrityError,match='injected transaction failure'):
  s.reject_checkpoint(r,'a','start b',redirect_to='b')
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]==before
 assert s.get_run(r)==run and s.get_trace(r)==trace


def test_reclaim_preserves_latest_without_feedback_growth(flow,monkeypatch):
 s,r=flow;complete(s,r)
 monkeypatch.setattr(s,'_append_feedback_log',lambda *a:None)
 monkeypatch.setattr(s,'_read_feedback_log',lambda *a:'Previous instruction: do not remove X')
 s.reject_checkpoint(r,'a','remove X');n=s.claim_next_step(r)
 context=n.inputs['_resolved_context'];feedback=n.inputs['_feedback']
 s.release_claim(n.token,'crash');n2=s.claim_next_step(r)
 assert n2.inputs['_feedback']==feedback
 assert n2.inputs['_resolved_context']['Latest checkpoint rejection']==context['Latest checkpoint rejection']=='remove X'


@pytest.mark.parametrize('trace_store', ['external', 'legacy_main'])
def test_external_trace_configuration_refuses_any_prior_attempt(tmp_path,trace_store):
 s=SkillFlow(str(tmp_path/'state.db'),workspace_base=str(tmp_path/'ws'),trace_db_path=str(tmp_path/'traces'))
 s.register_graph(PipelineGraph(name='external',begin='a',steps=[StepNode(id='a',step_type='agent',checkpoint=True,transitions=[Transition(to='b')]),StepNode(id='b',step_type='agent')]))
 r=s.create_run('external',project_id='p');s.start_run(r);s.advance_run(r);complete(s,r)
 iid=s._conn.execute("SELECT id FROM skillflow_steps WHERE run_id=? AND step_id='b'",(r,)).fetchone()[0]
 # Both supported storage routes are exercised through the production writer.
 writer=s if trace_store=='external' else SkillFlow(str(tmp_path/'state.db'))
 writer.trace(r,'agent','prompt_delta',{'index':0,'role':'user','content':'prior attempt'},step_id='b',step_instance_id=iid)
 assert len(writer.get_trace(r,step_instance_id=iid))==1
 main_before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_trace')]
 routed_before=s.get_trace(r)
 rows_before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]
 run_before=s.get_run(r)
 with pytest.raises(SkillFlowError,match='owned or previously attempted'):
  s.reject_checkpoint(r,'a','new instruction',redirect_to='b')
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]==rows_before
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_trace')]==main_before
 assert s.get_trace(r)==routed_before and s.get_run(r)==run_before


def stranded(flow):
 s,r=flow;complete(s,r);s.reject_checkpoint(r,'a','revise UI');c=s.claim_next_step(r)
 s.trace(r,'agent','prompt_delta',{'turn':5,'role':'assistant','content':'unfinished'},step_id='a',step_instance_id=c.token.step_instance_id)
 s.release_claim(c.token,'fixture simulates interrupted driver')
 # Reproduce the old host startup bug, not the recovery operation.
 with s._tx() as db: db.execute('UPDATE skillflow_runs SET current_node=NULL WHERE id=?',(r,))
 s.advance_run(r)
 run=s.get_run(r);assert run['status']=='paused'
 return s,r,c,dict(step_instance_id=c.token.step_instance_id,step_id='a',expected_current_node=run['current_node'],graph_version=run['graph_version'],graph_digest=run['graph_digest'])

def test_recover_stranded_revision_preserves_all_steps_and_transcript(flow):
 s,r,c,args=stranded(flow)
 rows=[dict(x) for x in s._conn.execute('SELECT * FROM skillflow_steps WHERE run_id=?',(r,))]
 trace=s.get_trace(r,step_instance_id=c.token.step_instance_id)
 assert s.recover_stranded_checkpoint_revision(r,**args)['current_node']=='a'
 assert [dict(x) for x in s._conn.execute('SELECT * FROM skillflow_steps WHERE run_id=?',(r,))]==rows
 assert s.get_trace(r,step_instance_id=c.token.step_instance_id)[:len(trace)]==trace
 assert s.advance_run(r)=='a'
 assert s.claim_next_step(r).token.step_instance_id==c.token.step_instance_id
 with pytest.raises(SkillFlowError):s.recover_stranded_checkpoint_revision(r,**args)

@pytest.mark.parametrize('field,value',[('step_instance_id',-1),('step_id','b'),('expected_current_node','other'),('graph_version',99),('graph_digest','wrong')])
def test_stranded_recovery_refuses_stale_target(flow,field,value):
 s,r,c,args=stranded(flow);before=s.get_run(r);args[field]=value
 with pytest.raises(SkillFlowError):s.recover_stranded_checkpoint_revision(r,**args)
 assert s.get_run(r)==before

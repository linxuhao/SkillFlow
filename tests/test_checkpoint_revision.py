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
 c=c or s.claim_next_step(r);s.trace(r,'agent','prompt_delta',{'turn':1,'message':{'role':'user','content':'old'}},step_id='a',step_instance_id=c.token.step_instance_id)
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

def test_conflict_rolls_back(flow):
 s,r=flow;c=complete(s,r);before=[tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','new',redirect_to='b')
 assert [tuple(x) for x in s._conn.execute('SELECT * FROM skillflow_steps')]==before
 assert s.get_run(r)['status']=='paused'

def test_invalid_redirect_rolls_back(flow):
 s,r=flow;complete(s,r)
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','new',redirect_to='missing')
 assert s.get_run(r)['status']=='paused'
 assert s._conn.execute("SELECT COUNT(*) FROM skillflow_steps WHERE step_id='a'").fetchone()[0]==1

def test_latest_feedback_survives_log_lag_and_reopen(flow,monkeypatch):
 s,r=flow;complete(s,r)
 monkeypatch.setattr(s,'_append_feedback_log',lambda *a:None)
 monkeypatch.setattr(s,'_read_feedback_log',lambda *a:'old feedback')
 s.reject_checkpoint(r,'a','new correction');n=s.claim_next_step(r)
 assert 'new correction' in n.inputs['_feedback']
 assert 'old feedback' in n.inputs['_feedback']
 with pytest.raises(SkillFlowError):s.reject_checkpoint(r,'a','new correction')

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

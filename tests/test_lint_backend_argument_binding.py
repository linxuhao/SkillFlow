"""Nonauthor public backend(Path) compatibility and authoritative-context controls."""
import ast,importlib.metadata,json,os
from pathlib import Path
import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph,StepNode,Transition
from skillflow.lint_backends import register_backend,_backends
from skillflow.step_validation import StepValidator
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager
TOOLS=Path(skillflow.__file__).parent/'tools'
@pytest.fixture(autouse=True)
def clean_registry():
 old=dict(_backends);yield;_backends.clear();_backends.update(old)
def fixture(root,backend,text='value = 1'):
 (root/'candidate.py').write_text(text);(root/'manifest.json').write_text('{".py":"independent"}')
 register_backend('independent',backend)
 return StepValidator(ToolLoader(TOOLS),root).validate([{'tool':'lint','files':['candidate.py'],'manifest_path':'manifest.json'}])
def parse(path,seen):
 seen.append(path)
 try:ast.parse(path.read_text());return {'passed':True,'error_message':''}
 except SyntaxError as error:return {'passed':False,'error_message':str(error)}
@pytest.mark.parametrize('parameter',['project_root','run_id','step_id'])
@pytest.mark.parametrize('valid',[True,False])
def test_legacy_path_backend_reserved_parameter_name_keeps_public_convention(tmp_path,parameter,valid):
 seen=[]
 def root(project_root):return parse(project_root,seen)
 def run(run_id):return parse(run_id,seen)
 def step(step_id):return parse(step_id,seen)
 backend={'project_root':root,'run_id':run,'step_id':step}[parameter]
 result=fixture(tmp_path,backend,'value = 1' if valid else 'def ((')
 assert seen==[tmp_path/'candidate.py'],result
 assert result['passed'] is valid,result



def test_bound_path_name_does_not_suppress_other_authoritative_keywords(tmp_path):
 seen=[]
 def backend(project_root,**kwargs):
  seen.append((project_root,kwargs));return {'passed':True}
 (tmp_path/'candidate.py').write_text('value=1');(tmp_path/'manifest.json').write_text('{".py":"independent"}')
 register_backend('independent',backend)
 result=StepValidator(ToolLoader(TOOLS),tmp_path,tool_context={'run_id':'own','step_id':'s','project_root':str(tmp_path)}).validate([
  {'tool':'lint','files':['candidate.py'],'manifest_path':'manifest.json','run_id':'forged','step_id':'forged','project_root':'/forged'}])
 assert result['passed'] is True
 assert seen==[(tmp_path/'candidate.py',{'run_id':'own','step_id':'s'})]


def test_positional_only_path_and_kwargs_can_share_a_keyword_name(tmp_path):
 seen=[]
 def backend(project_root,/,**kwargs):
  seen.append((project_root,kwargs));return {'passed':True}
 (tmp_path/'candidate.py').write_text('value=1');(tmp_path/'manifest.json').write_text('{".py":"independent"}')
 register_backend('independent',backend)
 context={'run_id':'own','step_id':'s','project_root':str(tmp_path)}
 result=StepValidator(ToolLoader(TOOLS),tmp_path,tool_context=context).validate([
  {'tool':'lint','files':['candidate.py'],'manifest_path':'manifest.json'}])
 assert result['passed'] is True
 assert seen==[(tmp_path/'candidate.py',context)]


def test_var_positional_path_does_not_consume_a_keyword_slot(tmp_path):
 seen=[]
 def backend(*run_id,**kwargs):
  seen.append((run_id,kwargs));return {'passed':True}
 (tmp_path/'candidate.py').write_text('value=1');(tmp_path/'manifest.json').write_text('{".py":"independent"}')
 register_backend('independent',backend)
 context={'run_id':'own','step_id':'s','project_root':str(tmp_path)}
 result=StepValidator(ToolLoader(TOOLS),tmp_path,tool_context=context).validate([
  {'tool':'lint','files':['candidate.py'],'manifest_path':'manifest.json'}])
 assert result['passed'] is True
 assert seen==[((tmp_path/'candidate.py',),context)]

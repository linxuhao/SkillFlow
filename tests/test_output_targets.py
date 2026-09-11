"""Behavioral migration contract: code has ONE tree; artifacts still publish."""
import json
import subprocess
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import CodeOutput, code_path, git
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

TOOLS = Path(__file__).parents[1] / 'src/skillflow/tools'


def init_repo(root):
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'migration-test')
    git(root, 'config', 'user.email', 'migration@test.invalid')
    (root / 'original.py').write_text('answer = 1\n')
    git(root, 'add', '--', 'original.py')
    git(root, 'commit', '-qm', 'baseline')
    return root


def engine(tmp_path, node=None, *, root=None, db=':memory:'):
    root = root or init_repo(tmp_path / 'repo')
    sf = SkillFlow(db)
    sf._tool_loader = ToolLoader(TOOLS)
    sf._workspace = WorkspaceManager(str(tmp_path / 'artifacts'),
                                    code_path_resolver=lambda pid, run_id=None: root)
    node = node or StepNode(id='implement', output_mode='write', output_target='code',
                           output_allow_full_write=True, context=[{'from': 'repository', 'mode': 'tool'}],
                           transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='g', begin=node.id, steps=[node]))
    rid = sf.create_run('g', {'project_id': 'p'}, project_id='p')
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    return sf, rid, claimed, root


def call(sf, rid, claim, name, **params):
    return sf.execute_tool(name, params, run_id=rid, step_id=claim.step_id,
                           step_instance_id=claim.token.step_instance_id,
                           claim_epoch=claim.token.claim_epoch)


def test_code_writes_reads_searches_same_tree_without_tmp(tmp_path, monkeypatch):
    sf, rid, claim, root = engine(tmp_path)
    assert claim.inputs['_output_target'] == 'code'
    assert claim.inputs['_output_dir'] == str(root)
    assert claim.inputs['_config_name'] == 'g'
    tmp = sf._workspace.get_config_path('p', 'g') / 'implement.tmp'
    assert not tmp.exists()
    # Any accidental code .tmp access after the claim is a test failure.
    def forbidden(*args, **kw):
        raise AssertionError('code must never ask for a staging folder')
    monkeypatch.setattr(sf._workspace, 'get_step_tmp_dir', forbidden)
    res = call(sf, rid, claim, 'edit', file='original.py', old_str='answer = 1', new_str='answer = 2')
    assert 'error' not in res, res
    assert res['path'] == 'original.py'
    assert (root / 'original.py').read_text() == 'answer = 2\n'
    got = call(sf, rid, claim, 'read', path='original.py')
    assert 'answer = 2' in str(got) and 'answer = 1' not in str(got)
    found = call(sf, rid, claim, 'search', pattern='answer = 2')
    assert 'original.py' in str(found)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'
    assert git(root, 'status', '--porcelain').strip() == ''
    artifacts = Path(claim.inputs['_artifact_dir'])
    assert (artifacts / 'code_changes.json').is_file()
    assert not (artifacts / 'original.py').exists()
    assert not tmp.exists()


def test_mixed_fixed_slots_never_copy_code_to_artifact_output(tmp_path):
    node = StepNode(id='design', output_mode='content', context=[{'from':'repository','mode':'tool'}],
        output_fixed={'plan': 'plan.md', 'manifest': {'file': 'manifest.json', 'target': 'code'}},
        validation=[{'tool':'file_exists','files':['plan.md','manifest.json']}],
        transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    assert 'error' not in call(sf,rid,claim,'write_plan',content='# Plan')
    assert 'error' not in call(sf,rid,claim,'write_manifest',content='{"safe":true}')
    assert (root / 'manifest.json').is_file()
    assert not (root / 'plan.md').exists()
    sf.confirm_step(claim.token, StepResult(outputs={},flags={}))
    artifacts = Path(claim.inputs['_artifact_dir'])
    assert (artifacts / 'plan.md').read_text() == '# Plan'
    assert (artifacts / 'code_changes.json').is_file()
    assert not (artifacts / 'manifest.json').exists()
    assert sf.get_steps(rid)[0]['status'] == 'completed'


def test_code_validation_fails_closed_and_retains_work(tmp_path):
    node = StepNode(id='implement',output_mode='write',output_target='code',
        output_allow_full_write=True, validation=[{'tool':'file_exists','files':['required.py']}],
        max_retries=0, transitions=[Transition(to=None)])
    sf,rid,claim,root=engine(tmp_path,node)
    head=git(root,'rev-parse','HEAD')
    call(sf,rid,claim,'write',file='partial.py',content='partial = True')
    sf.confirm_step(claim.token,StepResult(outputs={},flags={}))
    assert (root/'partial.py').is_file()
    assert git(root,'rev-parse','HEAD')==head
    assert sf.get_steps(rid)[0]['status'] != 'completed'
    assert not (sf._workspace.get_config_path('p','g') / 'implement.tmp').exists()


def test_code_glob_validation_is_scoped_to_candidate_files(tmp_path):
    node=StepNode(id='implement',output_mode='write',output_target='code',output_allow_full_write=True,
        validation=[{'tool':'lint','files':['*.py']}],transitions=[Transition(to=None)])
    sf,rid,claim,root=engine(tmp_path,node)
    call(sf,rid,claim,'write',file='nested/bad.py',content='def ! broken')
    result=sf._validate_outputs(claim.token,node)
    assert not result['passed'],result
    assert any('bad.py' in str(error) for error in result['errors']), result
    assert 'Tool not found' not in str(result)
    assert (root/'nested/bad.py').exists()


@pytest.mark.parametrize('raw',['../escape.py','/tmp/escape.py','.git/config','nested/../../escape','C:\\outside'])
def test_code_jail_rejects_escape(tmp_path,raw):
    root=init_repo(tmp_path/'repo')
    with pytest.raises(ValueError): code_path(root,raw)


def test_code_jail_rejects_symlink_parent(tmp_path):
    root=init_repo(tmp_path/'repo'); other=tmp_path/'other';other.mkdir()
    (root/'link').symlink_to(other,target_is_directory=True)
    with pytest.raises(ValueError):code_path(root,'link/escape.py')


def test_code_create_preserves_real_project_directory(tmp_path):
    sf,rid,claim,root=engine(tmp_path)
    res=call(sf,rid,claim,'write',file='project/real.py',content='real = True')
    assert 'error' not in res,res
    assert (root/'project/real.py').exists()
    assert not (root/'real.py').exists()


def test_unreported_change_prevents_cross_commit(tmp_path):
    sf,rid,claim,root=engine(tmp_path)
    base=git(root,'rev-parse','HEAD')
    call(sf,rid,claim,'write',file='ours.py',content='ours=True')
    (root/'unrelated.txt').write_text('do not absorb')
    sf.confirm_step(claim.token,StepResult(outputs={},flags={}))
    assert git(root,'rev-parse','HEAD')==base
    assert (root/'ours.py').is_file() and (root/'unrelated.txt').is_file()
    assert sf.get_steps(rid)[0]['status']!='completed'


def test_journal_reopen_retains_exact_paths_and_candidate(tmp_path):
    root=init_repo(tmp_path/'repo');state=tmp_path/'artifacts/step.json'
    code=CodeOutput(root,state);code.prepare('run',1,None)
    (root/'new name.py').write_text('x = 1');code.record(['new name.py'])
    recovered=CodeOutput(root,state);recovered.prepare('run',1,None)
    assert recovered.load()['paths']==['new name.py']
    recovered.commit(tmp_path/'artifacts/receipt.json','candidate')
    assert git(root,'status','--porcelain').strip()==''
    with pytest.raises(RuntimeError):
        (root/'pending.py').write_text('pending=True')
        recovered.prepare('run',2,'next-task')


def test_stopped_run_cannot_write_direct_code(tmp_path):
    sf,rid,claim,root=engine(tmp_path);sf.stop_run(rid,'test cancel')
    result=call(sf,rid,claim,'write',file='must_not_exist.py',content='bad')
    assert result.get('error'),result
    assert not (root/'must_not_exist.py').exists()


def test_target_round_trip_and_copy_hooks_are_rejected():
    graph=PipelineGraph._from_dict({'name':'g','begin':'s','steps':[{'id':'s','output':{'target':'code','mode':'write'}}]})
    assert PipelineGraph._from_dict(graph.to_dict()).steps[0].output_target=='code'
    with pytest.raises(ValueError):StepNode(id='s',output_target='cwd')
    with pytest.raises(ValueError):StepNode(id='s',output_target='code',lifecycle={'on_deliver':{'tool':'repo_apply'}})


def test_literal_git_paths_do_not_expand_into_unrelated_files(tmp_path):
    sf,rid,claim,root=engine(tmp_path)
    call(sf,rid,claim,'write',file='bracket[abc].py',content='literal=True')
    sf.confirm_step(claim.token,StepResult(outputs={},flags={}))
    receipt=json.loads((Path(claim.inputs['_artifact_dir'])/'code_changes.json').read_text())
    assert receipt['files']==['bracket[abc].py']
    assert git(root,'status','--porcelain').strip()==''


def test_same_task_review_revision_keeps_original_base(tmp_path):
    root=init_repo(tmp_path/'repo'); journal=tmp_path/'artifacts/journal.json'
    code=CodeOutput(root,journal);code.prepare('run',1,'card')
    base=code.load()['base_commit']
    (root/'original.py').write_text('answer = 2\n');code.record(['original.py'])
    code.commit(tmp_path/'one.json','first candidate')
    code.prepare('run',2,'card')
    assert code.load()['base_commit']==base
    (root/'more.py').write_text('more=True');code.record(['more.py'])
    code.commit(tmp_path/'two.json','review fix')
    report=json.loads((tmp_path/'two.json').read_text())
    assert report['base_commit']==base and set(report['files'])=={'original.py','more.py'}


def test_code_target_rejects_unsupported_or_ambiguous_shapes():
    with pytest.raises(ValueError,match='default target=artifact'):
        StepNode(id='s',output_target='code',output_fixed={'report':{'file':'r.json','target':'artifact'}})
    with pytest.raises(ValueError,match='agent output contract'):
        StepNode(id='s',step_type='tool',tool_name='x',output_target='code')


def test_ignored_output_cannot_be_reported_as_delivered(tmp_path):
    sf,rid,claim,root=engine(tmp_path)
    # An exclude rule is repo metadata, not an unrelated candidate source edit.
    (root/'.git/info/exclude').write_text('ignored.py\n')
    call(sf,rid,claim,'write',file='ignored.py',content='not_delivered=True')
    base=git(root,'rev-parse','HEAD')
    sf.confirm_step(claim.token,StepResult(outputs={},flags={}))
    assert (root/'ignored.py').exists()
    assert git(root,'rev-parse','HEAD')==base
    assert sf.get_steps(rid)[0]['status']!='completed'


def test_custom_partial_failure_retains_reported_code_paths(tmp_path):
    sf,rid,claim,root=engine(tmp_path)
    # Journal the successfully written subset even when the call also failed.
    (root/'partial.bin').write_bytes(b'\x00\xff')
    result=sf._record_code_result(rid,claim.step_id,{'written':['partial.bin'],'error':'second file failed'})
    assert result['error']=='second file failed'
    assert sf._code_output(rid,claim.step_id).load()['paths']==['partial.bin']


def test_recursive_code_validation_does_not_skip_root_files(tmp_path):
    node = StepNode(id='implement', output_mode='write', output_target='code',
                    output_allow_full_write=True,
                    validation=[{'tool': 'lint', 'files': ['**/*.py']}],
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    assert 'error' not in call(sf, rid, claim, 'write', file='bad.py', content='def ! broken')
    result = sf._validate_outputs(claim.token, node)
    assert not result['passed'], 'a root-level candidate must not disappear from **/*.py'


def test_new_execution_after_other_code_steps_uses_current_clean_baseline(tmp_path):
    root = init_repo(tmp_path / 'repo')
    code = CodeOutput(root, tmp_path / 'artifact/journal.json')
    code.prepare('run', 1, None)
    (root / 'README.md').write_text('first verification')
    code.record(['README.md'])
    code.commit(tmp_path / 'one.json', 'first verifier')
    # A later task accepted by the graph changes code before final verification
    # revisits this non-loop step. This is NOT a resumed stale claim.
    (root / 'original.py').write_text('answer = 2\n')
    git(root, 'add', '--', 'original.py')
    git(root, 'commit', '-qm', 'intervening implementation')
    current = git(root, 'rev-parse', 'HEAD').strip()
    code.prepare('run', 2, None)
    assert code.load()['base_commit'] == current
    assert code.load()['paths'] == []


def test_same_claim_still_refuses_external_head_movement(tmp_path):
    root = init_repo(tmp_path / 'repo')
    code = CodeOutput(root, tmp_path / 'artifact/journal.json')
    code.prepare('run', 1, None)
    (root / 'original.py').write_text('answer = 2\n')
    git(root, 'add', '--', 'original.py')
    git(root, 'commit', '-qm', 'external movement')
    with pytest.raises(RuntimeError, match='HEAD changed'):
        code.prepare('run', 1, None)


@pytest.mark.parametrize('raw', ['.', './', '././'])
def test_code_jail_reports_root_as_invalid_input(tmp_path, raw):
    root = init_repo(tmp_path / 'repo')
    with pytest.raises(ValueError):
        code_path(root, raw)


def test_mixed_post_delivery_checks_use_each_slots_real_destination(tmp_path):
    node = StepNode(id='design', output_mode='content',
                    output_fixed={'plan': 'plan.md', 'manifest': {'file': 'manifest.json', 'target': 'code'}},
                    lifecycle={'after_deliver': [{'tool': 'file_exists', 'files': ['plan.md', 'manifest.json']}]},
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    assert 'error' not in call(sf, rid, claim, 'write_plan', content='# Plan')
    assert 'error' not in call(sf, rid, claim, 'write_manifest', content='{"ok":true}')
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed', sf.get_steps(rid)


def test_mixed_recursive_validation_checks_root_code_slot(tmp_path):
    node = StepNode(id='design', output_mode='content',
                    output_fixed={'plan': 'plan.md', 'script': {'file': 'root.py', 'target': 'code'}},
                    validation=[{'tool': 'lint', 'files': ['**/*.py']}],
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    assert 'error' not in call(sf, rid, claim, 'write_plan', content='# Plan')
    assert 'error' not in call(sf, rid, claim, 'write_script', content='def ! broken')
    result = sf._validate_outputs(claim.token, node)
    assert not result['passed'], result
    assert any('root.py' in str(error) for error in result['errors']), result
    assert 'Tool not found' not in str(result)
    call(sf, rid, claim, 'write_script', content='answer = 42\n')
    assert sf._validate_outputs(claim.token, node)['passed']


def test_post_delivery_mutation_cannot_claim_a_clean_candidate(tmp_path):
    node = StepNode(id='implement', output_mode='write', output_target='code',
                    output_allow_full_write=True,
                    lifecycle={'after_deliver': [{'tool': 'late_mutator', 'files': ['new.py']}]},
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    def mutate(files, workspace_root=''):
        (Path(workspace_root) / 'new.py').write_text('answer = 999\n')
        return {'passed': True}
    sf._tool_loader.register_dynamic_tool('late_mutator', {}, mutate)
    call(sf, rid, claim, 'write', file='new.py', content='answer = 42\n')
    sf.confirm_step(claim.token, StepResult())
    assert sf.get_steps(rid)[0]['status'] != 'completed'
    assert (root / 'new.py').read_text() == 'answer = 999\n'  # retained, never rolled back
    assert 'answer = 42' in git(root, 'show', 'HEAD:new.py')


@pytest.mark.parametrize('legacy', [False, True])
def test_custom_tools_receive_host_owned_destinations_and_legacy_flag(tmp_path, legacy):
    node = StepNode(id='implement', output_mode='write',
                    output_target='artifact' if legacy else 'code',
                    config={'extra_tools': ['probe_destinations']},
                    lifecycle={'on_deliver': {'tool': 'repo_apply'}} if legacy else {},
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    def probe_destinations(*, output_dir='', output_target='', legacy_code_staging=False, **kwargs):
        return {'directory': output_dir, 'target': output_target, 'legacy': legacy_code_staging}
    sf._tool_loader.register_dynamic_tool('probe_destinations', {}, probe_destinations)
    result = call(sf, rid, claim, 'probe_destinations', output_dir='/wrong',
                  output_target='wrong', legacy_code_staging=not legacy)
    assert result['target'] == ('artifact' if legacy else 'code')
    assert result['legacy'] is legacy
    assert result['directory'] == claim.inputs['_output_dir']
    assert claim.inputs['_legacy_code_staging'] is legacy


def test_binary_output_replacement_is_atomic_and_preserves_mode(tmp_path, monkeypatch):
    import os
    import stat
    from skillflow.output_targets import atomic_write_bytes
    path = tmp_path / "asset.bin"
    path.write_bytes(b"original\x00\xff")
    path.chmod(0o755)
    replace = os.replace
    def fail_replace(*args):
        raise OSError("simulated interruption before replace")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write_bytes(path, b"new\xff\x00")
    assert path.read_bytes() == b"original\x00\xff"
    assert not list(tmp_path.glob(".code-write-*"))
    monkeypatch.setattr(os, "replace", replace)
    atomic_write_bytes(path, b"new\xff\x00")
    assert path.read_bytes() == b"new\xff\x00"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_fixed_code_paths_cannot_validate_one_path_and_write_another(tmp_path):
    root = init_repo(tmp_path / "repo")
    from skillflow.write_tools import execute_write
    with pytest.raises(ValueError, match="forward slashes"):
        execute_write("code", {"code": {"file": r"folder\code.py", "target": "code"}},
                      {"content": "answer = 42"}, str(root), strict_paths=True)
    assert not (root / "folder/code.py").exists()
    assert not (root / r"folder\code.py").exists()

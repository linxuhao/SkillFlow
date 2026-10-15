"""Git delivery failures must be recoverable within the actual owning claim."""
import json
import subprocess
from pathlib import Path

import pytest

from skillflow.graph import StepNode, Transition
from skillflow.output_targets import git
from test_output_targets import call, engine, init_repo, StepResult


def test_ignored_write_refuses_before_mutation_and_corrects_in_same_claim(tmp_path):
    sf, rid, claim, root = engine(tmp_path)
    (root / '.git/info/exclude').write_text('discarded/\n')
    code = sf._code_output(rid, claim.step_id)
    journal = code.journal.read_bytes()
    index = (root / '.git/index').read_bytes()
    result = call(sf, rid, claim, 'write', file='discarded/wrong.py', content='x = 2\n')
    assert 'error' in result, result
    assert 'git-ignored' in result['error'] and 'discarded/' in result['error']
    assert '.git/info/exclude:1' in result['error']
    assert not (root / 'discarded').exists()
    assert code.journal.read_bytes() == journal
    assert (root / '.git/index').read_bytes() == index
    assert sf.get_steps(rid)[0]['status'] == 'claimed'
    assert 'error' not in call(sf, rid, claim, 'write', file='correct.py', content='x = 2\n')
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'
    receipt = json.loads((Path(claim.inputs['_artifact_dir']) / 'code_changes.json').read_text())
    assert receipt['files'] == ['correct.py']
    assert git(root, 'status', '--porcelain').strip() == ''


@pytest.mark.parametrize('name,path_key', [
    ('create', 'file'), ('create', 'filename'), ('create', 'path'),
    ('write', 'file'), ('write', 'filename'), ('write', 'path'),
    ('edit', 'file'), ('edit', 'file_path'), ('edit', 'filename'), ('edit', 'path'),
    ('write_note', None), ('create_note', None), ('edit_note', None),
])
def test_actual_generated_variants_share_early_refusal(tmp_path, name, path_key):
    root = init_repo(tmp_path / 'repo')
    (root / '.git/info/exclude').write_text('notes/ignored.md\n')
    editing = name.startswith('edit')
    if editing:
        (root / 'notes').mkdir()
        (root / 'notes/ignored.md').write_text('before\n')
        (root / 'notes/allowed.md').write_text('before\n')
        git(root, 'add', '--', 'notes/allowed.md')
        git(root, 'commit', '-qm', 'editable baseline')
    node = StepNode(id='implement', output_mode='content' if path_key is None else 'write',
                    output_target='code', output_allow_full_write=True,
                    output_fixed={'note': {'file': 'notes/*.md'}} if path_key is None else {},
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node, root=root)
    assert name in claim.inputs['_tool_schemas']
    code = sf._code_output(rid, claim.step_id)
    journal = code.journal.read_bytes()
    index = (root / '.git/index').read_bytes()
    params = {'old_str': 'before', 'new_str': 'after'} if editing else {
        'initialContent' if name == 'create_note' else 'content': 'after\n'}
    params.update({path_key: 'notes/ignored.md'} if path_key else {'id': 'ignored'})
    result = call(sf, rid, claim, name, **params)
    assert 'git-ignored' in result.get('error', ''), result
    assert '.git/info/exclude:1' in result['error'] and 'notes/ignored.md' in result['error']
    assert code.journal.read_bytes() == journal
    assert (root / '.git/index').read_bytes() == index
    if editing:
        assert (root / 'notes/ignored.md').read_text() == 'before\n'
    else:
        assert not (root / 'notes').exists()
    params.update({path_key: 'notes/allowed.md'} if path_key else {'id': 'allowed'})
    result = call(sf, rid, claim, name, **params)
    assert 'error' not in result, result
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'
    assert git(root, 'show', 'HEAD:notes/allowed.md') == 'after\n'


@pytest.mark.parametrize('source', ['root', 'nested', 'global', 'exclude'])
def test_git_rules_and_tracked_ignored_semantics(tmp_path, source):
    root = init_repo(tmp_path / 'repo')
    rule = '*.cache\n!keep.cache\n'
    if source == 'root':
        (root / '.gitignore').write_text(rule)
        git(root, 'add', '.gitignore')
    elif source == 'nested':
        (root / 'nested').mkdir()
        (root / 'nested/.gitignore').write_text(rule)
        git(root, 'add', 'nested/.gitignore')
    elif source == 'global':
        excludes = tmp_path / 'owned-global-ignore'
        excludes.write_text(rule)
        git(root, 'config', 'core.excludesFile', str(excludes))
    else:
        (root / '.git/info/exclude').write_text(rule)
    prefix = 'nested/' if source == 'nested' else ''
    tracked = prefix + 'tracked.cache'
    (root / tracked).write_text('before\n')
    git(root, 'add', '-f', '--', tracked)
    git(root, 'commit', '-qm', 'tracked despite ignore')
    sf, rid, claim, root = engine(tmp_path, root=root)
    result = call(sf, rid, claim, 'write', file=prefix + 'blocked.cache', content='blocked\n')
    assert 'git-ignored' in result.get('error', ''), result
    assert '*.cache' in result['error']
    assert not (root / (prefix + 'blocked.cache')).exists()
    for path in [tracked, prefix + 'keep.cache', prefix + 'ordinary.md', prefix + '-dash[abc].md']:
        result = call(sf, rid, claim, 'write', file=path, content='after\n')
        assert 'error' not in result, result
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'
    assert git(root, 'show', 'HEAD:' + tracked) == 'after\n'


def test_artifact_write_and_declared_append_are_unaffected(tmp_path):
    root = init_repo(tmp_path / 'repo')
    (root / '.git/info/exclude').write_text('*.md\n')
    node = StepNode(id='implement', output_mode='content', output_target='artifact',
                    output_fixed={'note': {'file': 'ignored.md', 'on_exists': 'append'}},
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node, root=root)
    assert 'write_note' in claim.inputs['_tool_schemas']
    assert 'APPEND' in claim.inputs['_tool_schemas']['write_note']['description']
    assert 'append_note' not in claim.inputs['_tool_schemas']
    for content in ['first\n', 'second\n']:
        assert 'error' not in call(sf, rid, claim, 'write_note', content=content)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert (Path(claim.inputs['_artifact_dir']) / 'ignored.md').read_text() == 'first\nsecond\n'
    assert not (root / 'ignored.md').exists()


@pytest.mark.parametrize('kind', ['add', 'update', 'reference'])
def test_native_patch_checks_whole_batch_before_any_write(tmp_path, kind):
    root = init_repo(tmp_path / 'repo')
    (root / '.git/info/exclude').write_text('ignored/\n')
    if kind != 'add':
        (root / 'ignored').mkdir()
        (root / 'ignored/a.py').write_text('before\n')
    node = StepNode(id='implement', output_mode='write', output_target='code',
                    config={'extra_tools': ['apply_patch']},
                    context=[{'from': 'repository', 'mode': 'tool'}],
                    transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node, root=root)
    code = sf._code_output(rid, claim.step_id)
    journal = code.journal.read_bytes()
    patch = '*** Begin Patch\n*** Add File: allowed.py\n+allowed = True\n'
    params = {}
    if kind == 'add':
        patch += '*** Add File: ignored/a.py\n+after\n'
    elif kind == 'update':
        patch += '*** Update File: ignored/a.py\n@@\n-before\n+after\n'
    else:
        read = call(sf, rid, claim, 'read', path='ignored/a.py')
        params['references'] = [{'file': 'ignored/a.py', 'sha': read['citation']['sha'],
                                 'from_line': 1, 'to_line': 1, 'new_text': 'after'}]
    params['patch'] = patch + '*** End Patch\n'
    result = call(sf, rid, claim, 'apply_patch', **params)
    assert result['applied'] is False and result['phase'] == 'preflight', result
    assert 'git-ignored' in result['error'] and '.git/info/exclude:1' in result['error']
    assert result['written'] == [] and not result['partial'] and 'echo' not in result
    assert not (root / 'allowed.py').exists()
    assert code.journal.read_bytes() == journal
    if kind != 'add':
        assert (root / 'ignored/a.py').read_text() == 'before\n'
    else:
        assert not (root / 'ignored').exists()
    result = call(sf, rid, claim, 'apply_patch', patch=(
        '*** Begin Patch\n*** Add File: allowed.py\n+allowed = True\n*** End Patch\n'))
    assert result['applied'] is True, result
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'


@pytest.mark.parametrize('tool', ['write', 'write_note', 'apply_patch'])
@pytest.mark.parametrize('failure', ['non_git', 'missing_git', 'timeout'])
def test_runtime_probe_failure_is_honest_and_does_not_mutate(tmp_path, monkeypatch, failure, tool):
    node = StepNode(id='implement', output_mode='content' if tool == 'write_note' else 'write',
                    output_target='code', output_allow_full_write=True,
                    output_fixed={'note': {'file': 'new/path.py'}} if tool == 'write_note' else {},
                    config={'extra_tools': ['apply_patch']}, transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    if failure == 'non_git':
        (root / '.git').rename(root / '.git-unavailable')
    else:
        real_run = subprocess.run
        def run(args, **kwargs):
            if args[0] == 'git':
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(args, 60)
                raise FileNotFoundError('controlled missing git executable')
            return real_run(args, **kwargs)
        monkeypatch.setattr(subprocess, 'run', run)
    code = sf._code_output(rid, claim.step_id)
    journal = code.journal.read_bytes()
    params = ({'patch': '*** Begin Patch\n*** Add File: new/path.py\n+after\n*** End Patch\n'}
              if tool == 'apply_patch' else {'content': 'after\n'})
    if tool == 'write':
        params['file'] = 'new/path.py'
    result = call(sf, rid, claim, tool, **params)
    assert 'error' in result, result
    assert 'git-ignored' not in result['error'], result
    reason = {'non_git': 'not a git repository', 'missing_git': 'missing git executable',
              'timeout': 'timed out'}[failure]
    assert reason in result['error']
    assert not (root / 'new').exists()
    assert code.journal.read_bytes() == journal


@pytest.mark.parametrize('existing', ['', 'owned synthetic content\n'])
@pytest.mark.parametrize('tool', ['write', 'create'])
def test_existing_ignored_file_is_untouched(tmp_path, existing, tool):
    root = init_repo(tmp_path / 'repo')
    (root / '.git/info/exclude').write_text('ignored.py\n')
    file = root / 'ignored.py'
    file.write_text(existing)
    sf, rid, claim, root = engine(tmp_path, root=root)
    before = file.stat()
    result = call(sf, rid, claim, tool, file='ignored.py', content='replacement\n')
    assert 'git-ignored' in result.get('error', ''), result
    assert file.read_text() == existing
    assert file.stat().st_ino == before.st_ino and file.stat().st_mtime_ns == before.st_mtime_ns
    assert sf._code_output(rid, claim.step_id).load()['paths'] == []


@pytest.mark.parametrize('name', ['space name.py', 'line\nbreak.py', '!literal.py', 'bracket[abc].py'])
def test_git_probe_preserves_literal_filenames(tmp_path, name):
    sf, rid, claim, root = engine(tmp_path)
    # '*' matches literal filenames containing newlines, brackets and '!'.
    (root / '.git/info/exclude').write_text('*\n')
    result = call(sf, rid, claim, 'write', file=name, content='owned synthetic\n')
    assert 'git-ignored' in result.get('error', ''), result
    assert not (root / name).exists()


def test_code_slot_has_no_declared_append_variant():
    with pytest.raises(ValueError, match='cannot archive/append'):
        StepNode(id='s', output_mode='content', output_target='code',
                 output_fixed={'note': {'file': '*.md', 'on_exists': 'append'}})


@pytest.mark.parametrize('tool', ['write_doc', 'create_doc'])
@pytest.mark.parametrize('structured_fields', [False, True])
def test_declared_json_slot_shapes_share_the_guard(tmp_path, tool, structured_fields):
    entry = {'file': 'docs/*.json'}
    if structured_fields:
        entry['format'] = '{"id": str, "description": str}'
    node = StepNode(id='implement', output_mode='content', output_target='code',
                    output_fixed={'doc': entry}, transitions=[Transition(to=None)])
    sf, rid, claim, root = engine(tmp_path, node)
    (root / '.git/info/exclude').write_text('docs/ignored.json\n')
    code = sf._code_output(rid, claim.step_id)
    journal = code.journal.read_bytes()
    params = {'id': 'ignored'}
    if structured_fields:
        params['description'] = 'owned synthetic'
    else:
        params['initialContent' if tool == 'create_doc' else 'content'] = {'description': 'owned synthetic'}
    assert set(params) <= claim.inputs['_tool_schemas'][tool]['parameters'].keys()
    result = call(sf, rid, claim, tool, **params)
    assert 'git-ignored' in result.get('error', ''), result
    assert not (root / 'docs').exists()
    assert code.journal.read_bytes() == journal
    params['id'] = 'allowed'
    assert 'error' not in call(sf, rid, claim, tool, **params)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed'
    assert json.loads(git(root, 'show', 'HEAD:docs/allowed.json'))['description'] == 'owned synthetic'

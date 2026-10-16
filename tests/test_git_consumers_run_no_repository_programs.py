"""Engine Git consumers must not run programs a repository/env/global config selects.

Every fixture here is a real repository configured with callbacks that write a
marker file when Git runs them: hooks, core.fsmonitor, a clean filter, gpg
signing, a global-config hooksPath, an inherited GIT_CONFIG_* override and a
local remote's uploadpack. `test_fixture_callbacks_are_live_for_native_git`
proves each callback fires for native git add/commit/status/fetch, so an empty
marker afterwards means the engine path never ran them — not that the fixture
was inert. The publication cases go through the actual engine claim/write/
confirm path, the repo_apply/draft_commit tools and artifact history, and also
check the useful result: commit, receipt, settled index, retained unrelated
work. No helper of the implementation is imported or mocked.
"""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import CodeOutput
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

TOOLS = Path(__file__).parents[1] / 'src/skillflow/tools'
HOOKS = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit',
         'reference-transaction', 'post-index-change', 'pre-auto-gc',
         'post-checkout', 'post-merge')
QUIET = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false')


def observe(root, *args, binary=False):
    """Test-side observer: metadata commands only, callbacks disabled."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    out = subprocess.run(['git', *QUIET, *args], cwd=root, env=env, check=True,
                         capture_output=True, text=not binary).stdout
    return out


def native(root, *args, check=True):
    """Ordinary git exactly as a host process would run it (fixture setup/control)."""
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=check)


def script(path, body):
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return path


def marks(marker):
    return marker.read_text().split('\n') if marker.exists() and marker.read_text() else []


def hostile_repo(base: Path, *, filter_pattern='*.dat'):
    """A repository whose local config/hooks run marker programs."""
    root, marker, bin_ = base / 'repo', base / 'marker.log', base / 'bin'
    root.mkdir(parents=True)
    bin_.mkdir()
    native(root, 'init', '-q', '-b', 'main')
    native(root, 'config', 'user.name', 'fixture')
    native(root, 'config', 'user.email', 'fixture@test.invalid')
    (root / 'original.py').write_text('answer = 1\n')
    (root / 'other.py').write_text('other = 1\n')
    (root / 'data.dat').write_text('payload\n')
    (root / 'remove_me.py').write_text('gone = True\n')
    (root / '.gitattributes').write_text(f'{filter_pattern} filter=mark\n')
    native(root, 'add', '-A')
    native(root, 'commit', '-qm', 'baseline')
    for hook in HOOKS:
        script(root / '.git/hooks' / hook, f'echo "hook {hook}" >> {marker}\nexit 0\n')
    filt = script(bin_ / 'clean', f'echo "filter $1" >> {marker}\ncat\n')
    fsmon = script(bin_ / 'fsmonitor', f'echo "fsmonitor" >> {marker}\nexit 1\n')
    gpg = script(bin_ / 'gpg', f'echo "gpg" >> {marker}\nexit 1\n')
    native(root, 'config', 'filter.mark.clean', f'{filt} %f')
    native(root, 'config', 'filter.mark.smudge', f'{filt} %f')
    native(root, 'config', 'core.fsmonitor', str(fsmon))
    native(root, 'config', 'commit.gpgSign', 'true')
    native(root, 'config', 'gpg.program', str(gpg))
    return root, marker


def engine(tmp_path, root, node=None):
    sf = SkillFlow(':memory:')
    sf._tool_loader = ToolLoader(TOOLS)
    sf._workspace = WorkspaceManager(str(tmp_path / 'artifacts'),
                                    code_path_resolver=lambda pid, run_id=None: root)
    node = node or StepNode(id='implement', output_mode='write', output_target='code',
                            output_allow_full_write=True,
                            config={'extra_tools': ['reshape']},
                            transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='g', begin=node.id, steps=[node]))
    rid = sf.create_run('g', {'project_id': 'p'}, project_id='p')
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    assert claim is not None

    def reshape(*, output_dir='', output_target='', **kwargs):
        """A custom code tool: delete one file, make another executable."""
        base = Path(output_dir)
        (base / 'remove_me.py').unlink()
        tool = base / 'bin tool[1].sh'
        tool.write_text('#!/bin/sh\necho ok\n')
        tool.chmod(0o755)
        return {'deleted': ['remove_me.py'], 'written': ['bin tool[1].sh']}
    sf._tool_loader.register_dynamic_tool('reshape', {}, reshape)
    return sf, rid, claim


def call(sf, rid, claim, name, **params):
    return sf.execute_tool(name, params, run_id=rid, step_id=claim.step_id,
                           step_instance_id=claim.token.step_instance_id,
                           claim_epoch=claim.token.claim_epoch)


def index_sha(root):
    return hashlib.sha256((root / '.git/index').read_bytes()).hexdigest()


def step_error(sf, rid):
    return json.dumps(sf.get_steps(rid)[0], default=str)


def write_outputs(sf, rid, claim):
    assert 'error' not in call(sf, rid, claim, 'write', file='pkg/feature.py',
                               content='def feature():\n    return 2\n')
    assert 'error' not in call(sf, rid, claim, 'edit', file='original.py',
                               old_str='answer = 1', new_str='answer = 2')
    res = call(sf, rid, claim, 'reshape')
    assert 'error' not in res, res


# ── the fixture's callbacks really fire for native Git ──────────────────────

def test_fixture_callbacks_are_live_for_native_git(tmp_path):
    root, marker = hostile_repo(tmp_path)
    (root / 'data.dat').write_text('changed payload\n')
    (root / 'new.py').write_text('x = 1\n')
    native(root, 'status', '--porcelain')
    native(root, 'add', '-A')
    native(root, 'commit', '-qm', 'native', check=False)
    seen = '\n'.join(marks(marker))
    for expected in ('fsmonitor', 'filter data.dat', 'hook pre-commit', 'gpg'):
        assert expected in seen, (expected, seen)
    marker.unlink()
    native(root, '-c', 'commit.gpgSign=false', 'commit', '-qm', 'native unsigned')
    seen = '\n'.join(marks(marker))
    for expected in ('hook post-commit', 'hook reference-transaction', 'hook commit-msg'):
        assert expected in seen, (expected, seen)


# ── CodeOutput through the engine ───────────────────────────────────────────

def test_code_output_publishes_through_engine_without_repository_programs(tmp_path):
    root, marker = hostile_repo(tmp_path)
    base = observe(root, 'rev-parse', 'HEAD').strip()
    sf, rid, claim = engine(tmp_path, root)
    write_outputs(sf, rid, claim)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert marks(marker) == []
    assert sf.get_steps(rid)[0]['status'] == 'completed', step_error(sf, rid)

    head = observe(root, 'rev-parse', 'HEAD').strip()
    assert observe(root, 'rev-parse', 'HEAD~1').strip() == base
    assert observe(root, 'symbolic-ref', 'HEAD').strip() == 'refs/heads/main'
    # Identity is what native commit would use: GIT_AUTHOR_* env, else repo config.
    author = (f"{os.environ.get('GIT_AUTHOR_NAME', 'fixture')} "
              f"<{os.environ.get('GIT_AUTHOR_EMAIL', 'fixture@test.invalid')}>")
    assert observe(root, 'show', '-s', '--format=%an <%ae>|%s', 'HEAD').startswith(
        f'{author}|step: implement [p]')
    assert 'gpgsig' not in observe(root, 'cat-file', '-p', 'HEAD')
    changed = observe(root, 'diff-tree', '--no-renames', '--name-status', '-r', '-z', base, head)
    assert sorted(zip(changed.split('\0')[1::2], changed.split('\0')[0::2])) == [
        ('bin tool[1].sh', 'A'), ('original.py', 'M'), ('pkg/feature.py', 'A'),
        ('remove_me.py', 'D')]
    assert observe(root, 'cat-file', 'blob', 'HEAD:original.py') == 'answer = 2\n'
    assert observe(root, 'ls-tree', 'HEAD', 'bin tool[1].sh').startswith('100755 ')
    assert observe(root, 'cat-file', 'blob', 'HEAD:data.dat') == 'payload\n'

    receipt = json.loads((Path(claim.inputs['_artifact_dir']) / 'code_changes.json').read_text())
    assert receipt['base_commit'] == base and receipt['commit'] == head
    assert sorted(receipt['files']) == ['bin tool[1].sh', 'original.py', 'pkg/feature.py',
                                        'remove_me.py']
    # The index was settled to the new commit: nothing staged against HEAD.
    assert observe(root, 'diff-index', '--cached', '--name-only', 'HEAD') == ''
    assert marks(marker) == []


def test_global_config_and_inherited_git_env_programs_do_not_run(tmp_path, monkeypatch):
    base_dir = tmp_path / 'fixture'
    base_dir.mkdir()
    root, marker = hostile_repo(base_dir)
    for hook in HOOKS:  # only global/env callbacks remain in this case
        (root / '.git/hooks' / hook).unlink()
    observe(root, 'config', '--unset', 'core.fsmonitor')
    observe(root, 'config', '--unset', 'commit.gpgSign')
    home, ghooks = tmp_path / 'home', tmp_path / 'global-hooks'
    home.mkdir()
    ghooks.mkdir()
    for hook in HOOKS:
        script(ghooks / hook, f'echo "global {hook}" >> {marker}\nexit 0\n')
    envmon = script(tmp_path / 'env-fsmonitor', f'echo "env fsmonitor" >> {marker}\nexit 1\n')
    (home / '.gitconfig').write_text(f'[core]\n\thooksPath = {ghooks}\n'
                                     '[user]\n\tname = global-user\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.fsmonitor')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(envmon))
    sf, rid, claim = engine(tmp_path, root)
    write_outputs(sf, rid, claim)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert marks(marker) == []
    assert sf.get_steps(rid)[0]['status'] == 'completed', step_error(sf, rid)
    assert observe(root, 'cat-file', 'blob', 'HEAD:pkg/feature.py').endswith('return 2\n')


def test_filtered_output_is_refused_visibly_and_retained(tmp_path):
    root, marker = hostile_repo(tmp_path, filter_pattern='*.py')
    base = observe(root, 'rev-parse', 'HEAD').strip()
    before_index = index_sha(root)
    sf, rid, claim = engine(tmp_path, root)
    assert 'error' not in call(sf, rid, claim, 'write', file='pkg/feature.py',
                               content='def feature():\n    return 2\n')
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert marks(marker) == []
    assert sf.get_steps(rid)[0]['status'] != 'completed'
    assert 'filter=mark' in step_error(sf, rid)
    assert observe(root, 'rev-parse', 'HEAD').strip() == base
    assert (root / 'pkg/feature.py').read_text() == 'def feature():\n    return 2\n'
    assert index_sha(root) == before_index


def test_unrelated_dirty_and_staged_index_are_refused_and_retained(tmp_path):
    root, marker = hostile_repo(tmp_path)
    base = observe(root, 'rev-parse', 'HEAD').strip()
    sf, rid, claim = engine(tmp_path, root)
    assert 'error' not in call(sf, rid, claim, 'write', file='pkg/feature.py',
                               content='def feature():\n    return 2\n')
    (root / 'other.py').write_text('other = "unrelated edit"\n')
    (root / 'staged.txt').write_text('user staged this\n')
    observe(root, 'add', '--', 'staged.txt')
    marker.unlink(missing_ok=True)  # the observer's own index write may run the filter
    staged_index = index_sha(root)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] != 'completed'
    error = step_error(sf, rid)
    assert 'Unreported code changes retained' in error
    assert 'other.py' in error and 'staged.txt' in error
    assert marks(marker) == []
    assert observe(root, 'rev-parse', 'HEAD').strip() == base
    assert index_sha(root) == staged_index
    assert (root / 'other.py').read_text() == 'other = "unrelated edit"\n'
    assert observe(root, 'diff-index', '--cached', '--name-only', 'HEAD').split() == ['staged.txt']


def test_new_step_refuses_dirty_tree_without_running_programs(tmp_path):
    root, marker = hostile_repo(tmp_path)
    (root / 'data.dat').write_text('dirty but filtered path\n')
    journal = tmp_path / 'journal.json'
    with pytest.raises(RuntimeError) as refused:
        CodeOutput(root, journal).prepare('run', 1, None)
    assert 'requires a clean worktree' in str(refused.value) or 'filter=mark' in str(refused.value)
    assert marks(marker) == []
    (root / 'data.dat').write_text('payload\n')
    (root / 'untracked.txt').write_text('new')
    with pytest.raises(RuntimeError, match='requires a clean worktree'):
        CodeOutput(root, journal).prepare('run', 1, None)
    (root / 'untracked.txt').unlink()
    CodeOutput(root, journal).prepare('run', 1, None)
    assert marks(marker) == []


def test_existing_index_lock_is_refused_and_not_removed(tmp_path):
    root, marker = hostile_repo(tmp_path)
    base = observe(root, 'rev-parse', 'HEAD').strip()
    sf, rid, claim = engine(tmp_path, root)
    assert 'error' not in call(sf, rid, claim, 'write', file='pkg/feature.py', content='x = 1\n')
    lock = root / '.git/index.lock'
    lock.write_bytes(b'held by someone else')
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] != 'completed'
    assert lock.read_bytes() == b'held by someone else'
    assert observe(root, 'rev-parse', 'HEAD').strip() == base
    assert marks(marker) == []


def test_linked_worktree_publication_settles_its_own_index(tmp_path):
    main, marker = hostile_repo(tmp_path)
    wt = tmp_path / 'wt'
    observe(main, 'worktree', 'add', '-q', '--no-checkout', '-b', 'run/1', str(wt), 'HEAD')
    observe(wt, 'reset', '-q', '--hard')  # observer-only checkout of plain files, hooks off
    marker.unlink(missing_ok=True)  # the observer checkout ran the smudge filter
    main_index = index_sha(main)
    sf, rid, claim = engine(tmp_path, wt)
    write_outputs(sf, rid, claim)
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert marks(marker) == []
    assert sf.get_steps(rid)[0]['status'] == 'completed', step_error(sf, rid)
    assert observe(wt, 'rev-parse', 'run/1').strip() == observe(wt, 'rev-parse', 'HEAD').strip()
    assert observe(wt, 'diff-index', '--cached', '--name-only', 'HEAD') == ''
    assert index_sha(main) == main_index
    assert observe(main, 'rev-parse', 'main').strip() == observe(main, 'rev-parse', 'run/1~1').strip()


def test_plain_repository_publication_stays_clean_for_native_status(tmp_path):
    """The useful ordinary case: native status agrees after publication."""
    root = tmp_path / 'repo'
    root.mkdir()
    native(root, 'init', '-q', '-b', 'main')
    native(root, 'config', 'user.name', 'plain')
    native(root, 'config', 'user.email', 'plain@test.invalid')
    for name in ('original.py', 'remove_me.py'):
        (root / name).write_text(f'# {name}\nanswer = 1\n')
    native(root, 'add', '-A')
    native(root, 'commit', '-qm', 'baseline')
    sf, rid, claim = engine(tmp_path, root)
    write_outputs(sf, rid, claim)
    (root / 'pkg/blob.bin').write_bytes(b'\x00\xff\r\n\x01')
    assert 'error' not in sf._record_code_result(rid, claim.step_id, {'written': ['pkg/blob.bin']})
    sf.confirm_step(claim.token, StepResult(outputs={}, flags={}))
    assert sf.get_steps(rid)[0]['status'] == 'completed', step_error(sf, rid)
    assert native(root, 'status', '--porcelain', '--untracked-files=all').stdout == ''
    assert observe(root, 'cat-file', 'blob', 'HEAD:pkg/blob.bin', binary=True) == b'\x00\xff\r\n\x01'


def test_crlf_conversion_is_refused_but_lf_output_publishes(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    native(root, 'init', '-q', '-b', 'main')
    native(root, 'config', 'user.name', 'eol')
    native(root, 'config', 'user.email', 'eol@test.invalid')
    native(root, 'config', 'core.autocrlf', 'input')
    (root / 'original.py').write_text('answer = 1\n')
    native(root, 'add', '-A')
    native(root, 'commit', '-qm', 'baseline')
    journal = tmp_path / 'journal.json'
    code = CodeOutput(root, journal)
    code.prepare('run', 1, None)
    (root / 'lf.txt').write_bytes(b'one\ntwo\n')
    code.record(['lf.txt'])
    code.commit(tmp_path / 'receipt-1.json', 'lf output')
    assert observe(root, 'cat-file', 'blob', 'HEAD:lf.txt') == 'one\ntwo\n'
    head = observe(root, 'rev-parse', 'HEAD').strip()
    code.prepare('run', 2, None)
    (root / 'crlf.txt').write_bytes(b'one\r\ntwo\r\n')
    code.record(['crlf.txt'])
    with pytest.raises(RuntimeError, match='end-of-line conversion'):
        code.commit(tmp_path / 'receipt-2.json', 'crlf output')
    assert observe(root, 'rev-parse', 'HEAD').strip() == head
    assert (root / 'crlf.txt').read_bytes() == b'one\r\ntwo\r\n'


# ── legacy publishers and artifact history ──────────────────────────────────

def test_repo_apply_commits_like_add_all_without_repository_programs(tmp_path):
    from skillflow.tools.repo_apply.impl import repo_apply
    root, marker = hostile_repo(tmp_path)
    base = observe(root, 'rev-parse', 'HEAD').strip()
    src = tmp_path / 'draft'
    (src / 'pkg').mkdir(parents=True)
    (src / 'pkg/applied.py').write_text('applied = True\n')
    (root / 'other.py').write_text('other = "dirty too"\n')  # add -A takes it as before
    result = repo_apply(str(src), project_root=str(root), step_id='impl')
    assert marks(marker) == []
    assert result == {'applied': True, 'files': ['pkg/applied.py'], 'committed': True}
    assert observe(root, 'rev-parse', 'HEAD~1').strip() == base
    assert observe(root, 'cat-file', 'blob', 'HEAD:other.py') == 'other = "dirty too"\n'
    assert observe(root, 'diff-index', '--cached', '--name-only', 'HEAD') == ''
    again = repo_apply(str(src), project_root=str(root), step_id='impl')
    assert again == {'applied': True, 'files': ['pkg/applied.py'], 'committed': False}
    assert marks(marker) == []


def test_repo_apply_refuses_a_filtered_path_visibly(tmp_path):
    from skillflow.tools.repo_apply.impl import repo_apply
    root, marker = hostile_repo(tmp_path)
    base = observe(root, 'rev-parse', 'HEAD').strip()
    src = tmp_path / 'draft'
    src.mkdir()
    (src / 'blob.dat').write_text('needs the filter\n')
    result = repo_apply(str(src), project_root=str(root))
    assert marks(marker) == []
    assert result['applied'] is False and 'filter=mark' in result['error']
    assert observe(root, 'rev-parse', 'HEAD').strip() == base


def test_draft_commit_and_artifact_history_run_no_repository_programs(tmp_path):
    from skillflow.tools.draft_commit.impl import draft_commit
    ws, marker = hostile_repo(tmp_path)
    tmp = ws / 'cfg' / 'step.tmp'
    tmp.mkdir(parents=True)
    (tmp / 'out.md').write_text('# out\n')
    result = draft_commit(str(tmp), step_id='step')
    assert marks(marker) == []
    assert result == {'committed': True, 'files': ['out.md']}, result
    assert observe(ws, 'cat-file', 'blob', 'HEAD:cfg/step/out.md') == '# out\n'

    # Artifact history versions a step directory in the workspace repository.
    sf = SkillFlow(':memory:')
    sf._workspace = WorkspaceManager(str(tmp_path / 'ws-base'))
    project = sf._workspace.get_project_path('p')
    project.mkdir(parents=True)
    observe(project, 'init', '-q')
    for hook in HOOKS:
        script(project / '.git/hooks' / hook, f'echo "artifact {hook}" >> {marker}\nexit 0\n')
    observe(project, 'config', 'core.fsmonitor', str(tmp_path / 'bin/fsmonitor'))
    step = project / 'g' / 's'
    step.mkdir(parents=True)
    (step / 'result.md').write_text('v1\n')
    sf._artifact_commit('p', 'g', 's', 'run-1')
    (step / 'result.md').write_text('v2\n')
    sf._artifact_commit('p', 'g', 's', 'run-1')
    versions = sf.step_output_versions('p', 'g', 's')
    assert len(versions) == 2, versions
    assert observe(project, 'cat-file', 'blob', 'HEAD:g/s/result.md') == 'v2\n'
    assert marks(marker) == []


def test_git_sync_pull_is_unavailable_and_runs_no_transport_program(tmp_path):
    from skillflow.tools.git_sync_pre.impl import git_sync_pre
    root, marker = hostile_repo(tmp_path)
    origin = tmp_path / 'origin.git'
    observe(tmp_path, 'clone', '-q', '--bare', str(root), str(origin))
    upload = script(tmp_path / 'bin/upload', f'echo "uploadpack" >> {marker}\nexec git-upload-pack "$@"\n')
    native(root, 'remote', 'add', 'origin', str(origin))
    native(root, 'config', 'remote.origin.uploadpack', str(upload))
    base = observe(root, 'rev-parse', 'HEAD').strip()
    result = git_sync_pre(project_root=str(root), policy='pull')
    assert marks(marker) == []
    assert result['synced'] is False and result['action'] == 'unavailable', result
    assert observe(root, 'rev-parse', 'HEAD').strip() == base
    assert git_sync_pre(project_root=str(root))['action'] == 'skip'

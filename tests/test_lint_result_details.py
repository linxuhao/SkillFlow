"""Explicit optional per-call backend data; no backend inference or policy change."""
from collections import UserDict
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import skillflow
from skillflow.lint_backends import _backends, register_backend
from skillflow.step_validation import StepValidator
from skillflow.tool_loader import ToolLoader
from skillflow.tools.lint.impl import lint


@pytest.fixture(autouse=True)
def registry():
    before = dict(_backends)
    yield
    _backends.clear()
    _backends.update(before)


def candidate(tmp_path, backend='receipt'):
    fp = tmp_path / 'candidate.js'
    fp.write_text('const answer = 42;')
    (tmp_path / 'manifest.json').write_text(json.dumps({'.js': backend}))
    return fp


def run(tmp_path, files=None):
    return lint(files or ['candidate.js'], workspace_root=str(tmp_path),
                manifest_path='manifest.json')


@pytest.mark.parametrize('passed', [True, False])
@pytest.mark.parametrize('factory', [dict, UserDict])
def test_optional_json_mapping_keeps_actual_result_fields_and_detaches(tmp_path, passed, factory):
    fp = candidate(tmp_path)
    receipt = factory({'parser': {'input': ['original', None, True], 'name': '例'},
                       'passed': not passed, 'file': '/spoof', 'error_message': 'spoof'})
    register_backend('receipt', lambda path: {'passed': passed, 'error_message': 'actual diagnostic',
                                             'file': '/backend-spoof', 'details': receipt})
    result = run(tmp_path)
    row, = result['results']
    assert result['all_passed'] is passed
    assert row['file'] == str(fp) and row['passed'] is passed
    assert row['error_message'] == 'actual diagnostic'
    assert row['details'] == dict(receipt)
    receipt['parser']['input'][0] = 'changed after return'
    assert row['details']['parser']['input'][0] == 'original'
    row['details']['parser']['input'].append('consumer changed copy')
    assert 'consumer changed copy' not in receipt['parser']['input']


def test_empty_mapping_is_explicit_data(tmp_path):
    candidate(tmp_path)
    register_backend('receipt', lambda fp: {'passed': True, 'details': {}})
    assert run(tmp_path)['results'][0]['details'] == {}


@pytest.mark.parametrize('metadata', [None, [], 'receipt', 1, ('receipt',), {'not-json': object()},
                                     {'not-json': {1, 2}}, {'not-json': float('nan')}])
def test_nonmapping_or_nonjson_details_are_omitted_without_changing_result(tmp_path, metadata):
    fp = candidate(tmp_path)
    register_backend('receipt', lambda path: {'passed': False, 'error_message': 'real failure',
                                             'details': metadata})
    assert run(tmp_path) == {'all_passed': False, 'results': [
        {'file': str(fp), 'passed': False, 'error_message': 'real failure'}]}


def test_cyclic_details_are_omitted(tmp_path):
    fp = candidate(tmp_path)
    details = {}; details['cycle'] = details
    register_backend('receipt', lambda path: {'passed': True, 'details': details})
    assert run(tmp_path)['results'] == [{'file': str(fp), 'passed': True, 'error_message': ''}]


@pytest.mark.parametrize('backend', ['basic', 'skip', 'unknown-backend', 'ruff', 'djlint'])
def test_existing_noop_or_wrong_extension_outcomes_do_not_gain_receipts(tmp_path, backend):
    candidate(tmp_path, backend)
    result = run(tmp_path)
    assert result['all_passed'] is True  # inherited policy, explicitly not a parser proof
    assert len(result['results']) == 1 and 'details' not in result['results'][0]


def test_absent_metadata_and_caught_backend_exception_do_not_gain_receipts(tmp_path):
    candidate(tmp_path)
    register_backend('receipt', lambda fp: {'passed': True})
    assert 'details' not in run(tmp_path)['results'][0]
    def broken(fp):
        raise RuntimeError('backend unavailable before a result')
    register_backend('receipt', broken)
    result = run(tmp_path)
    assert result['all_passed'] is False
    assert 'backend unavailable' in result['results'][0]['error_message']
    assert 'details' not in result['results'][0]


def test_missing_files_do_not_manufacture_per_file_data(tmp_path):
    assert run(tmp_path, ['missing.js']) == {'all_passed': True, 'results': []}


def parser_backend(seen):
    def backend(fp, **context):
        node = '/opt/aitelier-node/bin/node'
        done = subprocess.run([node, '--check', str(fp)], capture_output=True, text=True, timeout=10)
        details = {'syntax-parser': {'backend': 'node-check', 'returncode': done.returncode,
                    'input_sha256': hashlib.sha256(fp.read_bytes()).hexdigest(),
                    'stdout': done.stdout, 'stderr': done.stderr}}
        seen.append((fp, context, details))
        return {'passed': done.returncode == 0, 'error_message': done.stderr, 'details': details}
    return backend


@pytest.mark.parametrize('valid', [True, False])
def test_actual_native_parser_keeps_receipt_for_valid_and_invalid_input(tmp_path, valid):
    fp = candidate(tmp_path)
    if not valid: fp.write_text('function (( {')
    seen = []
    register_backend('receipt', parser_backend(seen))
    result = run(tmp_path)
    assert result['all_passed'] is valid
    assert result['results'][0]['details'] == seen[0][2]
    assert result['results'][0]['passed'] is valid
    assert bool(result['results'][0]['error_message']) is not valid


@pytest.mark.parametrize('valid', [True, False])
def test_stepvalidator_public_host_wrapper_receives_actual_native_details(tmp_path, valid):
    fp = candidate(tmp_path)
    if not valid: fp.write_text('function (( {')
    seen = []; normalized = []
    register_backend('receipt', parser_backend(seen))
    loader = ToolLoader(Path(skillflow.__file__).parent / 'tools')
    native = loader.load_fn('lint')
    # Public host wrapper seam; no private dispatcher replacement or ambient collector.
    def measured(files, *, workspace_root='', manifest_path=None, run_id='', step_id='', project_root=''):
        result = native(files, workspace_root=workspace_root, manifest_path=manifest_path,
                        run_id=run_id, step_id=step_id, project_root=project_root)
        normalized.append(result)
        assert result['results'][0]['details'] == seen[0][2]
        return result
    loader.register_dynamic_tool('measured_lint', {}, measured)
    outcome = StepValidator(loader, tmp_path, tool_context={
        'run_id': 'host-run', 'step_id': 'host-step', 'project_root': str(tmp_path)}).validate([
        {'tool': 'measured_lint', 'files': ['candidate.js'], 'manifest_path': 'manifest.json',
         'run_id': 'spoof', 'step_id': 'spoof', 'project_root': '/spoof'}])
    assert outcome['passed'] is valid
    assert len(normalized) == 1
    assert seen[0][1] == {'run_id': 'host-run', 'step_id': 'host-step', 'project_root': str(tmp_path)}
    if not valid:
        assert outcome['errors'][0]['details'] == seen[0][2]
        assert 'SyntaxError' in outcome['errors'][0]['error_message']


def test_manifest_mutated_during_callback_cannot_relabel_the_result(tmp_path):
    candidate(tmp_path)
    (tmp_path / 'second.js').write_text('const second = 2;')
    shared = {'backend': 'actually-called', 'files': []}
    seen = []
    def backend(fp):
        seen.append(fp.name)
        shared['files'][:] = [fp.name]
        (tmp_path / 'manifest.json').write_text('{".js":"basic"}')
        return {'passed': True, 'details': shared}
    register_backend('receipt', backend)
    result = run(tmp_path, ['*.js'])
    assert seen == ['candidate.js', 'second.js']
    assert json.loads((tmp_path / 'manifest.json').read_text()) == {'.js': 'basic'}
    assert [row['details'] for row in result['results']] == [
        {'backend': 'actually-called', 'files': ['candidate.js']},
        {'backend': 'actually-called', 'files': ['second.js']}]


def test_deep_valid_json_mapping_is_retained_and_detached(tmp_path):
    fp = candidate(tmp_path)
    leaf = {"value": "original"}
    details = leaf
    for _ in range(128):
        details = {"nested": details}
    register_backend("receipt", lambda path: {"passed": True, "details": details})
    result, = run(tmp_path)["results"]
    assert result["file"] == str(fp) and result["passed"] is True
    copied = result["details"]
    for _ in range(128):
        copied = copied["nested"]
    assert copied == {"value": "original"}
    leaf["value"] = "backend changed"
    assert copied == {"value": "original"}

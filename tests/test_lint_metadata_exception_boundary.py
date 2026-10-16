"""Optional data failures preserve outcomes; cancellation is never metadata."""
import asyncio
from collections.abc import Mapping
from pathlib import Path

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


def invoke(root, entry, callback):
    fp = root / 'candidate.js'
    fp.write_text('const answer = 42;')
    (root / 'manifest.json').write_text('{".js":"optional-data"}')
    register_backend('optional-data', callback)
    if entry == 'native':
        return lint(['candidate.js'], workspace_root=str(root), manifest_path='manifest.json')
    return StepValidator(ToolLoader(Path(skillflow.__file__).parent / 'tools'), root).validate([
        {'tool': 'lint', 'files': ['candidate.js'], 'manifest_path': 'manifest.json'}])


@pytest.mark.parametrize('entry', ['native', 'stepvalidator'])
@pytest.mark.parametrize('passed', [True, False])
def test_optional_result_getter_failure_preserves_normalized_fields(tmp_path, entry, passed):
    class Result(dict):
        def get(self, key, default=None):
            if key == 'details':
                raise RuntimeError('optional receipt getter unavailable')
            return super().get(key, default)
    def backend(fp):
        return Result(passed=passed, error_message='actual parser diagnostic')
    expected = {'file': str(tmp_path / 'candidate.js'), 'passed': passed,
                'error_message': 'actual parser diagnostic'}
    actual = invoke(tmp_path, entry, backend)
    if entry == 'native':
        assert actual == {'all_passed': passed, 'results': [expected]}
    else:
        assert actual == {'passed': passed, 'errors': [] if passed else [expected], 'warnings': []}


@pytest.mark.parametrize('entry', ['native', 'stepvalidator'])
@pytest.mark.parametrize('passed', [True, False])
@pytest.mark.parametrize('signal', [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_metadata_materialization_preserves_baseexception_cancellation(tmp_path, entry, passed, signal):
    class Receipt(Mapping):
        def __iter__(self):
            return iter(['snapshot'])
        def __len__(self):
            return 1
        def __getitem__(self, key):
            raise signal('cancel optional receipt read')
    def backend(fp):
        return {'passed': passed, 'error_message': 'actual parser diagnostic', 'details': Receipt()}
    with pytest.raises(signal, match='cancel optional receipt read'):
        invoke(tmp_path, entry, backend)

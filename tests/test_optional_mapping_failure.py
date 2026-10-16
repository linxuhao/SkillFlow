"""An optional lazy receipt snapshot may be unavailable without changing parsing."""
from collections.abc import Mapping
from pathlib import Path
import pytest,skillflow
from skillflow.lint_backends import _backends,register_backend
from skillflow.tools.lint.impl import lint
from skillflow.step_validation import StepValidator
from skillflow.tool_loader import ToolLoader

class LazyReceipt(Mapping):
    def __iter__(self): return iter(['snapshot'])
    def __len__(self): return 1
    def __getitem__(self,key):
        raise RuntimeError('optional receipt snapshot unavailable')

@pytest.mark.parametrize('entry',['native','stepvalidator'])
@pytest.mark.parametrize('passed',[True,False])
def test_unavailable_optional_mapping_keeps_actual_normalized_result(tmp_path,entry,passed):
    fp=tmp_path/'candidate.js';fp.write_text('const answer=42;')
    (tmp_path/'manifest.json').write_text('{".js":"lazy_measurement"}')
    before=dict(_backends);seen=[]
    def backend(path):
        seen.append(path)
        return {'passed':passed,'error_message':'actual parser diagnostic','details':LazyReceipt()}
    expected={'file':str(fp),'passed':passed,'error_message':'actual parser diagnostic'}
    try:
        register_backend('lazy_measurement',backend)
        if entry=='native':
            result=lint(['candidate.js'],workspace_root=str(tmp_path),manifest_path='manifest.json')
            assert result=={'all_passed':passed,'results':[expected]}
        else:
            result=StepValidator(ToolLoader(Path(skillflow.__file__).parent/'tools'),tmp_path).validate([
                {'tool':'lint','files':['candidate.js'],'manifest_path':'manifest.json'}])
            assert result=={'passed':passed,'errors':[] if passed else [expected],'warnings':[]}
        assert seen==[fp]
    finally:
        _backends.clear();_backends.update(before)

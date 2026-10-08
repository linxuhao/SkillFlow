"""Explicit refusal is terminal; unknown failures and cancellation keep their contracts."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from skillflow import ToolExecutionRefused
from skillflow.exceptions import ToolArgumentsUnavailable
from test_cancelled_tool_terminal import _engine, _run, _rows, _events


@pytest.mark.parametrize('tool_error', ['fail', 'route'])
def test_definitive_refusal_is_called_once_and_never_routes(tmp_path, tool_error):
    calls = []
    reason = 'constraint refused: same frozen journal'

    def refuse(**kwargs):
        calls.append(kwargs['run_id'])
        raise ToolExecutionRefused(reason)

    sf = _engine(tmp_path, refuse)
    # Explicit typed refusal is an exception contract even on result-routing tools.
    graph = sf._get_resolver('normal_cpu').graph
    graph.steps[0].tool_error = tool_error
    sf.register_graph(graph)
    run = _run(sf)
    try:
        for _ in range(5):
            sf.advance_run(run)
        assert calls == [run]
        assert sf.get_run(run)['status'] == 'failed'
        assert sf.get_run(run)['error_reason'] == reason
        steps = _rows(sf, 'skillflow_steps', run)
        assert next(s for s in steps if s['step_id'] == 'test')['last_error'] == reason
        assert next(s for s in steps if s['step_id'] == 'test')['status'] == 'failed'
        assert all(s['status'] == 'pending' for s in steps if s['step_id'] == 'done')
        assert not any(e['from_step'] == 'test' for e in _rows(sf, 'skillflow_edge_counts', run))
        assert _rows(sf, 'skillflow_active_ops', run) == []
        assert 'run_completed' not in _events(sf, run)
        assert _events(sf, run).count('run_failed') == 1
        assert not issubclass(ToolExecutionRefused, ToolArgumentsUnavailable)
    finally:
        sf._conn.close()


@pytest.mark.parametrize('error', [OSError, ValueError])
def test_unknown_failure_with_identical_refusal_prose_retries_then_succeeds(tmp_path, error):
    calls = []

    def transient(**kwargs):
        calls.append(kwargs['run_id'])
        if len(calls) == 1:
            raise error('constraint refused: same frozen journal')
        return {'passed': True}

    sf = _engine(tmp_path, transient)
    run = _run(sf)
    try:
        with pytest.raises(error, match='constraint refused'):
            sf.advance_run(run)
        assert sf.get_run(run)['status'] == 'running'
        assert _rows(sf, 'skillflow_active_ops', run) == []
        sf.advance_run(run)
        assert calls == [run, run]
        assert sf.get_run(run)['status'] == 'completed'
        assert _rows(sf, 'skillflow_active_ops', run) == []
    finally:
        sf._conn.close()


def test_refusal_during_cancellation_retires_before_settling_with_stop_reason(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs['run_id'])
        entered.set()
        assert release.wait(10)
        raise ToolExecutionRefused('actual refusal after stop')

    sf = _engine(tmp_path, refuse)
    run = _run(sf)
    staged = tmp_path / 'preserved-output.txt'
    staged.write_text('preserve admitted output')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(sf.advance_run, run)
            try:
                assert entered.wait(10)
                assert sf.stop_run(run, 'original exact stop reason')['outcome'] == 'draining'
                assert sf.get_run(run)['status'] == 'running'
                assert len(_rows(sf, 'skillflow_active_ops', run)) == 1
            finally:
                release.set()
            work.result(timeout=10)
        row = sf.get_run(run)
        assert row['status'] == 'failed'
        assert row['error_reason'] == 'original exact stop reason'
        assert row['cancel_requested_at']
        assert calls == [run]
        assert _rows(sf, 'skillflow_active_ops', run) == []
        steps = _rows(sf, 'skillflow_steps', run)
        assert not any(s['status'] == 'claimed' for s in steps)
        assert next(s for s in steps if s['step_id'] == 'test')['last_error'] == 'actual refusal after stop'
        assert staged.read_text() == 'preserve admitted output'
        assert 'run_completed' not in _events(sf, run)
        assert _events(sf, run).count('run_failed') == 1
    finally:
        sf._conn.close()

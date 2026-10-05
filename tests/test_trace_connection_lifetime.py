"""Real SQLite ownership regressions; no mocked connections or FD limits."""
import os
import sqlite3
import threading
from contextlib import ExitStack
from pathlib import Path

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode


def _engine(tmp_path):
    sf = SkillFlow(str(tmp_path / 'main.db'), trace_db_path=str(tmp_path / 'traces'))
    sf.register_graph(PipelineGraph(name='trace-probe', begin='one',
                                   steps=[StepNode(id='one')]))
    return sf


def _run(sf, pid):
    sf.create_project(pid)
    run = sf.create_run('trace-probe', project_id=pid)
    sf.trace(run, 'tool_result', 'tool_result', {'output': pid + '!' * 2048})
    sf.complete_run(run)
    sf.update_project_status(pid, 'completed')
    return run


def _trace_fds(tmp_path):
    prefix = str(tmp_path / 'traces') + '/'
    paths = []
    for fd in Path('/proc/self/fd').iterdir():
        try:
            p = os.readlink(fd)
        except FileNotFoundError:
            continue
        if p.startswith(prefix):
            paths.append(p)
    return paths


def test_420_terminal_projects_bound_actual_sqlite_fds_and_reopen(tmp_path):
    sf = _engine(tmp_path)
    runs = []
    for i in range(420):
        pid = f'terminal-{i}'
        run = _run(sf, pid)
        runs.append((pid, run))
        assert sf.get_run(run)['status'] == 'completed'
        assert sf.get_project(pid)['status'] == 'completed'
        assert len(sf.get_trace(run)) == 1
        assert len(sf._trace_conns) <= 32
        assert len(_trace_fds(tmp_path)) <= 96
    assert runs[0][0] not in sf._trace_conns
    for pid, run in runs:
        assert sf.get_trace(run)[0]['payload']['output'] == pid + '!' * 2048
        assert sf.trace_query(run, 'SELECT COUNT(*) FROM skillflow_trace')[0][0] == 1
        assert len(_trace_fds(tmp_path)) <= 96


def test_nested_pin_survives_cache_pressure_and_invalidation(tmp_path):
    sf = _engine(tmp_path)
    first = _run(sf, 'pinned')
    with sf.trace_connection('pinned') as conn:
        conn.execute('BEGIN IMMEDIATE')
        for i in range(80):
            _run(sf, f'pressure-{i}')
            assert len(sf._trace_conns) <= 32
        assert conn.execute('SELECT COUNT(*) FROM skillflow_trace').fetchone()[0] == 1
        sf.prune_trace(run_id=first)
        with sf.trace_connection('pinned') as same:
            assert same is conn
            assert same.execute('SELECT 1').fetchone()[0] == 1
        conn.commit()
        assert 'pinned' in sf._trace_conns
    assert 'pinned' not in sf._trace_conns
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    assert len(sf.get_trace(first)) == 1
    assert sf._trace_borrows == {} and sf._trace_close_pending == set()


def test_actual_active_nested_borrows_bound_independent_of_lifetime(tmp_path):
    sf = _engine(tmp_path)
    with ExitStack() as stack:
        conns = [stack.enter_context(sf.trace_connection(f'active-{i}')) for i in range(40)]
        assert len(sf._trace_conns) == len(sf._trace_borrows) == 40
        for i in range(80):
            _run(sf, f'transient-{i}')
            assert len(sf._trace_conns) == 40
        assert all(c.execute('SELECT 1').fetchone()[0] == 1 for c in conns)
    assert len(sf._trace_conns) == 32 and not sf._trace_borrows
    assert len(_trace_fds(tmp_path)) == 96


def test_other_thread_cannot_close_borrow_before_transaction_releases(tmp_path):
    sf = _engine(tmp_path)
    run = _run(sf, 'held')
    started, returned = threading.Event(), threading.Event()
    errors = []

    def invalidate():
        started.set()
        try:
            sf.prune_trace(run_id=run)
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    with sf.trace_connection('held') as conn:
        conn.execute('BEGIN IMMEDIATE')
        t = threading.Thread(target=invalidate)
        t.start()
        assert started.wait(2)
        assert not returned.wait(.05)
        conn.execute("INSERT INTO skillflow_trace (run_id, seq, category, event, payload_json) "
                     "VALUES (?, 2, 'tool_result', 'tool_result', '{}')", (run,))
        conn.commit()
    t.join(5)
    assert not t.is_alive() and returned.is_set() and not errors
    assert [r['seq'] for r in sf.get_trace(run)] == [1, 2]


def test_shared_fallback_and_exception_release(tmp_path):
    sf = SkillFlow(str(tmp_path / 'shared.db'))
    with sf.trace_connection('anything') as conn:
        assert conn is sf._conn
        assert conn.execute('SELECT 1').fetchone()[0] == 1
    sf = _engine(tmp_path)
    with pytest.raises(ValueError):
        with sf.trace_connection('exception'):
            raise ValueError('owned failure')
    assert not sf._trace_borrows
    sf._close_trace_conn('exception')
    assert 'exception' not in sf._trace_conns

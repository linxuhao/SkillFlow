"""Real COMMIT-time fault coverage, adapted from independent SQLite evidence.

Deferred foreign keys fail when SQLite finalizes the transaction, after the
binding INSERT succeeded. No connection, transaction or checkpoint API is mocked.
"""
import json
import sqlite3

import pytest

from skillflow.core import SkillFlow, _TxRollback
from test_legacy_checkpoint_binding import legacy_pause, phases, assert_no_decision


def install_commit_failure(sf):
    sf._conn.execute('PRAGMA foreign_keys=ON')
    with sf._tx() as conn:
        conn.execute('CREATE TABLE binding_parent(id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE binding_commit_failure(parent_id INTEGER REFERENCES binding_parent(id) DEFERRABLE INITIALLY DEFERRED)')
        conn.execute("CREATE TRIGGER fail_binding_commit AFTER INSERT ON skillflow_outbox WHEN NEW.event_type='checkpoint_paused' BEGIN INSERT INTO binding_commit_failure(parent_id) VALUES(917); END")


def persisted_phases(tmp_path):
    with sqlite3.connect(str(tmp_path / 'own.db')) as fresh:
        return fresh.execute("SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused' ORDER BY id").fetchall()


def test_commit_failure_rolls_back_binding_and_ordinary_retry_requires_durability(tmp_path):
    sf, run, _, raw = legacy_pause(tmp_path)
    install_commit_failure(sf)
    before = sf.get_run(run)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY constraint failed'):
            sf.get_checkpoint(run)
        outstanding_transaction = sf._conn.in_transaction
        persisted = persisted_phases(tmp_path)
        retry_identity = None
        retry_error = None
        try:
            retry_identity = sf.get_checkpoint(run)
        except sqlite3.IntegrityError as exc:
            retry_error = str(exc)
        observed = {
            'uncommitted_transaction_after_failed_commit': outstanding_transaction,
            'persisted_phase_count': len(persisted),
            'same_connection_phase_count': len(phases(sf)),
            'retry_returned_identity': retry_identity,
            'retry_error': retry_error,
            'transaction_open_after_retry': sf._conn.in_transaction,
        }
        print('COMMIT_FAILURE_OBSERVATION', json.dumps(observed, sort_keys=True))
        assert not outstanding_transaction
        assert persisted == [(raw,)]
        assert retry_identity is None
        assert retry_error == 'FOREIGN KEY constraint failed'
        assert not sf._conn.in_transaction
        assert len(phases(sf)) == 1 and phases(sf)[0]['payload_json'] == raw
        assert persisted_phases(tmp_path) == [(raw,)]
        assert sf.get_run(run) == before
        assert_no_decision(sf, run)
    finally:
        # Cleanup only the owned synthetic transaction if the regression fails;
        # observations and assertions above never infer success from this cleanup.
        sf._conn.rollback()
    with sf._tx() as conn:
        conn.execute('DROP TRIGGER fail_binding_commit')
    checkpoint = sf.get_checkpoint(run)
    assert checkpoint['outputs'] == {'plan': 'exact retained content'}
    assert len(phases(sf)) == 2
    assert len(persisted_phases(tmp_path)) == 2
    fresh = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
    assert fresh.get_checkpoint(run) == checkpoint
    assert not sf._conn.in_transaction
    assert sf.get_run(run) == before
    assert_no_decision(sf, run)


def test_transaction_commit_failure_rolls_back_all_pending_writes(tmp_path):
    sf = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
    sf._conn.execute('PRAGMA foreign_keys=ON')
    with sf._tx() as conn:
        conn.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY constraint failed'):
        with sf._tx() as conn:
            conn.execute('INSERT INTO child VALUES(917)')
    try:
        assert not sf._conn.in_transaction
        assert sf._conn.execute('SELECT count(*) FROM child').fetchone()[0] == 0
        with sqlite3.connect(str(tmp_path / 'own.db')) as fresh:
            assert fresh.execute('SELECT count(*) FROM child').fetchone()[0] == 0
    finally:
        sf._conn.rollback()
    with sf._tx() as conn:
        conn.execute('INSERT INTO parent VALUES(917)')
        conn.execute('INSERT INTO child VALUES(917)')
    assert not sf._conn.in_transaction
    with sqlite3.connect(str(tmp_path / 'own.db')) as fresh:
        assert fresh.execute('SELECT parent_id FROM child').fetchall() == [(917,)]


def test_intentional_transaction_rollback_still_swallows_only_its_abort(tmp_path):
    sf = SkillFlow(str(tmp_path / 'own.db'), artifact_history=False)
    with sf._tx() as conn:
        conn.execute('CREATE TABLE owned_values(value INTEGER)')
    with sf._tx() as conn:
        conn.execute('INSERT INTO owned_values VALUES(1)')
        raise _TxRollback()
    assert not sf._conn.in_transaction
    assert sf._conn.execute('SELECT count(*) FROM owned_values').fetchone()[0] == 0
    with sf._tx() as conn:
        conn.execute('INSERT INTO owned_values VALUES(2)')
    with sqlite3.connect(str(tmp_path / 'own.db')) as fresh:
        assert fresh.execute('SELECT value FROM owned_values').fetchall() == [(2,)]

import pytest

from tradebot.core.store import Store
from tradebot.execution import submission_inspection as inspection


def test_wal_snapshot_is_diagnostic_and_does_not_clear(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    identifier = store.claim_order_submission('venue', 'client', request_snapshot={'qty': '1'})
    result = inspection.inspect_pending_submissions(path)
    assert result['pending_count'] == 1
    assert result['pending'][0]['submission_id'] == identifier
    assert result['pending'][0]['identity_record_matches'] is True
    assert result['verified'] is False and result['usable_for_reset'] is False
    assert store.has_pending_submission()
    store._exec('DELETE FROM kv WHERE key=?', (f'order_identity:{identifier}',))
    assert inspection.inspect_pending_submissions(path)['pending'][0]['identity_record_matches'] is False
    store.close()


@pytest.mark.parametrize('failure', ['malformed', 'row_limit', 'byte_limit', 'total_limit'])
def test_invalid_or_oversized_snapshot_fails_closed(tmp_path, monkeypatch, failure):
    path = tmp_path / 'state.db'
    store = Store(path)
    identifier = store.claim_order_submission('venue', 'client')
    if failure == 'malformed':
        store.kv_set(f'submission_pending:{identifier}', {'venue': 'other', 'client_id': 'client'})
    else:
        monkeypatch.setattr(inspection, {'row_limit': '_MAX_ROWS', 'byte_limit': '_MAX_BYTES',
                                       'total_limit': '_MAX_TOTAL_BYTES'}[failure], 0)
    with pytest.raises(inspection.SubmissionInspectionError):
        inspection.inspect_pending_submissions(path)
    assert store.has_pending_submission()
    store.close()


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(inspection.SubmissionInspectionError):
        inspection.inspect_pending_submissions(path)
    assert not path.exists()

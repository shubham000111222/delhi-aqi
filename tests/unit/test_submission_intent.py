"""Submission fences are durable containment, not exchange replay authority."""
import sqlite3

import pytest

from tradebot.config import Mode
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch


def test_pending_intent_survives_restart_and_blocks_reset(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    identifier = store.claim_order_submission('venue', 'client')
    store.close()
    store = Store(path)
    switch = KillSwitch(store, mode=Mode.PAPER, kill_file=None)
    assert not switch.can_open_entries()
    switch.trigger(KillLevel.L1, trigger='test', reason='test')
    with pytest.raises(PermissionError):
        switch.reset(reason='not evidence')
    assert store.execution_recovery_status()['unresolved_submissions'] == 1
    store.acknowledge_order_submission(identifier)
    assert not store.has_pending_submission()
    with pytest.raises(ValueError, match='already reserved'):
        store.claim_order_submission('venue', 'client')
    store.close()


@pytest.mark.parametrize('kind', ['order_submission_intent', 'order_submission_observed'])
def test_intent_audit_transactions_roll_back(kind):
    store = Store(':memory:')
    if kind == 'order_submission_observed':
        identifier = store.claim_order_submission('venue', 'client')
    store._exec(f"CREATE TRIGGER fail_intent BEFORE INSERT ON events WHEN NEW.kind='{kind}' "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        if kind == 'order_submission_intent':
            store.claim_order_submission('venue', 'client')
        else:
            store.acknowledge_order_submission(identifier)
    assert store.has_pending_submission() == (kind == 'order_submission_observed')
    store.close()


@pytest.mark.parametrize('damage', ['missing_identity', 'changed_pending', 'changed_identity', 'both_changed'])
def test_inconsistent_intent_cannot_be_acknowledged(damage):
    store = Store(':memory:')
    identifier = store.claim_order_submission('venue', 'client', request_snapshot={'qty': '1.25'})
    if damage == 'missing_identity':
        store._exec('DELETE FROM kv WHERE key=?', (f'order_identity:{identifier}',))
    if damage in ('changed_pending', 'both_changed'):
        store.kv_set(f'submission_pending:{identifier}', {'venue': 'other', 'client_id': 'changed', 'request': None})
    if damage in ('changed_identity', 'both_changed'):
        store.kv_set(f'order_identity:{identifier}', {'venue': 'other', 'client_id': 'changed', 'request': None})
    with pytest.raises(ValueError):
        store.acknowledge_order_submission(identifier)
    assert store.has_pending_submission()
    assert not store._query("SELECT 1 FROM events WHERE kind='order_submission_observed'")
    store.close()


def test_request_snapshot_does_not_follow_callers_later_mutation():
    store = Store(':memory:')
    request = {'qty': '1.25', 'reduce_only': False}
    identifier = store.claim_order_submission('venue', 'client', request_snapshot=request)
    request['qty'] = '100'
    assert store.kv_get(f'order_identity:{identifier}')['request']['qty'] == '1.25'
    store.acknowledge_order_submission(identifier)
    assert store.kv_get(f'order_identity:{identifier}')['request']['qty'] == '1.25'
    store.close()

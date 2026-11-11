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

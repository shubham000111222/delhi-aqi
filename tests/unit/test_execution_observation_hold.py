"""Invalid venue observations must not become entry permission after restart."""
import sqlite3

import pytest

from tradebot.config import Mode
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch


def test_observation_hold_survives_restart_and_blocks_reset(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    switch = KillSwitch(store, mode=Mode.PAPER, kill_file=None)
    switch.latch_execution_observation_fault('client1')
    store.close()
    store = Store(path)
    switch = KillSwitch(store, mode=Mode.PAPER, kill_file=None)
    assert not switch.can_open_entries()
    switch.trigger(KillLevel.L1, trigger='test', reason='test')
    with pytest.raises(PermissionError):
        switch.reset(reason='not recovery evidence')
    assert not store.set_kill_state('L0', require_no_pending_incident=True)
    store.kv_set('execution_observation_fault', 'malformed')
    assert not switch.can_open_entries()
    store.close()


def test_hold_is_first_fault_latched_and_audited_once():
    store = Store(':memory:')
    store.latch_execution_observation_fault('first')
    store.latch_execution_observation_fault('second')
    assert store.kv_get('execution_observation_fault')['order_id'] == 'first'
    assert len(store._query("SELECT 1 FROM events WHERE kind='execution_observation_fault'")) == 1
    store.close()


def test_audit_failure_rolls_back_durable_hold_but_keeps_local_hold():
    store = Store(':memory:')
    switch = KillSwitch(store, mode=Mode.PAPER, kill_file=None)
    store._exec("CREATE TRIGGER fail_observation BEFORE INSERT ON events "
                "WHEN NEW.kind='execution_observation_fault' BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.latch_execution_observation_fault('client')
    assert not store.has_execution_observation_fault()
    switch.latch_execution_observation_fault('client')
    assert not switch.can_open_entries()
    store.close()

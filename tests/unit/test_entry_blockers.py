import pytest

from tradebot.config import Mode
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch


def test_local_and_durable_blocker_visibility_does_not_release(tmp_path):
    store = Store(':memory:')
    try:
        switch = KillSwitch(store, kill_file=tmp_path/'KILL', mode=Mode.PAPER)
        assert switch.entry_blockers() == ()
        switch.latch_local_entry_hold()
        store.latch_execution_observation_fault('test-order')
        before = store.kill_state()
        assert 'local_entry_hold' in switch.entry_blockers()
        assert 'execution_observation_fault' in switch.entry_blockers()
        assert not switch.can_open_entries()
        assert store.kill_state() == before
        assert switch.entry_blockers() == switch.entry_blockers()
    finally:
        store.close()


def test_kill_file_diagnostic_does_not_mutate_kill_state(tmp_path):
    store = Store(':memory:')
    try:
        path = tmp_path/'KILL'
        switch = KillSwitch(store, kill_file=path, mode=Mode.PAPER)
        path.write_text('operator stop')
        before = store.kill_state()
        assert 'kill_file_present' in switch.entry_blockers()
        assert store.kill_state() == before and path.exists()
    finally:
        store.close()


def test_failed_blocker_read_is_not_reported_safe(monkeypatch):
    store = Store(':memory:')
    try:
        switch = KillSwitch(store, kill_file=None, mode=Mode.PAPER)

        def fail():
            raise OSError('unavailable hold state')

        monkeypatch.setattr(store, 'has_pending_fill', fail)
        with pytest.raises(OSError):
            switch.entry_blockers()
    finally:
        store.close()

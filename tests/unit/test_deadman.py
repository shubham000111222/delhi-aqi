"""Dead man's switch: armed only where it protects, and it latches (§20.5).

Three properties, in order of how much damage getting them wrong causes: it must not fire
in paper (a switch that cries wolf gets disabled), it must fire exactly once (a switch that
re-flattens every pass closes the position a human just re-opened), and it must refuse a
reset while the pulse is still missing (that is the state in which resetting is a lie).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.core.clock import SimClock
from tradebot.watchdog.deadman import DeadMansSwitch

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def beat(tmp_path: Path) -> Path:
    path = tmp_path / "heartbeat"
    path.write_text(T0.isoformat(), encoding="utf-8")
    _set_mtime(path, T0)
    return path


def _set_mtime(path: Path, ts: datetime) -> None:
    stamp = ts.timestamp()
    os.utime(path, (stamp, stamp))


def switch(path: Path, *, mode: str = "live", stale_after: float = 30.0) -> DeadMansSwitch:
    return DeadMansSwitch(
        heartbeat_path=path,
        stale_after=timedelta(seconds=stale_after),
        mode=mode,
        clock=SimClock(T0),
    )


class TestArming:
    def test_paper_mode_is_never_armed(self, tmp_path: Path):
        missing = tmp_path / "nothing-here"
        sw = switch(missing, mode="paper")

        assert sw.armed is False
        assert sw.check() is False, "there is nothing to close in paper, so nothing may fire"
        assert sw.state()["armed"] is False

    @pytest.mark.parametrize("mode", ["testnet", "live"])
    def test_testnet_and_live_are_armed(self, beat: Path, mode: str):
        assert switch(beat, mode=mode).armed is True


class TestTripping:
    def test_a_missing_file_counts_as_stale(self, tmp_path: Path):
        sw = switch(tmp_path / "gone")
        assert sw.age() is None
        assert sw.check() is True

    def test_a_fresh_heartbeat_does_not_trip(self, beat: Path):
        sw = switch(beat)
        assert sw.stale() is False
        assert sw.check() is False

    def test_an_old_heartbeat_trips(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(minutes=5))
        sw = switch(beat)

        # the clock is a SimClock at T0 and the file is five minutes older
        assert sw.check() is True
        assert sw.tripped is True

    def test_it_trips_only_once(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(minutes=5))
        sw = switch(beat)

        assert sw.check() is True
        assert sw.check() is False, "a second trip would flatten the position a human re-opened"
        assert sw.check() is False

    def test_the_state_dict_says_why(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(minutes=2))
        sw = switch(beat)
        sw.check()

        state = sw.state()
        assert state["tripped"] is True
        assert state["armed"] is True
        assert state["heartbeat_age_seconds"] == pytest.approx(120, abs=1)
        assert state["stale_after_seconds"] == 30
        assert state["tripped_at"] is not None


class TestResetting:
    def test_a_reset_is_refused_while_the_heartbeat_is_still_dead(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(minutes=5))
        sw = switch(beat)
        sw.check()

        assert sw.reset() is False
        assert sw.tripped is True, "resetting while the pulse is missing is a lie"

    def test_a_reset_works_once_the_heartbeat_is_fresh(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(minutes=5))
        sw = switch(beat)
        sw.check()

        _set_mtime(beat, T0)  # the bot comes back
        assert sw.reset() is True
        assert sw.tripped is False
        assert sw.resets == 1
        assert sw.check() is False

    def test_resetting_a_switch_that_never_tripped_does_nothing(self, beat: Path):
        sw = switch(beat)
        assert sw.reset() is False
        assert sw.resets == 0

    def test_it_can_trip_again_after_a_reset(self, beat: Path):
        sw = switch(beat)
        _set_mtime(beat, T0 - timedelta(minutes=5))
        assert sw.check() is True
        _set_mtime(beat, T0)
        assert sw.reset() is True

        _set_mtime(beat, T0 - timedelta(minutes=5))
        assert sw.check() is True, "a switch that can only ever fire once is a switch you replace"


class TestThreshold:
    def test_the_threshold_is_configurable(self, beat: Path):
        _set_mtime(beat, T0 - timedelta(seconds=20))
        strict = switch(beat, stale_after=10)
        lenient = switch(beat, stale_after=60)

        assert strict.check() is True
        assert lenient.check() is False

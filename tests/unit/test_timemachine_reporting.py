from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.store import Store
from tradebot.reporting.timemachine import check_history, order_ticket

START = datetime(2024, 1, 1, tzinfo=UTC)


def test_ticket_uses_only_recorded_metrics_and_warns():
    ticket = order_ticket({'trade_id': 'id', 'verdict': 'DOWN', 'multiplier': .5,
                           'research_only': True, 'simulation': {'target_first': .4,
                           'expected_net_r': .08, 'p05_r': -1.2}, 'replay': {'insufficient': True}})
    assert '40.0%' in ticket and '0.080 R' in ticket and '-1.200 R' in ticket
    assert 'unavailable' in ticket and 'RESEARCH ONLY' in ticket
    assert 'not a profit promise' in ticket


def test_ticket_does_not_print_nan_or_invent_missing_statistics():
    ticket = order_ticket({'simulation': {'expected_net_r': float('nan')}})
    assert 'Net mean unavailable' in ticket and 'nan' not in ticket


def test_history_is_read_only_bounded_and_excludes_future_day():
    store = Store(':memory:')
    try:
        for i in range(6):
            store.log_event('timemachine_check', {'trade_id': str(i), 'multiplier': 0},
                            ts=START + timedelta(hours=i))
        store.log_event('timemachine_check', {'trade_id': 'tomorrow'}, ts=START + timedelta(days=1))
        page = check_history(store, since=START, until=START + timedelta(days=1), limit=5)
        assert page['truncated'] is True
        assert [r['check']['trade_id'] for r in page['checks']] == ['5', '4', '3', '2', '1']
        assert len(store.events()) == 7
        assert not page['filter_enabled']
    finally:
        store.close()


@pytest.mark.parametrize('limit', [0, 1001])
def test_limit_validation(limit):
    store = Store(':memory:')
    try:
        with pytest.raises(ValueError):
            check_history(store, limit=limit)
    finally:
        store.close()

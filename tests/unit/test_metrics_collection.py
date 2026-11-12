from decimal import Decimal
from unittest.mock import Mock

import pytest

from tradebot.backtest.runner import EvidenceLimitError, collect

STAMP = '2024-01-01T00:00:00+00:00'
POINT = {'ts': STAMP, 'equity': Decimal(1000), 'positions_value': Decimal(100)}
TRADE = {'symbol': 'BTC/USDT', 'strategy': 'test', 'pnl': '2', 'r_multiple': '.5',
         'opened_at': STAMP, 'closed_at': STAMP, 'fees': '.1'}


def test_exact_limit_is_complete_and_fetches_one_extra():
    store = Mock()
    store.equity_series.return_value = [POINT]*3
    store.closed_positions.return_value = [TRADE]*3
    curve, trades, exposure = collect(store, max_records=3)
    assert len(curve) == len(trades) == len(exposure) == 3
    assert exposure == [.1]*3
    assert all(t.pnl == Decimal(2) and t.fees == Decimal('.1') for t in trades)
    store.equity_series.assert_called_once_with(limit=4)
    store.closed_positions.assert_called_once_with(limit=4)


@pytest.mark.parametrize('table', ['equity', 'trades'])
def test_overflow_never_returns_truncated_metrics_inputs(table):
    store = Mock()
    store.equity_series.return_value = [POINT] * (4 if table == 'equity' else 3)
    store.closed_positions.return_value = [TRADE] * (4 if table == 'trades' else 3)
    with pytest.raises(EvidenceLimitError):
        collect(store, max_records=3)


@pytest.mark.parametrize('value', [True, 0, -1, 100001, 1.5])
def test_invalid_limits_do_not_query(value):
    store = Mock()
    with pytest.raises(ValueError):
        collect(store, max_records=value)
    store.equity_series.assert_not_called()


def test_empty_evidence_is_not_manufactured():
    store = Mock()
    store.equity_series.return_value = []
    store.closed_positions.return_value = []
    assert collect(store) == ([], [], [])


@pytest.mark.parametrize('field,value', [('ts', None), ('ts', '2024-01-01'),
                                        ('equity', 'NaN'), ('positions_value', 'Infinity'), ('equity', True)])
def test_invalid_equity_evidence_refused(field, value):
    from tradebot.backtest.runner import EvidenceDataError

    store = Mock()
    store.equity_series.return_value = [{**POINT, field: value}]
    store.closed_positions.return_value = []
    with pytest.raises(EvidenceDataError):
        collect(store)


@pytest.mark.parametrize('field,value', [('opened_at', None), ('closed_at', ''),
                                        ('pnl', None), ('r_multiple', 'NaN'), ('fees', None),
                                        ('symbol', None), ('strategy', '')])
def test_invalid_trade_evidence_never_defaults_to_zero_or_current_time(field, value):
    from tradebot.backtest.runner import EvidenceDataError

    store = Mock()
    store.equity_series.return_value = []
    store.closed_positions.return_value = [{**TRADE, field: value}]
    with pytest.raises(EvidenceDataError):
        collect(store)


def test_backwards_equity_and_trade_times_refused():
    from tradebot.backtest.runner import EvidenceDataError

    store = Mock()
    store.equity_series.return_value = [{**POINT, 'ts': '2024-01-02T00:00:00Z'}, POINT]
    store.closed_positions.return_value = []
    with pytest.raises(EvidenceDataError, match='chronological'):
        collect(store)
    store.equity_series.return_value = []
    store.closed_positions.return_value = [{**TRADE, 'opened_at': '2024-01-02T00:00:00Z'}]
    with pytest.raises(EvidenceDataError, match='before'):
        collect(store)

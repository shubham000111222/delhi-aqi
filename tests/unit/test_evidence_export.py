import json
from decimal import Decimal

import pytest

from tradebot.backtest.evidence_export import publish_evidence


def test_complete_evidence_and_no_overwrite(tmp_path):
    path = tmp_path / 'research.json'
    publish_evidence(path, {'complete': True, 'filter_enabled': False, 'net': Decimal('.1')})
    assert json.loads(path.read_text()) == {'complete': True, 'filter_enabled': False, 'net': '0.1'}
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        publish_evidence(path, {'overwrite': True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('.timemachine-*.tmp'))


@pytest.mark.parametrize('failure', ['nan', 'size', 'keys'])
def test_serialization_failure_leaves_no_partial_destination(tmp_path, failure):
    path = tmp_path / 'research.json'
    result = {'bad': float('nan')} if failure == 'nan' else {'value': 'x'*100}
    if failure == 'keys':
        result = {(1, 2): 'unsupported JSON key'}
    with pytest.raises((ValueError, TypeError)):
        publish_evidence(path, result, max_bytes=20 if failure == 'size' else 1000)
    assert not path.exists()
    assert not list(tmp_path.glob('.timemachine-*.tmp'))


def test_racing_creator_is_preserved(tmp_path, monkeypatch):
    import tradebot.backtest.evidence_export as module

    path = tmp_path / 'research.json'
    original = module.os.link

    def race(source, destination):
        destination.write_text('existing evidence')
        original(source, destination)

    monkeypatch.setattr(module.os, 'link', race)
    with pytest.raises(FileExistsError):
        publish_evidence(path, {'new': 'evidence'})
    assert path.read_text() == 'existing evidence'
    assert not list(tmp_path.glob('.timemachine-*.tmp'))


def test_unsupported_filesystem_fails_without_fallback(tmp_path, monkeypatch):
    import tradebot.backtest.evidence_export as module

    def unavailable(*args):
        raise OSError('hard links unavailable')

    monkeypatch.setattr(module.os, 'link', unavailable)
    path = tmp_path / 'research.json'
    with pytest.raises(OSError):
        publish_evidence(path, {'complete': True})
    assert not path.exists()
    assert not list(tmp_path.glob('.timemachine-*.tmp'))

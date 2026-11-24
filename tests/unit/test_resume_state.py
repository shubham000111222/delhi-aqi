"""Failure-safe resume metadata publication; never archive provenance."""
import json

import pytest

from tradebot.data import resume_state


def test_resume_state_replaces_complete_document(tmp_path):
    path = tmp_path / 'nested' / 'state.json'
    resume_state.write_resume_state(path, {'chunks': {'old': True}})
    resume_state.write_resume_state(path, {'chunks': {'new': True}})
    assert json.loads(path.read_text()) == {'chunks': {'new': True}}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('failure', ['sync', 'replace', 'serialize', 'oversize'])
def test_failed_resume_write_preserves_previous_bytes_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{"old":true}')
    state = {'new': True}

    def fail(*args):
        raise OSError('injected write failure')

    if failure == 'sync':
        monkeypatch.setattr(resume_state.os, 'fsync', fail)
    elif failure == 'replace':
        monkeypatch.setattr(resume_state.os, 'replace', fail)
    elif failure == 'serialize':
        state = {'invalid': float('nan')}
    else:
        monkeypatch.setattr(resume_state, 'MAX_STATE_BYTES', 2)
    with pytest.raises((OSError, ValueError)):
        resume_state.write_resume_state(path, state)
    assert path.read_bytes() == b'{"old":true}'
    assert list(tmp_path.iterdir()) == [path]

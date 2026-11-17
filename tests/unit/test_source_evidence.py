import pytest

from tradebot.backtest.source_evidence import source_fingerprint


def test_source_content_and_names_determine_identity(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        (root / 'one.py').write_text('value = 1\n')
        (root / 'two.py').write_text('value = 2\n')
    original = source_fingerprint(a)
    assert source_fingerprint(b) == original
    (a / 'notes.txt').write_text('not Python source')
    assert source_fingerprint(a) == original
    (a / 'one.py').write_text('value = 3\n')
    assert source_fingerprint(a)['sha256'] != original['sha256']
    (b / 'one.py').rename(b / 'renamed.py')
    assert source_fingerprint(b)['sha256'] != original['sha256']


def test_empty_or_oversized_source_refused(tmp_path):
    with pytest.raises(ValueError, match='unavailable'):
        source_fingerprint(tmp_path)
    (tmp_path / 'large.py').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='limit'):
        source_fingerprint(tmp_path)


def test_linked_source_refused(tmp_path):
    ordinary = tmp_path / 'ordinary.py'
    ordinary.write_text('value = 1')
    try:
        (tmp_path / 'linked.py').symlink_to(ordinary)
    except OSError:
        pytest.skip('host does not permit symlink creation')
    with pytest.raises(ValueError, match='symbolic link'):
        source_fingerprint(tmp_path)


def test_current_package_is_identified():
    result = source_fingerprint()
    assert len(result['sha256']) == 64
    assert 'engine.py' in result['files']
    assert 'risk/engine.py' in result['files']

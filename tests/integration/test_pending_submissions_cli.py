import json

from tradebot import main as cli
from tradebot.core.store import Store


def test_pending_submissions_cli(fast_cfg, monkeypatch, capsys):
    fast_cfg.ensure_dirs()
    store = Store(fast_cfg.settings.db_path)
    store.claim_order_submission('venue', 'client')
    monkeypatch.setattr(cli, '_config', lambda args: fast_cfg)
    assert cli.main(['pending-submissions']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pending_count'] == 1
    assert result['verified'] is False
    assert store.has_pending_submission()
    store.close()

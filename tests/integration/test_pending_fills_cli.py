import json

from tradebot import main as cli
from tradebot.core.store import Store


def test_pending_fills_cli_is_diagnostic_only(fast_cfg, monkeypatch, capsys):
    fast_cfg.ensure_dirs()
    store = Store(fast_cfg.settings.db_path)
    monkeypatch.setattr(cli, "_config", lambda args: fast_cfg)
    assert cli.main(["pending-fills"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["pending_count"] == 0
    assert output["verified"] is False
    assert output["usable_for_reset"] is False
    store.close()


def test_pending_fills_cli_missing_db_fails(fast_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_config", lambda args: fast_cfg)
    assert cli.main(["pending-fills"]) == 1
    assert "inspection failed" in capsys.readouterr().err

"""Live and testnet CLI reset must not trust a claimed operator name."""
import yaml

from tradebot import main as cli
from tradebot.config import load_config
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch


def test_cli_live_reset_is_refused(fast_cfg, tmp_path, capsys):
    cdir = fast_cfg.settings.config_dir
    settings_file = cdir / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text())
    settings["data_dir"] = str(tmp_path / "data")
    settings["report"]["dir"] = str(tmp_path / "reports")
    settings_file.write_text(yaml.safe_dump(settings))
    config = load_config(cdir, environ={}, overrides={"mode": "live"})
    store = Store(config.settings.db_path)
    switch = KillSwitch(store, mode=config.mode, kill_file=None)
    switch.trigger(KillLevel.L1, trigger="manual", reason="pause")
    store.close()
    assert cli.main(["resume", "--config-dir", str(cdir), "--mode", "live", "--reason", "reviewed"]) == 2
    assert "authenticated authorization" in capsys.readouterr().err
    store = Store(config.settings.db_path)
    assert store.kill_level() == "L1"
    assert store.count_events("manual_reset_refused") == 1
    store.close()

"""Exercise offline key/sign tooling and allocation CLI without connecting to a venue."""
import json
import sys

import yaml
from tools import generate_operator_key, sign_recovery

from tradebot import main as cli
from tradebot.config import load_config
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.risk.live_guards import RampController


def test_signed_allocation_cli(fast_cfg, tmp_path, capsys, monkeypatch):
    cdir = fast_cfg.settings.config_dir
    settings_file = cdir / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text())
    settings["data_dir"] = str(tmp_path / "data")
    settings["report"]["dir"] = str(tmp_path / "reports")
    settings_file.write_text(yaml.safe_dump(settings))
    private_path = tmp_path / "key.pem"
    bundle_path = cdir.parent / "operator-keys.json"
    monkeypatch.setattr(sys, "argv", ["keygen", "--private-key", str(private_path), "--public-bundle", str(bundle_path)])
    monkeypatch.setattr(generate_operator_key.getpass, "getpass", lambda prompt: "test passphrase 123")
    assert generate_operator_key.main() == 0
    fingerprint = next(iter(json.loads(bundle_path.read_text())["keys"]))
    capsys.readouterr()
    assert cli.main(["allocation-request", "--mode", "live", "--config-dir", str(cdir),
                     "--key-id", fingerprint, "--reason", "reduce exposure", "--cap", "0.03"]) == 0
    request = json.loads(capsys.readouterr().out)
    reqfile, sigfile = tmp_path / "request.json", tmp_path / "signature.txt"
    reqfile.write_text(json.dumps(request), encoding="utf-8-sig")
    monkeypatch.setattr(sys, "argv", ["sign", "--private-key", str(private_path), "--request", str(reqfile),
                                      "--signature", str(sigfile)])
    monkeypatch.setattr(sign_recovery.getpass, "getpass", lambda prompt: "test passphrase 123")
    monkeypatch.setattr("builtins.input", lambda prompt: "SIGN")
    assert sign_recovery.main() == 0
    assert '"new_cap": "0.03"' in capsys.readouterr().out
    assert cli.main(["allocation-apply", "--mode", "live", "--config-dir", str(cdir),
                     "--request", str(reqfile), "--signature", str(sigfile)]) == 0
    assert "live readiness and kill level unchanged" in capsys.readouterr().out
    cfg = load_config(cdir, overrides={"mode": "live"}, environ={})
    store = Store(cfg.settings.db_path)
    try:
        assert RampController(cfg, store).multiplier() == D("0.03")
    finally:
        store.close()

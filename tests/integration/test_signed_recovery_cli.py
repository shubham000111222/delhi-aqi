"""Exercise the offline signing workflow through the real CLI against a paper DB."""
import json
import sys

import yaml
from tools import generate_operator_key, sign_recovery

from tradebot import main as cli
from tradebot.config import Mode, load_config
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch


def test_signed_recovery_cli_requests_and_consumes(fast_cfg, tmp_path, capsys, monkeypatch):
    cdir = fast_cfg.settings.config_dir
    settings_file = cdir / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text())
    settings["data_dir"] = str(tmp_path / "cli-data")
    settings["report"]["dir"] = str(tmp_path / "cli-reports")
    settings_file.write_text(yaml.safe_dump(settings))
    config = load_config(cdir, environ={})
    private_path = tmp_path / "private.pem"
    bundle_path = cdir.parent / "operator-keys.json"
    monkeypatch.setattr(sys, "argv", ["generate_operator_key", "--private-key", str(private_path),
                                      "--public-bundle", str(bundle_path)])
    monkeypatch.setattr(generate_operator_key.getpass, "getpass", lambda prompt: "test passphrase 123")
    assert generate_operator_key.main() == 0
    fingerprint = next(iter(json.loads(bundle_path.read_text())["keys"]))
    assert b"ENCRYPTED PRIVATE KEY" in private_path.read_bytes()
    capsys.readouterr()
    store = Store(config.settings.db_path)
    switch = KillSwitch(store, kill_file=None, mode=Mode.PAPER)
    store._exec("CREATE TRIGGER fail_outbox BEFORE INSERT ON kv WHEN NEW.key GLOB 'kill_incident_pending:*' "
                "BEGIN SELECT RAISE(ABORT, 'outbox failed'); END")
    assert switch.trigger(KillLevel.L2, trigger="test", reason="critical")
    store._exec("DROP TRIGGER fail_outbox")
    store.close()
    assert cli.main(["recovery-request", "--config-dir", str(cdir), "--key-id", fingerprint,
                     "--reason", "reviewed missing history"]) == 0
    request = json.loads(capsys.readouterr().out)
    request_path = tmp_path / "request.json"
    signature_path = tmp_path / "request.sig"
    request_path.write_text(json.dumps(request), encoding="utf-8-sig")
    monkeypatch.setattr(sys, "argv", ["sign_recovery", "--private-key", str(private_path),
                                      "--request", str(request_path), "--signature", str(signature_path)])
    monkeypatch.setattr(sign_recovery.getpass, "getpass", lambda prompt: "test passphrase 123")
    monkeypatch.setattr("builtins.input", lambda prompt: "SIGN")
    assert sign_recovery.main() == 0
    output = capsys.readouterr().out
    assert '"state":' in output and '"level": "L2"' in output
    assert cli.main(["recovery-apply", "--config-dir", str(cdir), "--request", str(request_path),
                     "--signature", str(signature_path)]) == 0
    assert "kill level unchanged" in capsys.readouterr().out
    store = Store(config.settings.db_path)
    assert store.kill_level() == "L2"
    assert store.kill_state()["incident_tracking_failed"] is False
    assert store.has_invalid_ramp_tracking()
    assert store.count_events("signed_tracking_fault_ack") == 1
    store.close()

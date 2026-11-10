"""Exercise the offline signing workflow through the real CLI against a paper DB."""
import base64
import json

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradebot import main as cli
from tradebot.config import load_config
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.risk.killswitch import KillSwitch
from tradebot.signed_recovery import canonical_bytes, key_id


def test_signed_recovery_cli_requests_and_consumes(fast_cfg, tmp_path, capsys):
    cdir = fast_cfg.settings.config_dir
    settings_file = cdir / "settings.yaml"
    settings = yaml.safe_load(settings_file.read_text())
    settings["data_dir"] = str(tmp_path / "cli-data")
    settings["report"]["dir"] = str(tmp_path / "cli-reports")
    settings_file.write_text(yaml.safe_dump(settings))
    config = load_config(cdir, environ={})
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = key_id(public)
    (cdir.parent / "operator-keys.json").write_text(json.dumps({
        "version": 1, "keys": {fingerprint: base64.b64encode(public).decode()},
    }))
    store = Store(config.settings.db_path)
    switch = KillSwitch(store, kill_file=None)
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
    signature_path.write_text(base64.b64encode(private.sign(canonical_bytes(request))).decode(), encoding="utf-8-sig")
    assert cli.main(["recovery-apply", "--config-dir", str(cdir), "--request", str(request_path),
                     "--signature", str(signature_path)]) == 0
    assert "kill level unchanged" in capsys.readouterr().out
    store = Store(config.settings.db_path)
    assert store.kill_level() == "L2"
    assert store.kill_state()["incident_tracking_failed"] is False
    assert store.has_invalid_ramp_tracking()
    assert store.count_events("signed_tracking_fault_ack") == 1
    store.close()

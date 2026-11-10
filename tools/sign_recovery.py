"""Sign a reviewed recovery request on an offline operator machine.

Requires cryptography. Private key must be encrypted PKCS#8 PEM and kept outside this repo.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--signature", required=True)
    args = parser.parse_args()
    private_path = Path(args.private_key).resolve()
    repo = Path(__file__).resolve().parent.parent
    if private_path.is_relative_to(repo):
        raise ValueError("private key must be kept outside this repository")
    request_path = Path(args.request)
    if request_path.stat().st_size > 16384 or private_path.stat().st_size > 16384:
        raise ValueError("input file too large")
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    if not isinstance(request, dict) or request.get("action") != "acknowledge_kill_incident_tracking_failure":
        raise ValueError("unsupported action")
    print(json.dumps(request, indent=2, sort_keys=True))
    if input("Review the request and type SIGN to approve: ").strip() != "SIGN":
        print("Not signed.")
        return 1
    password = getpass.getpass("Private-key passphrase: ").encode()
    if not password:
        raise ValueError("an encrypted private key and nonempty passphrase are required")
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("an Ed25519 private key is required")
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if request.get("key_id") != hashlib.sha256(public).hexdigest():
        raise ValueError("private key does not match requested key fingerprint")
    message = json.dumps(request, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode()
    signature = base64.b64encode(key.sign(message)).decode()
    with Path(args.signature).open("x", encoding="ascii") as out:
        out.write(signature + "\n")
    print("Signature written; keep the private key offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

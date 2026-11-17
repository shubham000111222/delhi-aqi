"""Create an encrypted Ed25519 private key outside the repository and a public trust bundle."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--public-bundle", required=True)
    args = parser.parse_args()
    private_path = Path(args.private_key).resolve()
    if private_path.is_relative_to(Path(__file__).resolve().parent.parent):
        raise ValueError("private key must be kept outside the repository")
    public_path = Path(args.public_bundle).resolve()
    if private_path == public_path or private_path.exists() or public_path.exists():
        raise FileExistsError("key and bundle paths must be different and must not already exist")
    password = getpass.getpass("New private-key passphrase: ")
    confirmation = getpass.getpass("Repeat private-key passphrase: ")
    if len(password) < 12 or password != confirmation:
        raise ValueError("passphrases must match and be at least 12 characters")
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode()))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = hashlib.sha256(public).hexdigest()
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(private)
    with public_path.open("x", encoding="utf-8") as out:
        json.dump({"version": 1, "keys": {fingerprint: base64.b64encode(public).decode()}}, out, indent=2)
        out.write("\n")
    print(f"Public key fingerprint: {fingerprint}")
    print("Keep the private key offline and protect it with OS access controls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

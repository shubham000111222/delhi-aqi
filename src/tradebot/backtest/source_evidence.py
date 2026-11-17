"""Bounded local Python-source identity; not signatures or venue provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def source_fingerprint(root: Path | None = None) -> dict[str, object]:
    """Hash exact package Python bytes, with deterministic relative-name ordering.

    Refuse linked files/directories and oversized trees rather than silently omit
    code. This is a local snapshot, not proof against a privileged process swapping
    code and swapping it back, monkeypatching memory, or editing this verifier.
    Dependencies/interpreter/config are not included in this Python-source digest.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    if root.is_symlink() or not root.is_dir():
        raise ValueError('source root must be an ordinary directory')
    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('source tree contains a symbolic link')
        if path.suffix != '.py' or not path.is_file():
            continue
        if len(files) >= 512 or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('source evidence file/count limit exceeded')
        data = path.read_bytes()
        total += len(data)
        if len(data) > 2 * 1024 * 1024 or total > 16 * 1024 * 1024:
            raise ValueError('source evidence byte limit exceeded')
        files[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    if not files:
        raise ValueError('Python source evidence unavailable')
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'sha256': digest, 'files': files, 'bytes': total,
            'scope': 'local tradebot Python source only; dependencies and interpreter excluded'}

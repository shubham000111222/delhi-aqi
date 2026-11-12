"""Bounded no-clobber research JSON publication; no promotion authority."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def publish_evidence(path: Path, result: dict[str, Any], *, max_bytes: int = 64 * 1024 * 1024) -> None:
    """Publish a complete JSON file or leave the destination untouched.

    A same-directory hard link installs the flushed temporary inode atomically and
    refuses an existing destination (including races/symlinks). There is no unsafe
    overwrite fallback. Filesystems without hard links fail closed. A process crash
    can leave a hidden temporary file, never a partly serialized destination.
    File fsync is used; power-loss directory durability/Windows host support still
    require deployment verification. Concurrent mutation of result is not supported.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError('evidence byte limit must be1..67108864')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError('evidence destination already exists')
    descriptor, temporary = tempfile.mkstemp(prefix='.timemachine-', suffix='.tmp', dir=path.parent)
    scratch = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            size = 0
            encoder = json.JSONEncoder(indent=2, default=str, allow_nan=False)
            for chunk in encoder.iterencode(result):
                encoded = chunk.encode('utf-8')
                size += len(encoded)
                if size > max_bytes:
                    raise ValueError('research evidence exceeds byte limit')
                stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(scratch, path)  # atomic no-clobber; never replace an existing report
    finally:
        # Cleanup failure must not destroy a successfully published report.
        # Hidden scratch files may be removed by an operator after inspection.
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)

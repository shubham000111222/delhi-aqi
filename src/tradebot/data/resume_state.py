"""Bounded atomic replacement of local resume metadata, not archive certification."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_STATE_BYTES = 4 * 1024 * 1024


def write_resume_state(path: Path, state: dict[str, Any]) -> None:
    """Preserve the old destination on serialization/write/replace failure.

    Same-directory replacement avoids cross-device rename. File fsync does not
    guarantee directory power-loss durability. Concurrent writers are unsupported.
    """
    payload = json.dumps(state, indent=2, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError('resume state exceeds 4 MiB')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

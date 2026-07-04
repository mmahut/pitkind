from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def output_filename(proposal_id: str, started_at: datetime) -> str:
    ts = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    safe_id = proposal_id.replace("/", "_").replace(" ", "_")
    return f"{safe_id}__{ts}.json"


def atomic_write_json(path: Path, data: dict, *, overwrite: bool = True) -> None:
    _atomic_write(path, data, overwrite=overwrite)


def atomic_write_bytes(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    _atomic_write(path, data, overwrite=overwrite)


def _atomic_write(path: Path, data: dict | bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            if isinstance(data, bytes):
                f.write(data)
            else:
                json.dump(data, f, indent=2, default=str)
        if overwrite:
            os.replace(tmp_name, path)
        else:
            os.link(tmp_name, path)  # Atomic publication fails if another writer created the destination.
            os.unlink(tmp_name)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

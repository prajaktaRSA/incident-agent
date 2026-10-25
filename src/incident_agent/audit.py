"""Append-only JSONL audit log: one file per chat session, one line per event."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, logs_dir: Path, session_id: str) -> None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"{session_id}.jsonl"

    def write(self, kind: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

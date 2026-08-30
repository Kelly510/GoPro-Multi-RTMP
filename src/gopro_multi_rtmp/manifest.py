"""Crash-tolerant session manifest."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Manifest:
    """Persist state atomically after every meaningful transition."""

    def __init__(self, path: Path, session_id: str, public_config: dict[str, Any]) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "session_id": session_id,
            "status": "initializing",
            "created_at_utc": utc_now(),
            "configuration": public_config,
            "events": [],
            "recordings": [],
        }
        self.write()

    def event(self, source: str, state: str, **details: Any) -> None:
        record = {"at_utc": utc_now(), "source": source, "state": state}
        record.update(details)
        self.data["events"].append(record)
        self.write()

    def status(self, value: str, **details: Any) -> None:
        self.data["status"] = value
        self.data.update(details)
        self.write()

    def recordings(self, values: list[dict[str, Any]]) -> None:
        self.data["recordings"] = values
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


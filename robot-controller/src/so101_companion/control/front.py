"""Persistence of the Follow Me front reference (owner decision D2, ADR-019).

One JSON file (``config/follow_front.json``, gitignored like the rest pose) holding one entry per
calibration fingerprint ("simulation" for Simulation Only). An entry is only used when BOTH the
calibration fingerprint and the model identity match; otherwise the front falls back to the pan-travel
center and the status says so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from so101_companion.control.overlays import FrontReference

SIM_KEY = "simulation"


class FrontStore(Protocol):
    def load(self, key: str) -> FrontReference | None: ...

    def save(self, key: str, front: FrontReference) -> str: ...


class MemoryFrontStore:
    def __init__(self) -> None:
        self.entries: dict[str, FrontReference] = {}

    def load(self, key: str) -> FrontReference | None:
        return self.entries.get(key)

    def save(self, key: str, front: FrontReference) -> str:
        self.entries[key] = front
        return f"memory:{key}"


class FileFrontStore:
    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "follow_front.json"

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        entries = doc.get("entries") if isinstance(doc, dict) else None
        return entries if isinstance(entries, dict) else {}

    def load(self, key: str) -> FrontReference | None:
        raw = self._read().get(key)
        if not isinstance(raw, dict):
            return None
        try:
            return FrontReference(
                float(raw["bearing_deg"]),  # type: ignore[arg-type]
                raw.get("calibration_fingerprint"),  # type: ignore[arg-type]
                str(raw["model_id"]),
                str(raw.get("set_at", "")),
                str(raw.get("method", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, key: str, front: FrontReference) -> str:
        entries = self._read()
        entries[key] = front.to_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "entries": entries}, indent=2) + "\n")
        os.replace(tmp, self.path)
        return str(self.path)

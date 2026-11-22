#!/usr/bin/env python3
"""Regenerate MANIFEST.json: sha256 + size of every package file Git does not ignore.

Run after changing documentation or code:  python scripts/update_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.json"


def package_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    return sorted(p for p in out if p and p != "MANIFEST.json" and (ROOT / p).is_file())


def main() -> int:
    files = []
    for rel in package_files():
        data = (ROOT / rel).read_bytes()
        files.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {
        "package": "SO-101 Companion — M1 specification package and implementation",
        "created_date": "2026-11-06",
        "updated_date": date.today().isoformat(),
        "purpose": "Approved M1 specs, M2/M3 interview plans, and the M1 implementation (src/tests/scripts).",
        "milestone_1_status": "IMPLEMENTED_OFFLINE_VERIFIED",
        "offline_gate": "PASSED",
        "hardware_gate": "NOT_RUN_ARM_UNAVAILABLE",
        "excluded": "files ignored by .gitignore (.venv, vendor/ upstream model, artifacts, reports, caches)",
        "files": files,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"MANIFEST.json: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

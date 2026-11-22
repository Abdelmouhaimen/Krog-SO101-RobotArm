#!/usr/bin/env python3
"""Fetch the official SO-101 MuJoCo model at the PINNED commit into vendor/SO-ARM100 and verify it.

Only the files listed in src/so101_companion/assets/upstream_model_manifest.json are checked out
(git sparse checkout, blob-filtered clone). Every file is sha256-verified; nothing is modified.
Requires `git` and network access to github.com. Safe to re-run (no-op if already verified).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from so101_companion.simulation import model_assets  # noqa: E402


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def fetch(dest: Path, force: bool = False) -> int:
    manifest = model_assets.load_manifest()
    if dest.exists() and not force:
        problems = model_assets.find_asset_problems(dest, manifest)
        if not problems:
            print(f"OK: {dest} already matches {manifest.repo_url}@{manifest.commit}")
            return 0
        print(
            f"{dest} exists but does not verify ({len(problems)} problems); re-run with --force to replace."
        )
        for p in problems:
            print(f"  {p}")
        return 1

    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--quiet", "--filter=blob:none", "--no-checkout", manifest.repo_url, str(tmp))
    _git("sparse-checkout", "set", "--no-cone", *[f"/{rel}" for rel in manifest.files], cwd=tmp)
    _git("checkout", "--quiet", manifest.commit, cwd=tmp)

    problems = model_assets.find_asset_problems(tmp, manifest)
    if problems:
        print("Fetched files do not match the pinned manifest; leaving vendor/ untouched:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    print(
        f"Fetched and verified {len(manifest.files)} files: {manifest.repo_url}@{manifest.commit} "
        f"({manifest.license_spdx}) -> {dest}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=model_assets.default_vendor_dir())
    parser.add_argument("--force", action="store_true", help="Replace an existing non-verifying copy")
    args = parser.parse_args()
    return fetch(args.dest, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

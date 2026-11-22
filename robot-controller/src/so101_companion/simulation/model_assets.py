"""Location and integrity verification of the pinned, unmodified official SO-101 model files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

VENDOR_DIR_NAME = "SO-ARM100"


class AssetVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelManifest:
    repo_url: str
    commit: str
    commit_date: str
    license_spdx: str
    license_file: str
    scene: str
    files: dict[str, str]  # relative path -> sha256 hex


def load_manifest() -> ModelManifest:
    raw = resources.files("so101_companion.assets").joinpath("upstream_model_manifest.json").read_text()
    data = json.loads(raw)
    return ModelManifest(
        repo_url=data["repo_url"],
        commit=data["commit"],
        commit_date=data["commit_date"],
        license_spdx=data["license_spdx"],
        license_file=data["license_file"],
        scene=data["scene"],
        files=dict(data["files"]),
    )


def project_root() -> Path:
    """Repository root for a source checkout / editable install (src/so101_companion/simulation/..)."""
    return Path(__file__).resolve().parents[3]


def default_vendor_dir() -> Path:
    return project_root() / "vendor" / VENDOR_DIR_NAME


def default_scene_path() -> Path:
    return default_vendor_dir() / load_manifest().scene


def vendor_dir_for_scene(scene_path: Path) -> Path:
    """Inverse of ``vendor_dir / manifest.scene``."""
    depth = len(Path(load_manifest().scene).parts)
    return scene_path.resolve().parents[depth - 1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_asset_problems(vendor_dir: Path, manifest: ModelManifest | None = None) -> list[str]:
    """Return human-readable problems; an empty list means every pinned file is present and unmodified."""
    manifest = manifest or load_manifest()
    problems: list[str] = []
    for rel, expected in sorted(manifest.files.items()):
        path = vendor_dir / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif (actual := sha256_file(path)) != expected:
            problems.append(f"modified: {rel} (sha256 {actual[:12]}… != pinned {expected[:12]}…)")
    return problems


def verify_assets(vendor_dir: Path) -> ModelManifest:
    manifest = load_manifest()
    problems = find_asset_problems(vendor_dir, manifest)
    if problems:
        detail = "\n  ".join(problems)
        raise AssetVerificationError(
            f"Official SO-101 model files at {vendor_dir} do not match the pinned manifest "
            f"({manifest.repo_url}@{manifest.commit[:12]}):\n  {detail}\n"
            "Run: python scripts/fetch_model_assets.py"
        )
    return manifest

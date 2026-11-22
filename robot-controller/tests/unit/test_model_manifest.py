from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from so101_companion.simulation import model_assets


def test_manifest_is_pinned_to_an_exact_commit_with_license() -> None:
    m = model_assets.load_manifest()
    assert re.fullmatch(r"[0-9a-f]{40}", m.commit)
    assert m.license_spdx == "Apache-2.0"
    assert m.license_file in m.files
    assert m.scene == "Simulation/SO101/scene.xml"
    assert "Simulation/SO101/so101_new_calib.xml" in m.files
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in m.files.values())


def test_manifest_covers_every_mesh_referenced_by_the_official_xml(vendor_dir: Path) -> None:
    m = model_assets.load_manifest()
    xml = (vendor_dir / "Simulation/SO101/so101_new_calib.xml").read_text()
    referenced = {f"Simulation/SO101/assets/{f}" for f in re.findall(r'file="([^"]+\.stl)"', xml)}
    assert referenced, "no meshes referenced?"
    assert referenced <= set(m.files)


def test_vendor_copy_verifies(vendor_dir: Path) -> None:
    assert model_assets.find_asset_problems(vendor_dir) == []


def test_modified_or_missing_files_are_detected(vendor_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / "SO-ARM100"
    shutil.copytree(vendor_dir, copy, ignore=shutil.ignore_patterns(".git"))
    scene = copy / "Simulation/SO101/so101_new_calib.xml"
    scene.write_text(scene.read_text().replace('name="gripper"', 'name="gripper2"', 1))
    (copy / "Simulation/SO101/assets/base_so101_v2.stl").unlink()
    problems = model_assets.find_asset_problems(copy)
    assert any(p.startswith("modified: Simulation/SO101/so101_new_calib.xml") for p in problems)
    assert any(p.startswith("missing: Simulation/SO101/assets/base_so101_v2.stl") for p in problems)
    with pytest.raises(model_assets.AssetVerificationError):
        model_assets.verify_assets(copy)


def test_app_refuses_tampered_model(
    vendor_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from so101_companion.app import EXIT_SETUP_ERROR, main

    copy = tmp_path / "SO-ARM100"
    shutil.copytree(vendor_dir, copy, ignore=shutil.ignore_patterns(".git"))
    (copy / "Simulation/SO101/scene.xml").write_text("<mujoco/>")
    assert (
        main(["--model", str(copy / "Simulation/SO101/scene.xml"), "--smoke-seconds", "0.1"])
        == EXIT_SETUP_ERROR
    )
    assert "do not match the pinned manifest" in capsys.readouterr().err


def test_vendor_dir_for_scene_round_trips() -> None:
    v = model_assets.default_vendor_dir()
    assert model_assets.vendor_dir_for_scene(v / model_assets.load_manifest().scene) == v.resolve()

"""Deterministic Skill: skill_build_patch.

Reconstructs the KOF 2002 disc image with character mods applied:
  1. Runs strict preflight validation (blocks building on validation errors).
  2. Patches the character PXX.BIN container using 03_build_mod_iso.py logic.
  3. Verifies container size <= original slot size (LBA safety guarantee).
  4. Stash patched container in workspace/files/ (never modifies original or extracted/).
  5. Rebuilds Mode 2 Form 1 (2352 bytes/sector) disc image with fresh ECMA-130 EDC
     and Reed-Solomon P/Q ECC using 07_disc_editor.py.
  6. Generates matching .cue sheet and verifies sector integrity.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from skills.base import BaseSkill, SkillResult
from skills.validate_character import ValidateCharacterSkill

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
DEFAULT_OUT_BIN = ROOT_DIR / "build" / "kof2002_forge.bin"


def _load_module(name: str, filename: str):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BuildPatchSkill(BaseSkill):
    name = "skill_build_patch"
    display_name = "Build Modded Disc Image (LBA-Safe)"
    description = (
        "Preflights validation, patches the target character PXX.BIN container, "
        "and reconstructs a bootable Mode 2 Form 1 ISO/BIN with recalculated "
        "ECMA-130 EDC and Reed-Solomon P/Q ECC. Original files are never modified."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Name or slug of the character project to build.",
            },
            "output_bin": {
                "type": "string",
                "description": (
                    "Optional destination path for the modded .bin disc image. "
                    "Defaults to 'build/kof2002_forge.bin'."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": "If True, patches PXX.BIN and verifies size without writing the full 446MB disc image.",
                "default": False,
            },
        },
        "required": ["project_name"],
    }

    def execute(
        self,
        project_name: str,
        output_bin: str | None = None,
        dry_run: bool = False,
        workspace_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        errors: list[str] = []
        warnings: list[str] = []

        ws_path = Path(workspace_dir) if workspace_dir else None

        # 1. Preflight Validation
        val_skill = ValidateCharacterSkill()
        val_res = val_skill.execute(project_name=project_name, workspace_dir=ws_path)
        if not val_res.success:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Preflight validation failed for '{project_name}'. Cannot build mod.",
                errors=val_res.errors,
                warnings=val_res.warnings,
                data={"validation_result": val_res.data},
            )

        try:
            from core.forge_project import ForgeProject

            proj = ForgeProject.load(project_name, workspace_dir=ws_path)
        except Exception as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Failed to load project: {exc}",
                errors=[str(exc)],
            )

        # 2. Load Build Engines
        try:
            m03 = _load_module("kof_build", "03_build_mod_iso.py")
            m07 = _load_module("kof_disc", "07_disc_editor.py")
        except Exception as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Failed to load disc building engine: {exc}",
                errors=[str(exc)],
            )

        disc_proj = m07.Project()
        slot = proj.base_slot
        clean_rel = "/" + slot.rel_bin_path.replace("\\", "/").lstrip("/")

        try:
            entry = disc_proj.entry(clean_rel)
        except KeyError as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Slot file {clean_rel} not found in disc manifest: {exc}",
                errors=[str(exc)],
            )

        orig_bin = disc_proj.original_bytes(entry)

        # 3. Collect modified frames from project
        frame_files = sorted(proj.frames_dir.glob("frame_*.png")) if proj.frames_dir.exists() else []
        edited_frames: dict[int, tuple[np.ndarray, dict[str, Any]]] = {}

        for f_path in frame_files:
            try:
                m = m03.FRAME_NAME_RE.match(f_path.name)
                if not m:
                    continue
                fidx = int(m.group(1))
                with Image.open(f_path) as im:
                    arr = np.array(im, dtype=np.uint8)
                    w, h = im.size
                edited_frames[fidx] = (
                    arr,
                    {
                        "w_tiles": w // 16,
                        "h_tiles": h // 16,
                        "pal_slot": 0x10,
                        "flg": 0x01,
                    },
                )
            except Exception as exc:
                errors.append(f"Failed to load frame {f_path.name}: {exc}")

        if errors:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message="Error loading frame files.",
                errors=errors,
            )

        # 4. Patch PXX.BIN container
        if edited_frames:
            try:
                patched_bytes, patch_info = m03.patch_pxx_bin(
                    orig_bin=orig_bin,
                    edited_frames=edited_frames,
                    source_label=f"{proj.name} ({slot.display_name})",
                )
            except Exception as exc:
                return SkillResult(
                    skill_name=self.name,
                    success=False,
                    message=f"Container patching failed: {exc}",
                    errors=[str(exc)],
                )
        else:
            # No custom frames, use original bytes
            patched_bytes = orig_bin
            patch_info = {"mode": "unchanged", "edited_count": 0}

        # 5. Save patched container in workspace/files/
        try:
            save_note = disc_proj.save_bytes(entry, patched_bytes)
        except ValueError as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"LBA-Safe Violation: {exc}",
                errors=[str(exc)],
            )

        out_bin_path = Path(output_bin) if output_bin else DEFAULT_OUT_BIN
        out_bin_path.parent.mkdir(parents=True, exist_ok=True)

        if dry_run:
            return SkillResult(
                skill_name=self.name,
                success=True,
                message=f"Dry-run successful: {slot.rel_bin_path} patched ({patch_info.get('mode')}, {save_note}). Ready for full disc reconstruction.",
                data={
                    "dry_run": True,
                    "target_file": clean_rel,
                    "patch_info": patch_info,
                    "save_note": save_note,
                    "output_bin_target": str(out_bin_path),
                },
            )

        # 6. Rebuild full disc image with EDC/ECC
        try:
            built_path = disc_proj.build(out_bin_path, log=lambda *args: None)
            verified = disc_proj.verify(built_path, log=lambda *args: None)
            if not verified:
                return SkillResult(
                    skill_name=self.name,
                    success=False,
                    message="Disc image built but sector verification failed.",
                    errors=["EDC/ECC or sector mismatch post-build."],
                )

            proj.update_build(output_bin=built_path, verified=True)

            return SkillResult(
                skill_name=self.name,
                success=True,
                message=f"Modded disc image successfully built and verified at {built_path.name}.",
                data={
                    "output_bin": str(built_path),
                    "output_cue": str(built_path.with_suffix(".cue")),
                    "size_bytes": built_path.stat().st_size,
                    "target_file": clean_rel,
                    "patch_info": patch_info,
                    "verified": True,
                },
            )
        except Exception as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Disc reconstruction failed: {exc}",
                errors=[str(exc)],
            )

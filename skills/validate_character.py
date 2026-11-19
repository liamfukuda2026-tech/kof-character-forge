"""Deterministic Skill: skill_validate_character.

Validates a character project against strict KOF 2002 PS2 engine contracts:
  1. Image format: PNG, Indexed 16 colors (mode='P').
  2. Palette constraint: Max index <= 15, index 0 transparency (tRNS=0).
  3. Tile grid: Width and height must be multiples of 16 (16x16 column-major tiles).
  4. Memory constraint (LBA-Safe): Total container size must not exceed the original
     character slot PXX.BIN size on disc (padding with 0x00 is allowed; growth is strictly rejected).
  5. Palettes: 16 RGB values per button (A/B/C/D), index 0 = (255, 0, 255) transparent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from core.forge_project import ForgeProject
from skills.base import BaseSkill, SkillResult

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"


def _load_build_module():
    """Dynamically load scripts/03_build_mod_iso.py for block encoding."""
    script_path = SCRIPTS_DIR / "03_build_mod_iso.py"
    if not script_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("kof_build_val", str(script_path))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kof_build_val"] = mod
    spec.loader.exec_module(mod)
    return mod


class ValidateCharacterSkill(BaseSkill):
    name = "skill_validate_character"
    display_name = "Validate Character Assets & Memory"
    description = (
        "Deterministically validates character project frames, palettes, and memory constraints. "
        "Verifies mode='P', max index <= 15, index 0 transparency, 16x16 tile grid, and ensures "
        "total encoded size does not exceed original character slot memory (LBA-safe)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Name or slug of the project to validate.",
            }
        },
        "required": ["project_name"],
    }

    def execute(
        self,
        project_name: str,
        workspace_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        errors: list[str] = []
        warnings: list[str] = []
        metrics: dict[str, Any] = {}

        try:
            ws_path = Path(workspace_dir) if workspace_dir else None
            proj = ForgeProject.load(project_name, workspace_dir=ws_path)
        except Exception as exc:
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Could not load project '{project_name}': {exc}",
                errors=[str(exc)],
            )

        slot = proj.base_slot
        max_slot_bytes = slot.size_bytes
        metrics["base_slot"] = slot.display_name
        metrics["slot_id_hex"] = slot.char_id_hex
        metrics["max_memory_bytes"] = max_slot_bytes

        # ---------------------------------------------------------------------
        # 1. Validate Palettes (A, B, C, D)
        # ---------------------------------------------------------------------
        palettes = proj.data.get("palettes", {})
        for btn in ("A", "B", "C", "D"):
            if btn not in palettes:
                warnings.append(f"Button palette '{btn}' not configured in character.json; using defaults.")
                continue
            cols = palettes[btn]
            if not isinstance(cols, list) or len(cols) != 16:
                errors.append(f"Button palette '{btn}' must have exactly 16 color entries (got {len(cols) if isinstance(cols, list) else type(cols)}).")
                continue
            # Check RGB bounds
            for c_idx, rgb in enumerate(cols):
                if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
                    errors.append(f"Palette '{btn}' color #{c_idx} is not a 3-element RGB tuple: {rgb}")
                    break
                for val in rgb:
                    if not (0 <= int(val) <= 255):
                        errors.append(f"Palette '{btn}' color #{c_idx} value {val} out of bounds 0..255.")
                        break

        # ---------------------------------------------------------------------
        # 2. Validate Sprite Frames
        # ---------------------------------------------------------------------
        frame_files = sorted(proj.frames_dir.glob("frame_*.png")) if proj.frames_dir.exists() else []
        metrics["total_custom_frames"] = len(frame_files)

        total_encoded_bytes = 0
        builder = _load_build_module()

        for f_path in frame_files:
            rel_name = f_path.name
            try:
                with Image.open(f_path) as im:
                    fmt = im.format
                    mode = im.mode
                    w, h = im.size
                    trns = im.info.get("transparency")

                    # Check format
                    if fmt != "PNG":
                        errors.append(f"{rel_name}: format is '{fmt}', must be 'PNG'.")

                    # Check color mode
                    if mode != "P":
                        errors.append(
                            f"{rel_name}: color mode is '{mode}', must be Indexed ('P', 16 colors)."
                        )

                    # Check transparency index 0
                    if trns != 0:
                        errors.append(
                            f"{rel_name}: transparency is {trns!r}, must be 0 (palette index 0 transparent)."
                        )

                    # Check tile alignment
                    if w % 16 != 0 or h % 16 != 0:
                        errors.append(
                            f"{rel_name}: dimensions {w}x{h} px are not multiples of 16 (tile grid requirement)."
                        )

                    arr = np.array(im, dtype=np.uint8)
                    max_idx = int(arr.max()) if arr.size else 0
                    if max_idx > 15:
                        errors.append(
                            f"{rel_name}: uses palette index {max_idx} > 15 (max 16 colors allowed)."
                        )

                    # Calculate block encoding size
                    if builder and w % 16 == 0 and h % 16 == 0 and mode == "P" and max_idx <= 15:
                        w_tiles = w // 16
                        h_tiles = h // 16
                        try:
                            block_bytes, _ = builder.encode_sprite_block(
                                pixels=arr,
                                w_tiles=w_tiles,
                                h_tiles=h_tiles,
                                pal_slot=0x10,
                                target_flg=0x01,  # Uncompressed standard
                            )
                            total_encoded_bytes += len(block_bytes)
                        except Exception as enc_err:
                            warnings.append(f"{rel_name}: failed to encode block for size check ({enc_err})")
                            total_encoded_bytes += f_path.stat().st_size
                    else:
                        total_encoded_bytes += f_path.stat().st_size

            except Exception as exc:
                errors.append(f"{rel_name}: failed to inspect image: {exc}")

        # ---------------------------------------------------------------------
        # 3. Memory & LBA-Safety Check
        # ---------------------------------------------------------------------
        # Header + offset table for original slot (approx 4 bytes * total_frames)
        estimated_table_bytes = 4096
        estimated_total_bin = total_encoded_bytes + estimated_table_bytes

        metrics["encoded_frames_bytes"] = total_encoded_bytes
        metrics["estimated_bin_size_bytes"] = estimated_total_bin
        headroom = max_slot_bytes - estimated_total_bin
        metrics["headroom_bytes"] = headroom

        if estimated_total_bin > max_slot_bytes:
            overflow = estimated_total_bin - max_slot_bytes
            errors.append(
                f"Memory overflow: estimated size ({estimated_total_bin} bytes) exceeds original slot capacity "
                f"({max_slot_bytes} bytes) by {overflow} bytes. LBA-safe patching strictly requires container <= slot size."
            )
        else:
            if headroom < 10240:
                warnings.append(
                    f"Low memory headroom: only {headroom} bytes remaining in character slot."
                )

        is_valid = len(errors) == 0
        proj.update_validation(is_valid, errors, warnings, metrics)

        msg = (
            f"Character '{proj.name}' is valid and ready for disc injection (Headroom: {headroom} bytes)."
            if is_valid
            else f"Validation failed with {len(errors)} error(s)."
        )

        return SkillResult(
            skill_name=self.name,
            success=is_valid,
            message=msg,
            data={
                "project_name": proj.name,
                "is_valid": is_valid,
                "metrics": metrics,
            },
            errors=errors,
            warnings=warnings,
        )

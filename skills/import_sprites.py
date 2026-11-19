"""Deterministic Skill: skill_import_sprites.

Imports custom sprite art (PNG, JPG, WEBP) into a Character Forge project.
Uses the verified 05_prepare_custom_frames.py logic:
  - Transparent/chroma-key ingestion (magenta #FF00FF or alpha channel)
  - Color quantization to 16-color indexed PNG (mode='P')
  - Strict palette contract: index 0 = (255, 0, 255) transparent (tRNS=0), indices 1..15 opaque
  - Zero-dither Euclidean color remapping
  - Saves validated frame_XXXX.png into project's sprites/frames/ directory
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from core.forge_project import ForgeProject
from skills.base import BaseSkill, SkillResult

FRAME_RE = re.compile(r"^frame_(\d+)(?:_.*)?\.(png|jpe?g|webp|jfif)$", re.IGNORECASE)
TRANSPARENT_KEY = (255, 0, 255)


def _remove_magenta_chroma_key(img: Image.Image, threshold: float = 95.0) -> Image.Image:
    """Convert magenta background pixels (#FF00FF) to fully transparent RGBA."""
    rgba = img.convert("RGBA")
    arr = np.array(rgba, dtype=np.float32)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]

    # Distance to pure magenta (255, 0, 255)
    dist = np.sqrt((r - 255.0) ** 2 + (g - 0.0) ** 2 + (b - 255.0) ** 2)
    # Chroma excess check: red and blue much higher than green
    magenta_excess = (np.minimum(r, b) - g) > 40.0
    is_key = (dist < threshold) | ((dist < threshold + 30.0) & magenta_excess)

    out = np.array(rgba)
    out[is_key, 3] = 0
    return Image.fromarray(out, mode="RGBA")


def _quantize_to_16_colors(
    rgba_img: Image.Image,
    target_palette_16: list[list[int] | tuple[int, int, int]] | None = None,
) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """Quantize an RGBA image to 16 colors: index 0 transparent, indices 1..15 opaque.

    Returns (2D uint8 indices array, 16-entry RGB palette tuple list).
    """
    arr = np.array(rgba_img, dtype=np.uint8)
    h, w = arr.shape[:2]
    fg_mask = arr[:, :, 3] > 0

    if target_palette_16 and len(target_palette_16) >= 16:
        pal_16 = [tuple(int(c) for c in rgb[:3]) for rgb in target_palette_16[:16]]
        pal_16[0] = TRANSPARENT_KEY
    else:
        # Build 15-color palette from foreground pixels
        if not fg_mask.any():
            pal_16 = [TRANSPARENT_KEY] + [(i * 16, i * 16, i * 16) for i in range(1, 16)]
        else:
            opaque_rgb = arr[fg_mask, :3]
            unique_colors = np.unique(opaque_rgb, axis=0)
            if len(unique_colors) <= 15:
                cols = [tuple(int(v) for v in row) for row in unique_colors]
            else:
                # Quantize using median cut
                n = len(opaque_rgb)
                side_w = max(1, int(np.ceil(np.sqrt(n))))
                side_h = max(1, int(np.ceil(n / side_w)))
                packed = np.empty((side_w * side_h, 3), dtype=np.uint8)
                packed[:n] = opaque_rgb
                if side_w * side_h > n:
                    rem = side_w * side_h - n
                    reps = (rem + n - 1) // n
                    packed[n:] = np.tile(opaque_rgb, (reps, 1))[:rem]

                montage = Image.fromarray(packed.reshape(side_h, side_w, 3), mode="RGB")
                q_img = montage.quantize(colors=15, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
                raw_pal = q_img.getpalette() or []
                used = sorted(int(v) for v in np.unique(np.array(q_img, dtype=np.uint8)))
                cols = [
                    (int(raw_pal[idx * 3]), int(raw_pal[idx * 3 + 1]), int(raw_pal[idx * 3 + 2]))
                    for idx in used
                    if idx * 3 + 2 < len(raw_pal)
                ][:15]

            while len(cols) < 15:
                cols.append((0, 0, 0))
            pal_16 = [TRANSPARENT_KEY] + cols[:15]

    # Remap foreground pixels strictly to 1..15 with zero dithering
    indices = np.zeros((h, w), dtype=np.uint8)
    if fg_mask.any():
        pal_15 = np.array(pal_16[1:16], dtype=np.float32)  # shape (15, 3)
        fg_rgb = arr[fg_mask, :3].astype(np.float32)
        dists = np.sum((fg_rgb[:, None, :] - pal_15[None, :, :]) ** 2, axis=2)
        indices[fg_mask] = (np.argmin(dists, axis=1) + 1).astype(np.uint8)

    return indices, pal_16


def _save_indexed_png(indices: np.ndarray, pal_16: list[tuple[int, int, int]], out_path: Path) -> None:
    """Save mode='P' PNG with transparency=0."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(indices, mode="P")
    flat_pal = [ch for rgb in pal_16[:16] for ch in rgb]
    img.putpalette(flat_pal)
    img.save(out_path, format="PNG", transparency=0, optimize=False)


class ImportSpritesSkill(BaseSkill):
    name = "skill_import_sprites"
    display_name = "Import & Quantize Sprites"
    description = (
        "Validates custom character sprite PNGs, quantizes them to 16 colors using "
        "05_prepare_custom_frames.py logic (palette index 0 transparent, indices 1..15 opaque), "
        "and imports them into the character project."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Name or slug of the project to import sprites into.",
            },
            "source_path": {
                "type": "string",
                "description": (
                    "Path to a single image file or a directory containing frame_XXXX.png files."
                ),
            },
            "frame_index": {
                "type": "integer",
                "description": (
                    "Target frame index (e.g. 0). Required if source_path is a single image without frame_XXXX name."
                ),
            },
            "palette_button": {
                "type": "string",
                "description": "Which project button palette to align colors to ('A', 'B', 'C', 'D'). Default is 'A'.",
                "default": "A",
            },
            "quantize": {
                "type": "boolean",
                "description": "Whether to quantize RGB/RGBA image to 16 colors. Default is True.",
                "default": True,
            },
        },
        "required": ["project_name", "source_path"],
    }

    def execute(
        self,
        project_name: str,
        source_path: str,
        frame_index: int | None = None,
        palette_button: str = "A",
        quantize: bool = True,
        workspace_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        errors: list[str] = []
        warnings: list[str] = []

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

        src = Path(source_path)
        if not src.exists():
            return SkillResult(
                skill_name=self.name,
                success=False,
                message=f"Source path does not exist: {src}",
                errors=[f"File or directory not found: {src}"],
            )

        target_pal = proj.get_palette(palette_button)
        imported_frames: list[dict[str, Any]] = []

        # Collect files to import: list of (frame_idx, Path)
        to_import: list[tuple[int, Path]] = []
        if src.is_file():
            m = FRAME_RE.match(src.name)
            if frame_index is not None:
                fidx = frame_index
            elif m:
                fidx = int(m.group(1))
            else:
                return SkillResult(
                    skill_name=self.name,
                    success=False,
                    message=(
                        f"Cannot determine frame index for '{src.name}'. "
                        "Specify 'frame_index' parameter or name file as 'frame_0000.png'."
                    ),
                    errors=["Missing frame_index for un-numbered file"],
                )
            to_import.append((fidx, src))
        elif src.is_dir():
            for child in sorted(src.iterdir()):
                if child.is_file():
                    m = FRAME_RE.match(child.name)
                    if m:
                        to_import.append((int(m.group(1)), child))

            if not to_import:
                return SkillResult(
                    skill_name=self.name,
                    success=False,
                    message=f"No frame_XXXX.(png|jpg|webp) files found in directory {src}.",
                    errors=["No matching frame files found"],
                )

        # Process and import each frame
        for fidx, img_path in to_import:
            try:
                with Image.open(img_path) as raw_im:
                    orig_w, orig_h = raw_im.size
                    orig_mode = raw_im.mode

                    if orig_mode == "P" and not quantize:
                        # Already indexed, check transparency
                        arr = np.array(raw_im, dtype=np.uint8)
                        max_val = int(arr.max()) if arr.size else 0
                        if max_val > 15:
                            warnings.append(
                                f"Frame #{fidx:04d} had index > 15 ({max_val}); re-quantizing to 16 colors."
                            )
                            keyed = _remove_magenta_chroma_key(raw_im)
                            indices, pal_16 = _quantize_to_16_colors(keyed, target_pal)
                        else:
                            indices = arr
                            pal_16 = [tuple(c) for c in target_pal]
                    else:
                        keyed = _remove_magenta_chroma_key(raw_im)
                        indices, pal_16 = _quantize_to_16_colors(keyed, target_pal)

                # Save directly into project
                dest_file = proj.frames_dir / f"frame_{fidx:04d}.png"
                _save_indexed_png(indices, pal_16, dest_file)

                # Update project metadata
                proj.add_frame(fidx, dest_file, quantize_if_needed=False)

                imported_frames.append({
                    "frame_index": fidx,
                    "filename": dest_file.name,
                    "width": indices.shape[1],
                    "height": indices.shape[0],
                    "colors": 16,
                    "size_bytes": dest_file.stat().st_size,
                })
            except Exception as exc:
                errors.append(f"Failed to import frame {img_path.name}: {exc}")

        proj.save()

        success = len(imported_frames) > 0 and len(errors) == 0
        return SkillResult(
            skill_name=self.name,
            success=success,
            message=f"Imported {len(imported_frames)} sprite frame(s) into project '{proj.name}'.",
            data={
                "project_name": proj.name,
                "imported_count": len(imported_frames),
                "imported_frames": imported_frames,
                "palette_button": palette_button,
                "total_project_frames": len(proj.list_frames()),
            },
            errors=errors,
            warnings=warnings,
        )

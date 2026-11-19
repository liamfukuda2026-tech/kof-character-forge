"""KOF 2002 (PS2, SLES-53381) - Character Forge Project Management.

Manages character mod projects located in workspace/forge/<project_name>/.
Handles:
  - Project creation from existing character slots (e.g. Terry Bogard -> Terry Legendary)
  - Asset initialization and metadata management via character.json
  - Frame additions and tracking in sprites/frames/
  - Palette management (A/B/C/D)
  - Export and import of self-contained .kofchar ZIP packages
  - Safe operations: NEVER modifies original ISO or extracted/ files
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from core.characters import CharacterSlot, resolve_character_slot

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE_FORGE = ROOT_DIR / "workspace" / "forge"
EXTRACTED_DIR = ROOT_DIR / "extracted"
FIRST_GRC_PATH = EXTRACTED_DIR / "DATA" / "BASE" / "FIRST.GRC"


def _slugify(text: str) -> str:
    """Generate a clean filesystem slug from a project name."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "_", s)
    return s.strip("_") or "character_mod"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_base_palettes_from_first_grc(base_pal_indices: dict[str, int]) -> dict[str, list[list[int]]]:
    """Extract default A/B/C/D RGB palettes (16 colors each) from FIRST.GRC."""
    palettes: dict[str, list[list[int]]] = {}
    if not FIRST_GRC_PATH.is_file():
        # Fallback if FIRST.GRC is not extracted
        for btn in ("A", "B", "C", "D"):
            palettes[btn] = [[255, 0, 255]] + [[i * 16, i * 16, i * 16] for i in range(1, 16)]
        return palettes

    # Direct extraction using LZ decompressor
    def _lz_decompress(data: bytes | memoryview) -> bytes:
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            i += 1
            if b < 0xC0:
                out.append(b)
            elif b == 0xC0:
                break
            else:
                if i >= n:
                    break
                arg = data[i]
                i += 1
                length = b & 0x3F
                if length == 1:
                    out.append(arg)
                else:
                    if arg <= 0 or arg > len(out):
                        break
                    for _ in range(length):
                        out.append(out[-arg])
        return bytes(out)

    try:
        raw = FIRST_GRC_PATH.read_bytes()
        decompressed = raw[:4] + _lz_decompress(raw[4:])
        offs = struct.unpack_from("<64I", decompressed, 0)
        pal_start = offs[1]
        pal_end = offs[2]
        pal_sub = decompressed[pal_start:pal_end]
        num_palettes = len(pal_sub) // 32

        for btn, pal_idx in base_pal_indices.items():
            if 0 <= pal_idx < num_palettes:
                words = struct.unpack_from("<16H", pal_sub, pal_idx * 32)
                cols: list[list[int]] = [[255, 0, 255]]  # Transparent index 0
                for w in words[1:]:
                    r = ((w >> 10) & 0x1F) * 255 // 31
                    g = ((w >> 5) & 0x1F) * 255 // 31
                    b = (w & 0x1F) * 255 // 31
                    cols.append([r, g, b])
                palettes[btn] = cols
            else:
                palettes[btn] = [[255, 0, 255]] + [[i * 16, i * 16, i * 16] for i in range(1, 16)]
    except Exception:
        for btn in ("A", "B", "C", "D"):
            palettes[btn] = [[255, 0, 255]] + [[i * 16, i * 16, i * 16] for i in range(1, 16)]

    return palettes


class ForgeProject:
    """Represents a Character Forge project on disk."""

    def __init__(self, project_dir: Path, data: dict[str, Any]) -> None:
        self.project_dir = project_dir
        self.data = data

    @property
    def name(self) -> str:
        return self.data.get("name", self.project_dir.name)

    @property
    def slug(self) -> str:
        return self.data.get("slug", _slugify(self.name))

    @property
    def base_slot(self) -> CharacterSlot:
        slot_data = self.data["base_slot"]
        return CharacterSlot(
            char_id=slot_data["char_id"],
            char_id_hex=slot_data["char_id_hex"],
            slug=slot_data["slug"],
            display_name=slot_data["display_name"],
            rel_bin_path=slot_data["rel_bin_path"],
            size_bytes=slot_data["size_bytes"],
            base_palettes=slot_data["base_palettes"],
        )

    @property
    def frames_dir(self) -> Path:
        return self.project_dir / "sprites" / "frames"

    @property
    def audio_dir(self) -> Path:
        return self.project_dir / "audio" / "voices"

    @property
    def palettes_dir(self) -> Path:
        return self.project_dir / "palettes"

    @property
    def base_dir(self) -> Path:
        return self.project_dir / "base"

    @property
    def json_path(self) -> Path:
        return self.project_dir / "character.json"

    # -------------------------------------------------------------------------
    # Factory & Lifecycle Methods
    # -------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        base_slot: int | str,
        author: str = "",
        description: str = "",
        workspace_dir: Path | None = None,
        overwrite: bool = False,
    ) -> ForgeProject:
        """Create a new Character Forge project based on an existing character slot."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name cannot be empty.")

        slot = resolve_character_slot(base_slot)
        slug = _slugify(clean_name)
        root_ws = workspace_dir or DEFAULT_WORKSPACE_FORGE
        project_dir = root_ws / slug

        if project_dir.exists():
            if overwrite:
                shutil.rmtree(project_dir)
            else:
                raise FileExistsError(
                    f"Project '{clean_name}' already exists at {project_dir}. "
                    "Choose another name or set overwrite=True."
                )

        # Create directories
        frames_dir = project_dir / "sprites" / "frames"
        audio_dir = project_dir / "audio" / "voices"
        palettes_dir = project_dir / "palettes"
        base_dir = project_dir / "base"

        for d in (frames_dir, audio_dir, palettes_dir, base_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Extract base palettes from FIRST.GRC
        base_palettes = _read_base_palettes_from_first_grc(slot.base_palettes)

        # Save base metadata for reference
        base_meta = {
            "slot": slot.to_dict(),
            "extracted_source_path": slot.rel_bin_path,
            "max_memory_bytes": slot.size_bytes,
            "created_at": _now_iso(),
        }
        (base_dir / "base_character.json").write_text(
            json.dumps(base_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        data: dict[str, Any] = {
            "schema_version": "1.0",
            "name": clean_name,
            "slug": slug,
            "author": author.strip(),
            "description": description.strip(),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "base_slot": slot.to_dict(),
            "sprites": {
                "frames_dir": "sprites/frames",
                "modified_frames": [],
                "frames_meta": {},
            },
            "palettes": base_palettes,
            "audio": {
                "voices_dir": "audio/voices",
                "custom_clips": [],
            },
            "validation": {
                "last_validated": None,
                "valid": False,
                "errors": [],
                "warnings": [],
                "metrics": {},
            },
            "build": {
                "last_built": None,
                "output_bin": None,
                "verified": False,
            },
        }

        proj = cls(project_dir, data)
        proj.save()
        return proj

    @classmethod
    def load(cls, name_or_path: str | Path, workspace_dir: Path | None = None) -> ForgeProject:
        """Load an existing project by project name, slug, or directory path."""
        if isinstance(name_or_path, Path) and name_or_path.is_dir():
            p_dir = name_or_path
        else:
            cand_path = Path(name_or_path)
            if cand_path.is_dir():
                p_dir = cand_path
            else:
                root_ws = workspace_dir or DEFAULT_WORKSPACE_FORGE
                slug = _slugify(str(name_or_path))
                p_dir = root_ws / slug
                if not p_dir.exists():
                    p_dir = root_ws / str(name_or_path)

        json_path = p_dir / "character.json"
        if not json_path.is_file():
            raise FileNotFoundError(f"character.json not found in {p_dir}")

        raw_data = json.loads(json_path.read_text(encoding="utf-8"))
        return cls(p_dir, raw_data)

    @classmethod
    def list_projects(cls, workspace_dir: Path | None = None) -> list[dict[str, Any]]:
        """List all valid Character Forge projects in the workspace."""
        root_ws = workspace_dir or DEFAULT_WORKSPACE_FORGE
        if not root_ws.exists():
            return []

        projects: list[dict[str, Any]] = []
        for child in sorted(root_ws.iterdir()):
            if child.is_dir() and (child / "character.json").is_file():
                try:
                    p = cls.load(child)
                    projects.append({
                        "name": p.name,
                        "slug": p.slug,
                        "path": str(p.project_dir),
                        "base_slot": p.base_slot.display_name,
                        "char_id_hex": p.base_slot.char_id_hex,
                        "author": p.data.get("author", ""),
                        "modified_frames_count": len(p.data.get("sprites", {}).get("modified_frames", [])),
                        "valid": p.data.get("validation", {}).get("valid", False),
                        "created_at": p.data.get("created_at"),
                        "updated_at": p.data.get("updated_at"),
                    })
                except Exception:
                    pass
        return projects

    def save(self) -> None:
        """Save character.json to disk."""
        self.data["updated_at"] = _now_iso()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def delete(self) -> None:
        """Delete this project and all its files from disk."""
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    # -------------------------------------------------------------------------
    # Frame & Sprite Operations
    # -------------------------------------------------------------------------

    def get_frame_path(self, frame_idx: int) -> Path | None:
        """Get path to a specific frame PNG if it exists in the project."""
        p = self.frames_dir / f"frame_{frame_idx:04d}.png"
        return p if p.is_file() else None

    def list_frames(self) -> list[dict[str, Any]]:
        """List all frames currently stored in the project."""
        frames: list[dict[str, Any]] = []
        meta = self.data.get("sprites", {}).get("frames_meta", {})
        if self.frames_dir.exists():
            for f in sorted(self.frames_dir.glob("frame_*.png")):
                m = re.match(r"frame_(\d+)\.png", f.name)
                if m:
                    idx = int(m.group(1))
                    f_meta = meta.get(str(idx), {})
                    try:
                        with Image.open(f) as im:
                            w, h = im.size
                            mode = im.mode
                    except Exception:
                        w, h = 0, 0
                        mode = "unknown"

                    frames.append({
                        "frame_index": idx,
                        "filename": f.name,
                        "path": str(f),
                        "width": w,
                        "height": h,
                        "mode": mode,
                        "size_bytes": f.stat().st_size,
                        **f_meta,
                    })
        return frames

    def add_frame(
        self,
        frame_idx: int,
        source: Path | bytes | Image.Image,
        quantize_if_needed: bool = True,
    ) -> Path:
        """Add or overwrite a frame in the project."""
        dest_path = self.frames_dir / f"frame_{frame_idx:04d}.png"
        self.frames_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(source, Image.Image):
            img = source
        elif isinstance(source, bytes):
            import io
            img = Image.open(io.BytesIO(source))
        elif isinstance(source, Path):
            img = Image.open(source)
        else:
            raise TypeError(f"Unsupported source type: {type(source)}")

        # Ensure mode P with 16 colors
        if img.mode != "P" or img.info.get("transparency") != 0:
            if quantize_if_needed:
                # Convert RGBA to 16-color indexed PNG with index 0 transparent
                rgba = img.convert("RGBA")
                palette_rgb = self.get_palette("A")
                # Fast direct palette remapping
                import numpy as np
                arr = np.array(rgba, dtype=np.uint8)
                h, w = arr.shape[:2]
                indices = np.zeros((h, w), dtype=np.uint8)
                fg_mask = arr[:, :, 3] > 0
                if fg_mask.any():
                    pal_15 = np.array(palette_rgb[1:16], dtype=np.float32)
                    fg_rgb = arr[fg_mask, :3].astype(np.float32)
                    dists = np.sum((fg_rgb[:, None, :] - pal_15[None, :, :]) ** 2, axis=2)
                    indices[fg_mask] = (np.argmin(dists, axis=1) + 1).astype(np.uint8)

                out_im = Image.fromarray(indices, mode="P")
                flat_pal = [ch for rgb in palette_rgb[:16] for ch in rgb]
                out_im.putpalette(flat_pal)
                out_im.save(dest_path, format="PNG", transparency=0, optimize=False)
            else:
                img.save(dest_path, format="PNG")
        else:
            img.save(dest_path, format="PNG", transparency=0)

        # Update metadata
        modified = set(self.data.get("sprites", {}).get("modified_frames", []))
        modified.add(frame_idx)
        self.data.setdefault("sprites", {})["modified_frames"] = sorted(list(modified))

        with Image.open(dest_path) as saved:
            w, h = saved.size

        frames_meta = self.data.setdefault("sprites", {}).setdefault("frames_meta", {})
        frames_meta[str(frame_idx)] = {
            "width": w,
            "height": h,
            "size_bytes": dest_path.stat().st_size,
            "updated_at": _now_iso(),
        }

        self.save()
        return dest_path

    def remove_frame(self, frame_idx: int) -> bool:
        """Remove a customized frame from the project."""
        p = self.get_frame_path(frame_idx)
        removed = False
        if p and p.exists():
            p.unlink()
            removed = True

        sprites = self.data.get("sprites", {})
        modified = sprites.get("modified_frames", [])
        if frame_idx in modified:
            modified.remove(frame_idx)
            sprites["modified_frames"] = modified
            sprites.get("frames_meta", {}).pop(str(frame_idx), None)
            self.save()

        return removed

    # -------------------------------------------------------------------------
    # Palette Operations
    # -------------------------------------------------------------------------

    def get_palette(self, btn: str = "A") -> list[list[int]]:
        """Get 16-color RGB palette for button A, B, C, or D."""
        btn_key = btn.upper().strip()
        palettes = self.data.get("palettes", {})
        if btn_key in palettes:
            return palettes[btn_key]
        return [[255, 0, 255]] + [[i * 16, i * 16, i * 16] for i in range(1, 16)]

    def set_palette(self, btn: str, colors: list[list[int] | tuple[int, int, int]]) -> None:
        """Set 16-color RGB palette for button A, B, C, or D."""
        btn_key = btn.upper().strip()
        if btn_key not in ("A", "B", "C", "D"):
            raise ValueError(f"Invalid button '{btn}'. Must be A, B, C, or D.")

        clean_cols: list[list[int]] = []
        for idx, col in enumerate(colors[:16]):
            if idx == 0:
                clean_cols.append([255, 0, 255])  # Index 0 is always key / transparent
            else:
                clean_cols.append([int(col[0]), int(col[1]), int(col[2])])

        while len(clean_cols) < 16:
            clean_cols.append([0, 0, 0])

        self.data.setdefault("palettes", {})[btn_key] = clean_cols
        self.save()

    # -------------------------------------------------------------------------
    # State Updates
    # -------------------------------------------------------------------------

    def update_validation(
        self,
        valid: bool,
        errors: list[str],
        warnings: list[str],
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Record validation results into character.json."""
        self.data["validation"] = {
            "last_validated": _now_iso(),
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "metrics": metrics or {},
        }
        self.save()

    def update_build(self, output_bin: Path | str, verified: bool) -> None:
        """Record build results into character.json."""
        self.data["build"] = {
            "last_built": _now_iso(),
            "output_bin": str(output_bin),
            "verified": verified,
        }
        self.save()

    # -------------------------------------------------------------------------
    # Package Export & Import (.kofchar ZIP)
    # -------------------------------------------------------------------------

    def export_package(self, out_path: Path | None = None) -> Path:
        """Export project as a portable .kofchar ZIP package."""
        if out_path is None:
            out_path = self.project_dir.parent / f"{self.slug}.kofchar"
        else:
            out_path = Path(out_path)

        out_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(self.project_dir):
                for f in files:
                    full_p = Path(root) / f
                    rel_p = full_p.relative_to(self.project_dir)
                    zf.write(full_p, arcname=str(rel_p))

        return out_path

    @classmethod
    def import_package(
        cls,
        kofchar_path: Path,
        workspace_dir: Path | None = None,
        overwrite: bool = False,
    ) -> ForgeProject:
        """Import a .kofchar ZIP package into the workspace."""
        kofchar_path = Path(kofchar_path)
        if not kofchar_path.is_file():
            raise FileNotFoundError(f".kofchar package not found: {kofchar_path}")

        if not zipfile.is_zipfile(kofchar_path):
            raise ValueError(f"File {kofchar_path} is not a valid zip archive.")

        with zipfile.ZipFile(kofchar_path, "r") as zf:
            namelist = zf.namelist()
            if "character.json" not in namelist:
                raise ValueError(
                    f"Package {kofchar_path.name} is missing root character.json."
                )

            # Peek character name from character.json in zip
            raw_json = zf.read("character.json").decode("utf-8")
            data = json.loads(raw_json)
            name = data.get("name", kofchar_path.stem)
            slug = data.get("slug", _slugify(name))

            root_ws = workspace_dir or DEFAULT_WORKSPACE_FORGE
            target_dir = root_ws / slug

            if target_dir.exists():
                if overwrite:
                    shutil.rmtree(target_dir)
                else:
                    raise FileExistsError(
                        f"Target project directory already exists: {target_dir}. "
                        "Set overwrite=True to replace."
                    )

            target_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(target_dir)

        return cls.load(target_dir)

    def to_dict(self) -> dict[str, Any]:
        """Return complete project dictionary."""
        return {
            **self.data,
            "project_path": str(self.project_dir),
            "frames_count": len(self.list_frames()),
        }

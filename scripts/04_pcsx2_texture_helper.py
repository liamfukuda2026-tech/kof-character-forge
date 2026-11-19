#!/usr/bin/env python3
"""Manage PCSX2 Qt texture replacement folders, validation, and staging.

Reads the PS2 game serial from scripts/iso_manifest.json (e.g. SLES_533.81 ->
SLES-53381), initializes textures/<SERIAL>/replacements/, validates RGBA and
Indexed PNGs (including power-of-two, integer-scale, palette, and PCSX2 hash
filename checks), and stages replacement textures via texture_map.json or
matching hash filenames.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

SERIAL_REGEX = re.compile(r"([A-Za-z]{4})[_\-]?(\d{3})\.?(\d{2})")
PCSX2_HASH_STEM_RE = re.compile(
    r"^[0-9a-fA-F]{8,16}(?:-[0-9a-fA-F]{1,16}){0,2}(?:-mip\d+)?$"
)
SUPPORTED_SPRITE_MODES = {"RGBA", "P"}
WARN_MODES = {"RGB", "LA", "L"}
MAX_TEXTURE_DIM = 8192

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FileValidationResult:
    """Validation report for a single PNG candidate."""

    path: str
    valid: bool
    width: int = 0
    height: int = 0
    mode: str = ""
    file_size: int = 0
    is_power_of_two: bool = False
    is_pcsx2_hash_name: bool = False
    scale_factor: str | None = None
    palette_colors: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_power_of_two(value: int) -> bool:
    """Return True if value is a positive power of two."""
    return value > 0 and (value & (value - 1)) == 0


def normalize_serial(raw_serial: str) -> str:
    """Convert PS2 disc executable name (SLES_533.81) to PCSX2 serial (SLES-53381)."""
    cleaned = raw_serial.strip()
    cleaned = re.sub(r"^cdrom0:[\\/]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.split(";")[0].strip()

    match = SERIAL_REGEX.search(cleaned)
    if match:
        prefix, part1, part2 = match.groups()
        return f"{prefix.upper()}-{part1}{part2}"

    fallback = cleaned.upper().replace("_", "-").replace(".", "")
    if not fallback:
        raise ValueError(f"Could not parse a valid PS2 serial from {raw_serial!r}")
    return fallback


def resolve_serial(serial_override: str | None, manifest_path: Path) -> str:
    """Resolve the PCSX2 game serial from --serial or scripts/iso_manifest.json."""
    if serial_override:
        return normalize_serial(serial_override)

    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Manifest not found at {manifest_path}. Provide --serial (e.g. SLES-53381)."
        )

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates: list[str] = []
    if isinstance(data.get("ps2_executable"), str):
        candidates.append(data["ps2_executable"])
    sys_cnf = data.get("system_cnf")
    if isinstance(sys_cnf, dict):
        if isinstance(sys_cnf.get("ps2_executable"), str):
            candidates.append(sys_cnf["ps2_executable"])
        if isinstance(sys_cnf.get("boot2_raw"), str):
            candidates.append(sys_cnf["boot2_raw"])
        entries = sys_cnf.get("entries")
        if isinstance(entries, dict) and isinstance(entries.get("BOOT2"), str):
            candidates.append(entries["BOOT2"])

    for candidate in candidates:
        if candidate.strip():
            return normalize_serial(candidate)

    raise ValueError(
        f"No PS2 executable serial found in {manifest_path}. Use --serial to specify one."
    )


def resolve_pcsx2_replacements_dir(pcsx2_dir: Path, serial: str) -> Path:
    """Resolve the PCSX2 replacements directory from a PCSX2 root or textures path."""
    lower_name = pcsx2_dir.name.lower()
    if lower_name == "replacements":
        return pcsx2_dir
    if lower_name == "textures":
        return pcsx2_dir / serial / "replacements"
    if pcsx2_dir.parent.name.lower() == serial.lower() and lower_name == "dumps":
        return pcsx2_dir.parent / "replacements"
    return pcsx2_dir / "textures" / serial / "replacements"


def is_replacements_path(path: Path) -> bool:
    """Return True if path is or resides inside a 'replacements' directory."""
    return any(part.lower() == "replacements" for part in path.parts)


def is_pcsx2_hash_filename(path: Path) -> bool:
    """Return True if the filename stem matches PCSX2 Qt texture hash naming."""
    return bool(PCSX2_HASH_STEM_RE.match(path.stem))


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_palette(img: Image.Image, errors: list[str], warnings: list[str]) -> int | None:
    """Validate indexed PNG palette integrity and return color count."""
    palette = img.getpalette()
    if not palette:
        errors.append("Indexed PNG (mode 'P') has no palette data.")
        return 0
    if len(palette) % 3 != 0:
        errors.append(
            f"Indexed PNG palette byte length ({len(palette)}) is not divisible by 3."
        )
        return len(palette) // 3
    color_count = len(palette) // 3
    if color_count == 0 or color_count > 256:
        errors.append(f"Indexed PNG palette color count out of range: {color_count}.")
    if "transparency" not in img.info:
        warnings.append(
            "Indexed PNG has no tRNS transparency chunk; index 0 may render opaque."
        )
    return color_count


def _check_reference_scale(
    width: int,
    height: int,
    ref_path: Path,
    errors: list[str],
    warnings: list[str],
) -> str | None:
    """Compare replacement dimensions against a reference dump PNG."""
    try:
        with Image.open(ref_path) as ref_img:
            orig_w, orig_h = ref_img.size
    except Exception as exc:
        warnings.append(f"Could not read reference dump {ref_path.name}: {exc}")
        return None

    if orig_w <= 0 or orig_h <= 0:
        return None

    if width * orig_h != height * orig_w:
        errors.append(
            f"Aspect ratio mismatch vs dump {ref_path.name} "
            f"({width}x{height} vs {orig_w}x{orig_h})."
        )
        return None

    if width < orig_w or height < orig_h:
        warnings.append(
            f"Replacement ({width}x{height}) is smaller than dump ({orig_w}x{orig_h})."
        )
        return f"{width / orig_w:.2f}x"

    if width % orig_w == 0 and height % orig_h == 0:
        scale_x = width // orig_w
        scale_y = height // orig_h
        if scale_x == scale_y:
            return f"{scale_x}x"

    warnings.append(
        f"Non-integer scale vs dump {ref_path.name} ({width}x{height} vs {orig_w}x{orig_h})."
    )
    return f"{width / orig_w:.2f}x"


def validate_png_file(
    png_path: Path,
    *,
    require_hash_name: bool = False,
    ref_dir: Path | None = None,
    strict: bool = False,
) -> FileValidationResult:
    """Validate a single PNG file for PCSX2 texture replacement compatibility."""
    errors: list[str] = []
    warnings: list[str] = []
    hash_ok = is_pcsx2_hash_filename(png_path)

    if require_hash_name and not hash_ok:
        errors.append(
            f"Invalid PCSX2 hash filename '{png_path.name}' "
            "(expected <8-16 hex>[ -<pal_hash>-<psm>].png)."
        )

    if not png_path.is_file():
        errors.append("File does not exist.")
        return FileValidationResult(
            path=str(png_path),
            valid=False,
            is_pcsx2_hash_name=hash_ok,
            errors=errors,
        )

    file_size = png_path.stat().st_size
    if file_size == 0:
        errors.append("File is empty (0 bytes).")
        return FileValidationResult(
            path=str(png_path),
            valid=False,
            file_size=0,
            is_pcsx2_hash_name=hash_ok,
            errors=errors,
        )

    try:
        raw_bytes = png_path.read_bytes()
        with Image.open(io.BytesIO(raw_bytes)) as verify_img:
            if verify_img.format != "PNG":
                errors.append(f"Expected PNG image, got format {verify_img.format!r}.")
            verify_img.verify()

        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.load()
            width, height = img.size
            mode = img.mode
            palette_colors: int | None = None

            if width <= 0 or height <= 0:
                errors.append(f"Invalid image dimensions: {width}x{height}.")
            elif width > MAX_TEXTURE_DIM or height > MAX_TEXTURE_DIM:
                errors.append(
                    f"Image dimensions {width}x{height} exceed {MAX_TEXTURE_DIM}px limit."
                )

            if mode == "P":
                palette_colors = _check_palette(img, errors, warnings)
            elif mode in SUPPORTED_SPRITE_MODES:
                pass
            elif mode in WARN_MODES:
                warnings.append(
                    f"Color mode is {mode!r}; RGBA or Indexed ('P') is recommended for sprites."
                )
            else:
                errors.append(
                    f"Unsupported PNG color mode {mode!r}; use RGBA or Indexed ('P')."
                )

            pot_ok = is_power_of_two(width) and is_power_of_two(height)
            if not pot_ok:
                warnings.append(
                    f"Dimensions {width}x{height} are not power-of-two "
                    "(acceptable for sprite sheets, may require POT for raw GS dumps)."
                )

            scale_factor: str | None = None
            if ref_dir is not None and ref_dir.is_dir():
                ref_candidate = ref_dir / png_path.name
                if ref_candidate.is_file():
                    scale_factor = _check_reference_scale(
                        width, height, ref_candidate, errors, warnings
                    )
    except Exception as exc:
        errors.append(f"Failed to decode PNG: {exc}")
        return FileValidationResult(
            path=str(png_path),
            valid=False,
            file_size=file_size,
            is_pcsx2_hash_name=hash_ok,
            errors=errors,
            warnings=warnings,
        )

    if strict and warnings:
        errors.extend([f"[strict] {w}" for w in warnings])
        warnings = []

    return FileValidationResult(
        path=str(png_path),
        valid=len(errors) == 0,
        width=width,
        height=height,
        mode=mode,
        file_size=file_size,
        is_power_of_two=pot_ok,
        is_pcsx2_hash_name=hash_ok,
        scale_factor=scale_factor,
        palette_colors=palette_colors,
        errors=errors,
        warnings=warnings,
    )


def build_template_map(serial: str) -> dict[str, Any]:
    """Return the starter texture_map.json payload."""
    return {
        "serial": serial,
        "source_dir": "custom_character",
        "notes": (
            "Map custom PNGs (or cropped regions from a sprite sheet) to PCSX2 "
            "dump hash names. Set 'enabled': true and fill in 'target_hash' "
            "from textures/<SERIAL>/dumps/<hash>.png."
        ),
        "mappings": [
            {
                "comment": "Example entry: copy/crop a custom sprite into a dumped PCSX2 hash.",
                "enabled": False,
                "source": "rugal_custom.png",
                "target_hash": "0000000000000000-0000000000000000-00000014",
                "crop": None,
                "resize": None,
                "convert_to_rgba": True,
            }
        ],
    }


def build_instructions_text(serial: str) -> str:
    """Return PCSX2 Qt v2.8.2 Dump -> Replace workflow instructions."""
    return f"""# PCSX2 Qt Texture Replacement Guide ({serial})

This folder manages custom HD/sprite texture replacements for **The King of Fighters 2002 (PS2)**
(`SLES_533.81` -> PCSX2 serial `{serial}`), compatible with **PCSX2 Qt v2.8.2** (`pcsx2-qt.exe`).

## Directory Layout (PCSX2 Qt Convention)

PCSX2 Qt looks for texture packs under its `textures/<SERIAL>/` directory:
- `textures/{serial}/dumps/` — Raw textures dumped by PCSX2 while playing (`<texhash>-<palhash>-<psm>.png` or `<hash>.png`).
- `textures/{serial}/replacements/` — Your edited/custom PNG replacements keeping the exact `[hash].png` filename.
- `textures/{serial}/texture_map.json` — Optional mapping from `custom_character/` filenames or sheet crops to `[hash].png`.

## Dump -> Replace Workflow

1. **Enable Texture Dumping in PCSX2 Qt v2.8.2**:
   - Open `Settings -> Graphics -> Texture Replacement`.
   - Enable **Dump Textures** (keep **Load Textures** off while dumping).
   - Start KOF 2002 (`SLES-53381`) and play the character/stage (e.g., Omega Rugal) so PCSX2 dumps textures to `<PCSX2_USER_DIR>/textures/{serial}/dumps/`.
2. **Identify & Edit Target Textures**:
   - Copy or inspect the dumped `[hash].png` files.
   - Keep the exact `[hash]` filename (e.g. `a1b2c3d4e5f60708-1122334455667788-00000014.png` or `<hash>.png`).
   - Save replacements as **RGBA** or **Indexed (`P`)** PNGs with power-of-two dimensions or exact integer upscaling (1x, 2x, 4x) of the dumped texture.
3. **Validate & Stage**:
   - Validate files:
     `python scripts/04_pcsx2_texture_helper.py validate --source custom_character`
   - Stage mapped or hash-named PNGs into `textures/{serial}/replacements/` (and optionally sync to PCSX2):
     `python scripts/04_pcsx2_texture_helper.py stage --source custom_character --pcsx2-dir "C:\\Path\\To\\PCSX2"`
   - Validate the staged replacements folder:
     `python scripts/04_pcsx2_texture_helper.py validate`
4. **Enable Texture Loading in PCSX2 Qt v2.8.2**:
   - Turn **OFF** `Dump Textures`.
   - Turn **ON** `Load Textures` (and optionally `Async Texture Loading` / `Precache Textures`).
   - Boot the game or press the hotkey to reload textures.
"""


def cmd_init(args: argparse.Namespace, base_dir: Path, serial: str) -> int:
    """Initialize textures/<SERIAL>/ structure, template map, and instructions."""
    tex_root = base_dir / "textures" / serial
    repl_dir = tex_root / "replacements"
    dumps_dir = tex_root / "dumps"
    map_path = tex_root / "texture_map.json"
    guide_path = tex_root / "README_PCSX2_TEXTURES.md"

    repl_dir.mkdir(parents=True, exist_ok=True)
    dumps_dir.mkdir(parents=True, exist_ok=True)

    if not map_path.exists() or args.force:
        map_path.write_text(
            json.dumps(build_template_map(serial), indent=2) + "\n", encoding="utf-8"
        )
        map_status = "created"
    else:
        map_status = "kept existing"

    if not guide_path.exists() or args.force:
        guide_path.write_text(build_instructions_text(serial), encoding="utf-8")
        guide_status = "created"
    else:
        guide_status = "kept existing"

    print(f"[init] Serial: {serial}")
    print(f"[init] Replacements dir: {repl_dir}")
    print(f"[init] Dumps dir:        {dumps_dir}")
    print(f"[init] Mapping template: {map_path} ({map_status})")
    print(f"[init] Instructions:     {guide_path} ({guide_status})")

    if args.pcsx2_dir:
        pcsx2_repl = resolve_pcsx2_replacements_dir(Path(args.pcsx2_dir), serial)
        pcsx2_dumps = pcsx2_repl.parent / "dumps"
        pcsx2_repl.mkdir(parents=True, exist_ok=True)
        pcsx2_dumps.mkdir(parents=True, exist_ok=True)
        print(f"[init] PCSX2 target dir: {pcsx2_repl}")

    return 0


def _collect_pngs(source_path: Path) -> list[Path]:
    """Collect PNG files from a file or directory recursively."""
    if source_path.is_file():
        return [source_path]
    if not source_path.is_dir():
        return []
    return sorted(
        p for p in source_path.rglob("*") if p.is_file() and p.suffix.lower() == ".png"
    )


def cmd_validate(args: argparse.Namespace, base_dir: Path, serial: str) -> int:
    """Validate PNG textures in replacements/ or custom_character/."""
    default_repl = base_dir / "textures" / serial / "replacements"
    source_path = Path(args.source) if args.source else default_repl
    if not source_path.is_absolute() and not source_path.exists():
        candidate = base_dir / source_path
        if candidate.exists():
            source_path = candidate

    if not source_path.exists():
        print(f"[validate] Error: source path does not exist: {source_path}", file=sys.stderr)
        return 1

    ref_dir: Path | None = None
    if args.ref_dir:
        ref_dir = Path(args.ref_dir)
    else:
        default_dumps = base_dir / "textures" / serial / "dumps"
        if default_dumps.is_dir():
            ref_dir = default_dumps

    require_hash = args.require_hash
    if require_hash is None:
        require_hash = is_replacements_path(source_path)

    pngs = _collect_pngs(source_path)
    results = [
        validate_png_file(
            png,
            require_hash_name=require_hash,
            ref_dir=ref_dir,
            strict=args.strict,
        )
        for png in pngs
    ]

    passed = sum(1 for r in results if r.valid)
    failed = len(results) - passed
    warnings_total = sum(len(r.warnings) for r in results)

    if args.json:
        payload = {
            "serial": serial,
            "source": str(source_path),
            "require_hash_name": require_hash,
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "warnings": warnings_total,
            "files": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"[validate] Serial={serial} Source={source_path} "
            f"RequireHash={require_hash} Files={len(results)}"
        )
        if not results:
            print("[validate] No PNG files found.")
        for res in results:
            status = "PASS" if res.valid else "FAIL"
            pot_tag = "POT" if res.is_power_of_two else "non-POT"
            scale_tag = f" scale={res.scale_factor}" if res.scale_factor else ""
            pal_tag = f" pal={res.palette_colors}" if res.palette_colors is not None else ""
            print(
                f"  [{status}] {Path(res.path).name} "
                f"({res.width}x{res.height}, {res.mode or '?'}, {pot_tag}{scale_tag}{pal_tag})"
            )
            for err in res.errors:
                print(f"      ERROR: {err}")
            for warn in res.warnings:
                print(f"      WARN:  {warn}")
        print(
            f"[validate] Summary: {passed} passed, {failed} failed, {warnings_total} warning(s)."
        )

    if args.fail_on_empty and len(results) == 0:
        return 1
    return 0 if failed == 0 else 1


def _resolve_mapping_source(raw_source: str, source_dir: Path, base_dir: Path) -> Path:
    """Resolve a mapping entry's source file path."""
    p = Path(raw_source)
    if p.is_absolute():
        return p
    under_source = source_dir / p
    if under_source.exists():
        return under_source
    under_base = base_dir / p
    if under_base.exists():
        return under_base
    return under_source


def _process_mapped_image(
    src_file: Path,
    entry: dict[str, Any],
    dest_file: Path,
    *,
    ref_dir: Path | None,
    strict: bool,
    dry_run: bool,
) -> tuple[str, FileValidationResult | None]:
    """Validate, optionally crop/resize/convert, and write a mapped PNG."""
    pre_check = validate_png_file(src_file, require_hash_name=False, strict=False)
    if not pre_check.valid:
        return "error", pre_check

    crop = entry.get("crop")
    resize = entry.get("resize")
    convert_to_rgba = bool(entry.get("convert_to_rgba", False))
    resample_name = str(entry.get("resample", "nearest")).lower()
    resample = (
        Image.Resampling.LANCZOS
        if resample_name == "lanczos"
        else Image.Resampling.NEAREST
    )

    needs_transform = bool(crop or resize or convert_to_rgba)
    if not needs_transform:
        # Validate target name & ref scale directly on source bytes
        with Image.open(src_file) as img:
            out_bytes = src_file.read_bytes()
    else:
        with Image.open(src_file) as img:
            work = img.copy()
            if crop is not None:
                if not (isinstance(crop, list) and len(crop) == 4):
                    raise ValueError(f"Invalid crop box {crop!r}; expected [x, y, w, h].")
                x, y, cw, ch = (int(v) for v in crop)
                if cw <= 0 or ch <= 0:
                    raise ValueError(f"Crop width/height must be positive: {crop!r}")
                work = work.crop((x, y, x + cw, y + ch))
            if resize is not None:
                if not (isinstance(resize, list) and len(resize) == 2):
                    raise ValueError(f"Invalid resize {resize!r}; expected [w, h].")
                rw, rh = (int(v) for v in resize)
                if rw <= 0 or rh <= 0:
                    raise ValueError(f"Resize dimensions must be positive: {resize!r}")
                work = work.resize((rw, rh), resample=resample)
            if convert_to_rgba and work.mode != "RGBA":
                work = work.convert("RGBA")
            buf = io.BytesIO()
            work.save(buf, format="PNG")
            out_bytes = buf.getvalue()

    # Validate output image in memory or temp buffer before committing
    with Image.open(io.BytesIO(out_bytes)) as out_img:
        out_img.load()
        width, height = out_img.size
        mode = out_img.mode
        errors: list[str] = []
        warnings: list[str] = []
        if not is_pcsx2_hash_filename(dest_file):
            errors.append(f"Invalid target_hash filename: {dest_file.name}")
        if mode == "P":
            _check_palette(out_img, errors, warnings)
        elif mode not in SUPPORTED_SPRITE_MODES:
            if mode in WARN_MODES:
                warnings.append(f"Mode {mode!r}; RGBA or Indexed ('P') recommended.")
            else:
                errors.append(f"Unsupported output mode {mode!r}.")
        pot_ok = is_power_of_two(width) and is_power_of_two(height)
        if not pot_ok:
            warnings.append(f"Output {width}x{height} is not power-of-two.")
        scale_factor = None
        if ref_dir is not None and ref_dir.is_dir():
            ref_candidate = ref_dir / dest_file.name
            if ref_candidate.is_file():
                scale_factor = _check_reference_scale(
                    width, height, ref_candidate, errors, warnings
                )
        if strict and warnings:
            errors.extend([f"[strict] {w}" for w in warnings])
            warnings = []

        post_res = FileValidationResult(
            path=str(dest_file),
            valid=len(errors) == 0,
            width=width,
            height=height,
            mode=mode,
            file_size=len(out_bytes),
            is_power_of_two=pot_ok,
            is_pcsx2_hash_name=is_pcsx2_hash_filename(dest_file),
            scale_factor=scale_factor,
            errors=errors,
            warnings=warnings,
        )
        if not post_res.valid:
            return "error", post_res

    new_hash = hashlib.sha256(out_bytes).hexdigest()
    if dest_file.is_file() and sha256_file(dest_file) == new_hash:
        return "unchanged", post_res

    if not dry_run:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(out_bytes)
    return "staged", post_res


def _sync_to_pcsx2(dest_dir: Path, pcsx2_repl: Path, dry_run: bool) -> tuple[int, int]:
    """Sync valid PNGs from dest_dir to PCSX2 replacements directory."""
    copied = 0
    unchanged = 0
    if not dry_run:
        pcsx2_repl.mkdir(parents=True, exist_ok=True)

    for src in _collect_pngs(dest_dir):
        rel = src.relative_to(dest_dir)
        target = pcsx2_repl / rel
        if target.is_file() and sha256_file(src) == sha256_file(target):
            unchanged += 1
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        copied += 1
    return copied, unchanged


def cmd_stage(args: argparse.Namespace, base_dir: Path, serial: str) -> int:
    """Stage/pack validated replacement PNGs into textures/<SERIAL>/replacements/."""
    tex_root = base_dir / "textures" / serial
    source_dir = Path(args.source) if args.source else (base_dir / "custom_character")
    if not source_dir.is_absolute() and not source_dir.exists():
        source_dir = base_dir / source_dir

    dest_dir = Path(args.dest) if args.dest else (tex_root / "replacements")
    if not dest_dir.is_absolute():
        dest_dir = base_dir / dest_dir

    map_path = Path(args.map) if args.map else (tex_root / "texture_map.json")
    if not map_path.is_absolute() and not map_path.exists():
        map_path = base_dir / map_path

    dumps_dir = Path(args.dumps_dir) if args.dumps_dir else (tex_root / "dumps")
    ref_dir = dumps_dir if dumps_dir.is_dir() else None

    if not args.dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    staged_count = 0
    unchanged_count = 0
    error_count = 0
    mapped_sources: set[Path] = set()

    # 1. Process explicit mappings in texture_map.json
    if map_path.is_file():
        map_data = json.loads(map_path.read_text(encoding="utf-8"))
        entries = map_data.get("mappings", [])
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if not entry.get("enabled", True):
                continue
            raw_src = entry.get("source")
            raw_hash = entry.get("target_hash")
            if not raw_src or not raw_hash:
                print(
                    f"[stage] ERROR: mapping[{idx}] missing 'source' or 'target_hash'.",
                    file=sys.stderr,
                )
                error_count += 1
                continue

            target_stem = Path(str(raw_hash)).stem
            if not PCSX2_HASH_STEM_RE.match(target_stem):
                print(
                    f"[stage] ERROR: mapping[{idx}] invalid target_hash {raw_hash!r}.",
                    file=sys.stderr,
                )
                error_count += 1
                continue

            src_file = _resolve_mapping_source(str(raw_src), source_dir, base_dir)
            mapped_sources.add(src_file.resolve())
            dest_file = dest_dir / f"{target_stem}.png"

            try:
                status, res = _process_mapped_image(
                    src_file,
                    entry,
                    dest_file,
                    ref_dir=ref_dir,
                    strict=args.strict,
                    dry_run=args.dry_run,
                )
            except Exception as exc:
                print(
                    f"[stage] ERROR: mapping[{idx}] {src_file.name} -> {dest_file.name}: {exc}",
                    file=sys.stderr,
                )
                error_count += 1
                continue

            if status == "error":
                error_count += 1
                errs = "; ".join(res.errors) if res else "unknown error"
                print(
                    f"[stage] FAIL {src_file} -> {dest_file.name}: {errs}",
                    file=sys.stderr,
                )
            elif status == "unchanged":
                unchanged_count += 1
                print(f"[stage] UNCHANGED {dest_file.name} (from {src_file.name})")
            else:
                staged_count += 1
                tag = "DRY-RUN" if args.dry_run else "STAGED"
                dims = f"{res.width}x{res.height}" if res else ""
                print(f"[stage] {tag} {src_file.name} -> {dest_file.name} ({dims})")

    # 2. Process direct hash-named PNGs from source_dir
    unhashed_skipped: list[Path] = []
    if source_dir.is_dir():
        for png in _collect_pngs(source_dir):
            if png.resolve() in mapped_sources:
                continue
            if not is_pcsx2_hash_filename(png):
                unhashed_skipped.append(png)
                continue

            res = validate_png_file(
                png, require_hash_name=True, ref_dir=ref_dir, strict=args.strict
            )
            if not res.valid:
                error_count += 1
                print(
                    f"[stage] FAIL {png.name}: {'; '.join(res.errors)}",
                    file=sys.stderr,
                )
                continue

            rel = png.relative_to(source_dir)
            dest_file = dest_dir / rel
            if dest_file.is_file() and sha256_file(png) == sha256_file(dest_file):
                unchanged_count += 1
                print(f"[stage] UNCHANGED {rel}")
                continue

            if not args.dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(png, dest_file)
            staged_count += 1
            tag = "DRY-RUN" if args.dry_run else "STAGED"
            print(f"[stage] {tag} {rel} ({res.width}x{res.height}, {res.mode})")
    elif not map_path.is_file():
        print(
            f"[stage] Warning: neither source dir ({source_dir}) nor map ({map_path}) exists.",
            file=sys.stderr,
        )

    if unhashed_skipped:
        print(
            f"[stage] Note: skipped {len(unhashed_skipped)} unhashed PNG(s) in {source_dir} "
            "(map them in texture_map.json to stage)."
        )

    if args.pcsx2_dir:
        pcsx2_repl = resolve_pcsx2_replacements_dir(Path(args.pcsx2_dir), serial)
        synced, sync_same = _sync_to_pcsx2(dest_dir, pcsx2_repl, args.dry_run)
        tag = "would sync" if args.dry_run else "synced"
        print(
            f"[stage] PCSX2 sync -> {pcsx2_repl}: {synced} {tag}, {sync_same} unchanged."
        )

    print(
        f"[stage] Summary: {staged_count} staged, {unchanged_count} unchanged, "
        f"{error_count} error(s)."
    )
    return 0 if error_count == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "PCSX2 Qt texture replacement helper for KOF 2002 (init, validate, stage/pack)."
        )
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_REPO_ROOT / "scripts" / "iso_manifest.json"),
        help="Path to scripts/iso_manifest.json (used to read PS2 serial).",
    )
    parser.add_argument(
        "--serial",
        default=None,
        help="Override PCSX2 serial (e.g. SLES-53381 or SLES_533.81).",
    )
    parser.add_argument(
        "--base-dir",
        default=str(DEFAULT_REPO_ROOT),
        help="Project root directory containing textures/ and custom_character/.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser(
        "init",
        help="Create textures/<SERIAL>/replacements/ and Dump->Replace templates.",
    )
    p_init.add_argument(
        "--pcsx2-dir",
        default=None,
        help="Optional PCSX2 root or textures/ directory to initialize as well.",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing texture_map.json and guide if present.",
    )

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Validate PNGs (RGBA/Indexed, POT/integer scale, palette, PCSX2 hash names).",
    )
    p_val.add_argument(
        "--source",
        default=None,
        help="PNG file or directory to scan (default: textures/<SERIAL>/replacements/).",
    )
    p_val.add_argument(
        "--ref-dir",
        default=None,
        help="Optional reference dumps dir (default: textures/<SERIAL>/dumps/ if present).",
    )
    p_val.add_argument(
        "--require-hash",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require valid PCSX2 hex hash filenames (auto-enabled for replacements/).",
    )
    p_val.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (e.g. non-POT, missing tRNS, RGB instead of RGBA) as errors.",
    )
    p_val.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="Exit non-zero if no PNG files are found.",
    )
    p_val.add_argument(
        "--json",
        action="store_true",
        help="Print validation report as JSON.",
    )

    # stage / pack
    for cmd_name in ("stage", "pack"):
        p_stage = subparsers.add_parser(
            cmd_name,
            help="Validate and stage/pack replacement PNGs into textures/<SERIAL>/replacements/.",
        )
        p_stage.add_argument(
            "--source",
            default=None,
            help="Source folder with edited/custom PNGs (default: custom_character/).",
        )
        p_stage.add_argument(
            "--map",
            default=None,
            help="Path to texture_map.json (default: textures/<SERIAL>/texture_map.json).",
        )
        p_stage.add_argument(
            "--dest",
            default=None,
            help="Destination replacements folder (default: textures/<SERIAL>/replacements/).",
        )
        p_stage.add_argument(
            "--dumps-dir",
            default=None,
            help="Reference dumps folder for scale sanity checks.",
        )
        p_stage.add_argument(
            "--pcsx2-dir",
            default=None,
            help="Optional PCSX2 root or textures/ folder to sync replacements into.",
        )
        p_stage.add_argument(
            "--strict",
            action="store_true",
            help="Treat validation warnings as errors during staging.",
        )
        p_stage.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview staging and sync actions without writing files.",
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (base_dir / manifest_path).resolve()

    try:
        serial = resolve_serial(args.serial, manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.command == "init":
        return cmd_init(args, base_dir, serial)
    if args.command == "validate":
        return cmd_validate(args, base_dir, serial)
    if args.command in ("stage", "pack"):
        return cmd_stage(args, base_dir, serial)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
KOF 2002 (PS2) - Custom Character Frame Preparation Tool
========================================================
Prepares AI-generated or custom character art (RGB/RGBA PNG, JPG, WEBP, JFIF,
often on a solid #FF00FF magenta background or multi-pose sprite sheet) into
KOF 2002-compatible 16-color indexed PNGs ready for scripts/03_build_mod_iso.py.

Core pipeline:
1. Input ingestion:
   - Folder of per-frame images named frame_XXXX.(png|jpg|jpeg|webp|jfif)
   - JSON map {frame_index: "path"} (via --input map.json or --map map.json)
   - Sprite sheet + crop boxes (--sheet img --boxes boxes.json [--map map.json])
   - Optional --auto-slice (--sheet img --auto-slice) to detect poses via
     8-connected components, export numbered crops + boxes.json + preview
2. Robust magenta chroma-key removal (Euclidean distance + chroma-excess halo
   removal + boundary despill + small-speck cleanup) and tight content crop.
3. Frame fitting against original Rugal frames (sprites_dump/offsets_map.json +
   sprites_dump/rugal_P27/frames/frame_XXXX.png): scales with nearest-neighbour
   preserving aspect ratio to match the original opaque bbox height, anchors
   bottom-center (feet) to the original bbox bottom-center, and clips to canvas
   with a warning if exceeded.
4. Shared 15-color palette generation across ALL input frames (or --palette-from),
   index 0 = transparent (PLTE[0]=(255,0,255), tRNS=0), and zero-dither remapping
   of opaque pixels strictly to indices 1..15.
5. Writes validated frame_XXXX.png files, palette swatch PNG, palette.json,
   and a side-by-side comparison contact sheet.

Strictly read-only with respect to original disc images, extracted/, and
scripts 01-04.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

FRAME_FILE_RE = re.compile(r"^frame_(\d{1,5})(?:_.*)?\.(png|jpe?g|webp|jfif)$", re.IGNORECASE)
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".jfif"}
QUANTIZE_METHODS = {
    "mediancut": Image.Quantize.MEDIANCUT,
    "fastoctree": Image.Quantize.FASTOCTREE,
    "maxcoverage": Image.Quantize.MAXCOVERAGE,
}


# ---------------------------------------------------------------------------
# Fast Scanline Connected Components (Pure NumPy + Python)
# ---------------------------------------------------------------------------

def _find_connected_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int, np.ndarray]]:
    """Find 8-connected components in a 2D boolean mask.

    Returns list of (x0, y0, x1, y1, area, run_ids) and uses fast scanline
    run-length union-find so megapixel sheets finish in tens of milliseconds.
    """
    h, w = mask.shape
    runs_y: list[int] = []
    runs_x0: list[int] = []
    runs_x1: list[int] = []
    row_ranges: list[tuple[int, int]] = []

    for y in range(h):
        row = mask[y]
        start_idx = len(runs_y)
        if row.any():
            padded = np.empty(w + 2, dtype=np.uint8)
            padded[0] = 0
            padded[1:-1] = row.view(np.uint8)
            padded[-1] = 0
            diffs = np.diff(padded.astype(np.int8))
            starts = np.flatnonzero(diffs == 1)
            ends = np.flatnonzero(diffs == -1)
            for s, e in zip(starts.tolist(), ends.tolist()):
                runs_y.append(y)
                runs_x0.append(s)
                runs_x1.append(e)
        row_ranges.append((start_idx, len(runs_y)))

    n_runs = len(runs_y)
    if n_runs == 0:
        return []

    parent = list(range(n_runs))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for y in range(1, h):
        p_start, p_end = row_ranges[y - 1]
        c_start, c_end = row_ranges[y]
        if p_start == p_end or c_start == c_end:
            continue
        pi = p_start
        ci = c_start
        while pi < p_end and ci < c_end:
            px0, px1 = runs_x0[pi], runs_x1[pi]
            cx0, cx1 = runs_x0[ci], runs_x1[ci]
            # 8-connected overlap: [cx0, cx1) touches [px0-1, px1+1)
            if cx0 <= px1 and px0 <= cx1:
                union(pi, ci)
            if px1 < cx1:
                pi += 1
            else:
                ci += 1

    groups: dict[int, list[int]] = {}
    for idx in range(n_runs):
        root = find(idx)
        groups.setdefault(root, []).append(idx)

    components: list[tuple[int, int, int, int, int, np.ndarray]] = []
    for run_list in groups.values():
        x0 = min(runs_x0[i] for i in run_list)
        y0 = min(runs_y[i] for i in run_list)
        x1 = max(runs_x1[i] for i in run_list)
        y1 = max(runs_y[i] for i in run_list) + 1
        area = sum(runs_x1[i] - runs_x0[i] for i in run_list)
        components.append((x0, y0, x1, y1, area, np.array(run_list, dtype=np.int32)))

    return components


def _remove_small_islands(mask: np.ndarray, min_area: int) -> np.ndarray:
    """Drop isolated foreground components smaller than min_area pixels."""
    if min_area <= 1 or not mask.any():
        return mask

    h, w = mask.shape
    runs_y: list[int] = []
    runs_x0: list[int] = []
    runs_x1: list[int] = []
    row_ranges: list[tuple[int, int]] = []

    for y in range(h):
        row = mask[y]
        start_idx = len(runs_y)
        if row.any():
            padded = np.empty(w + 2, dtype=np.uint8)
            padded[0] = 0
            padded[1:-1] = row.view(np.uint8)
            padded[-1] = 0
            diffs = np.diff(padded.astype(np.int8))
            starts = np.flatnonzero(diffs == 1)
            ends = np.flatnonzero(diffs == -1)
            for s, e in zip(starts.tolist(), ends.tolist()):
                runs_y.append(y)
                runs_x0.append(s)
                runs_x1.append(e)
        row_ranges.append((start_idx, len(runs_y)))

    n_runs = len(runs_y)
    if n_runs == 0:
        return mask

    parent = list(range(n_runs))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for y in range(1, h):
        p_start, p_end = row_ranges[y - 1]
        c_start, c_end = row_ranges[y]
        if p_start == p_end or c_start == c_end:
            continue
        pi, ci = p_start, c_start
        while pi < p_end and ci < c_end:
            px0, px1 = runs_x0[pi], runs_x1[pi]
            cx0, cx1 = runs_x0[ci], runs_x1[ci]
            if cx0 <= px1 and px0 <= cx1:
                union(pi, ci)
            if px1 < cx1:
                pi += 1
            else:
                ci += 1

    area_by_root: dict[int, int] = {}
    roots = [find(i) for i in range(n_runs)]
    for i, r in enumerate(roots):
        area_by_root[r] = area_by_root.get(r, 0) + (runs_x1[i] - runs_x0[i])

    out = mask.copy()
    for i, r in enumerate(roots):
        if area_by_root[r] < min_area:
            out[runs_y[i], runs_x0[i]:runs_x1[i]] = False
    return out


# ---------------------------------------------------------------------------
# Keying, Despill, and Cropping
# ---------------------------------------------------------------------------

def parse_key_color(spec: str, img_rgb: np.ndarray | None = None) -> tuple[int, int, int]:
    """Parse --key-color 'R,G,B' or 'auto' (corner median)."""
    if spec.strip().lower() == "auto":
        if img_rgb is None:
            return (255, 0, 255)
        h, w = img_rgb.shape[:2]
        corners = np.array([
            img_rgb[0, 0],
            img_rgb[0, max(0, w - 1)],
            img_rgb[max(0, h - 1), 0],
            img_rgb[max(0, h - 1), max(0, w - 1)],
        ], dtype=np.int32)
        med = np.median(corners, axis=0).astype(int)
        return (int(med[0]), int(med[1]), int(med[2]))

    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 3:
        raise ValueError(f"Invalid --key-color '{spec}': expected 'R,G,B' or 'auto'")
    rgb = tuple(int(p) for p in parts)
    if any(c < 0 or c > 255 for c in rgb):
        raise ValueError(f"Invalid --key-color '{spec}': RGB channels must be 0..255")
    return rgb  # type: ignore[return-value]


def _dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    """Fast 2D binary dilation with square structuring element of radius."""
    if radius <= 0:
        return mask
    h, w = mask.shape
    padded = np.pad(mask, radius, mode="constant", constant_values=False)
    out = np.zeros_like(mask, dtype=bool)
    for dy in range(-radius, radius + 1):
        y0 = radius + dy
        for dx in range(-radius, radius + 1):
            x0 = radius + dx
            out |= padded[y0 : y0 + h, x0 : x0 + w]
    return out


def remove_magenta_key(
    img: Image.Image,
    key_rgb: tuple[int, int, int] = (255, 0, 255),
    key_threshold: float = 95.0,
    magenta_excess: float = 45.0,
    despill_radius: int = 2,
    min_island_area: int = 8,
) -> Image.Image:
    """Remove magenta chroma-key background robustly, despill edges, and return RGBA.

    Handles JPEG/AI compression noise around #FF00FF and dark anti-aliased
    border fringes (where black outlines blend with magenta).
    """
    rgba = np.array(img.convert("RGBA"), dtype=np.uint8)
    rgb_f = rgba[:, :, :3].astype(np.float32)
    alpha_orig = rgba[:, :, 3]

    kr, kg, kb = (float(c) for c in key_rgb)
    r, g, b = rgb_f[:, :, 0], rgb_f[:, :, 1], rgb_f[:, :, 2]

    dist = np.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2)
    bg_mask = (alpha_orig < 128) | (dist < key_threshold)

    is_magenta_key = (min(kr, kb) - kg) > 80.0
    if is_magenta_key and magenta_excess > 0.0:
        mag = np.minimum(r, b) - g
        # Catch darkened magenta fringes along dark outlines (R ≈ B >> G)
        bg_mask |= (mag > magenta_excess) & (np.abs(r - b) < 90.0) & (dist < 220.0)

    fg_mask = ~bg_mask
    if min_island_area > 1 and fg_mask.any():
        # Guard against wiping out tiny legitimate sprites by capping min_island_area
        total_fg = int(fg_mask.sum())
        effective_min = min(min_island_area, max(1, total_fg // 20))
        if effective_min > 1:
            fg_mask = _remove_small_islands(fg_mask, effective_min)
            bg_mask = ~fg_mask

    # Boundary-only magenta despill so interior purple details (if any) stay untouched
    if is_magenta_key and despill_radius > 0 and fg_mask.any() and bg_mask.any():
        near_bg = _dilate_mask(bg_mask, despill_radius) & fg_mask
        if near_bg.any():
            nr = r[near_bg]
            ng = g[near_bg]
            nb = b[near_bg]
            spill = np.maximum(0.0, np.minimum(nr, nb) - ng)
            magenta_tint = np.abs(nr - nb) < 90.0
            apply = magenta_tint & (spill > 0.0)
            if apply.any():
                nr[apply] = np.clip(nr[apply] - spill[apply], 0.0, 255.0)
                nb[apply] = np.clip(nb[apply] - spill[apply], 0.0, 255.0)
                rgb_f[:, :, 0][near_bg] = nr
                rgb_f[:, :, 2][near_bg] = nb

    out_rgba = np.zeros_like(rgba)
    out_rgba[fg_mask, :3] = np.clip(np.rint(rgb_f[fg_mask]), 0, 255).astype(np.uint8)
    out_rgba[fg_mask, 3] = 255
    return Image.fromarray(out_rgba, mode="RGBA")


def crop_to_content_bbox(rgba_img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    """Crop an RGBA image tightly to pixels with alpha > 0."""
    bbox = rgba_img.getbbox()
    if bbox is None:
        return rgba_img, None
    return rgba_img.crop(bbox), bbox


# ---------------------------------------------------------------------------
# Auto-Slice Connected Component Pose Extraction
# ---------------------------------------------------------------------------

def _box_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    dx = max(0, b[0] - a[2], a[0] - b[2])
    dy = max(0, b[1] - a[3], a[1] - b[3])
    return float(np.hypot(dx, dy))


def _merge_fragments_into_poses(
    comps: list[tuple[int, int, int, int, int]],
    gap: int,
) -> list[tuple[int, int, int, int]]:
    """Merge small detached fragments (sparks/motion lines) into nearest major pose.

    Never merges two full character poses together even when adjacent poses on a
    sheet are closer than `gap` pixels (e.g. 10-15 px apart).
    """
    if not comps:
        return []
    areas = sorted(c[4] for c in comps)
    # Estimate typical full-pose area from upper half of components
    major_sample = areas[len(areas) // 2 :]
    median_major = float(np.median(major_sample)) if major_sample else float(areas[-1])
    frag_thresh = max(1500.0, 0.30 * median_major)

    majors = [[c[0], c[1], c[2], c[3]] for c in comps if c[4] >= frag_thresh]
    frags = [c for c in comps if c[4] < frag_thresh]
    if not majors:
        return [(c[0], c[1], c[2], c[3]) for c in comps]

    leftover: list[tuple[int, int, int, int]] = []
    if gap > 0:
        for fx0, fy0, fx1, fy1, _ in frags:
            fbox = (fx0, fy0, fx1, fy1)
            best_i = -1
            best_d = float(gap) + 1e-6
            for i, m in enumerate(majors):
                d = _box_distance(fbox, (m[0], m[1], m[2], m[3]))
                if d <= best_d:
                    best_d = d
                    best_i = i
            if best_i >= 0:
                m = majors[best_i]
                m[0] = min(m[0], fx0)
                m[1] = min(m[1], fy0)
                m[2] = max(m[2], fx1)
                m[3] = max(m[3], fy1)
            else:
                leftover.append(fbox)
    else:
        leftover = [(c[0], c[1], c[2], c[3]) for c in frags]

    return [(m[0], m[1], m[2], m[3]) for m in majors] + leftover


def _sort_boxes_reading_order(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Group boxes into horizontal rows by vertical overlap, then sort left-to-right."""
    if len(boxes) <= 1:
        return boxes

    by_y = sorted(boxes, key=lambda b: (b[1] + b[3]) * 0.5)
    rows: list[list[tuple[int, int, int, int]]] = []
    row_spans: list[tuple[int, int]] = []

    for box in by_y:
        cy = (box[1] + box[3]) * 0.5
        bh = max(1, box[3] - box[1])
        placed = False
        for idx, (ry0, ry1) in enumerate(row_spans):
            # Belongs to existing row if vertical overlap >= 35% of box height or center inside row span
            overlap = min(ry1, box[3]) - max(ry0, box[1])
            if overlap >= 0.35 * min(bh, ry1 - ry0) or (ry0 <= cy <= ry1):
                rows[idx].append(box)
                row_spans[idx] = (min(ry0, box[1]), max(ry1, box[3]))
                placed = True
                break
        if not placed:
            rows.append([box])
            row_spans.append((box[1], box[3]))

    ordered: list[tuple[int, int, int, int]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b[0]))
    return ordered


def auto_slice_sheet(
    sheet_path: Path,
    slice_dir: Path,
    key_rgb: tuple[int, int, int],
    key_threshold: float,
    magenta_excess: float,
    despill_radius: int,
    min_island_area: int,
    merge_gap: int,
    min_pose_area: int = 120,
) -> tuple[list[tuple[int, int, int, int]], Path, Path]:
    """Detect poses on a sprite sheet via connected components and export numbered crops."""
    with Image.open(sheet_path) as raw:
        keyed = remove_magenta_key(
            raw,
            key_rgb=key_rgb,
            key_threshold=key_threshold,
            magenta_excess=magenta_excess,
            despill_radius=despill_radius,
            min_island_area=min_island_area,
        )
        raw_rgb = raw.convert("RGB")

    alpha_mask = np.array(keyed)[:, :, 3] > 0
    comps = _find_connected_components(alpha_mask)
    raw_comps = [(c[0], c[1], c[2], c[3], c[4]) for c in comps if c[4] >= min_pose_area]
    # Merge small nearby fragments (e.g. motion lines/sparks) into adjacent poses
    merged_boxes = _merge_fragments_into_poses(raw_comps, gap=merge_gap)
    ordered_boxes = _sort_boxes_reading_order(merged_boxes)

    slice_dir.mkdir(parents=True, exist_ok=True)
    boxes_dict: dict[str, list[int]] = {}
    for idx, box in enumerate(ordered_boxes):
        crop_rgba = keyed.crop(box)
        crop_out = slice_dir / f"crop_{idx:03d}.png"
        crop_rgba.save(crop_out, format="PNG")
        boxes_dict[str(idx)] = [int(v) for v in box]

    boxes_json_path = slice_dir / "boxes.json"
    boxes_json_path.write_text(json.dumps(boxes_dict, indent=2), encoding="utf-8")

    # Annotated preview image showing detected boxes & indices
    preview = raw_rgb.copy()
    draw = ImageDraw.Draw(preview)
    for idx, (x0, y0, x1, y1) in enumerate(ordered_boxes):
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(0, 255, 128), width=2)
        tag = f"#{idx} ({x1-x0}x{y1-y0})"
        tx, ty = x0 + 2, max(0, y0 - 14)
        draw.rectangle([tx - 1, ty - 1, tx + len(tag) * 6 + 3, ty + 11], fill=(0, 0, 0))
        draw.text((tx, ty), tag, fill=(0, 255, 128))

    preview_path = slice_dir / "sheet_auto_slices.png"
    preview.save(preview_path, format="PNG")
    return ordered_boxes, boxes_json_path, preview_path


# ---------------------------------------------------------------------------
# Offsets Map, Original Frame BBoxes, and Input Resolution
# ---------------------------------------------------------------------------

def load_offsets_map(offsets_map_path: Path) -> dict[int, dict[str, Any]]:
    """Load sprites_dump/offsets_map.json into {frame_index: frame_meta}."""
    raw = json.loads(offsets_map_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        frames = raw
    elif isinstance(raw, dict) and isinstance(raw.get("frames"), list):
        frames = raw["frames"]
    else:
        raise ValueError(f"Unrecognized offsets_map.json schema in {offsets_map_path}")

    by_idx: dict[int, dict[str, Any]] = {}
    for item in frames:
        fidx = int(item["frame_index"])
        by_idx[fidx] = dict(item)
    return by_idx


def _parse_map_arg(arg_val: str) -> dict[str, Any]:
    """Parse --map from a JSON file path, inline JSON object, or '0=0,1=1,...' shorthand."""
    stripped = arg_val.strip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict):
            raise ValueError("--map JSON must be an object")
        return data
    p = Path(arg_val)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"--map file {p} must contain a JSON object")
        return data
    # Support shorthand like '0=0,1=1,2=2,3=3' or '0:0,1:1'
    if "=" in stripped or ":" in stripped:
        out: dict[str, Any] = {}
        for part in stripped.split(","):
            part = part.strip()
            if not part:
                continue
            sep = "=" if "=" in part else ":"
            if sep not in part:
                raise ValueError(f"Invalid --map entry '{part}': expected key=val")
            k, v = part.split(sep, 1)
            k, v = k.strip(), v.strip()
            out[k] = int(v) if v.isdigit() else v
        return out
    raise FileNotFoundError(f"--map file not found: {arg_val}")


def _parse_json_arg(arg_val: str) -> Any:
    stripped = arg_val.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(stripped)
    return json.loads(Path(arg_val).read_text(encoding="utf-8"))


def _normalize_box(box_spec: Any) -> tuple[int, int, int, int]:
    if isinstance(box_spec, (list, tuple)) and len(box_spec) == 4:
        x0, y0, x1, y1 = (int(v) for v in box_spec)
        return (x0, y0, x1, y1)
    if isinstance(box_spec, dict):
        if all(k in box_spec for k in ("x0", "y0", "x1", "y1")):
            return (int(box_spec["x0"]), int(box_spec["y0"]), int(box_spec["x1"]), int(box_spec["y1"]))
        if all(k in box_spec for k in ("x", "y", "w", "h")):
            x, y, w, h = int(box_spec["x"]), int(box_spec["y"]), int(box_spec["w"]), int(box_spec["h"])
            return (x, y, x + w, y + h)
    raise ValueError(f"Invalid crop box specification: {box_spec!r}")


def _resolve_rel_path(raw_path: str, base_dir: Path) -> Path:
    p = Path(raw_path)
    if p.is_absolute():
        return p
    cand = (base_dir / p).resolve()
    if cand.exists():
        return cand
    return p.resolve()


def resolve_input_sources(
    args: argparse.Namespace,
    key_rgb: tuple[int, int, int],
) -> dict[int, tuple[Image.Image, str]]:
    """Resolve input frames into {frame_index: (raw_pil_image, source_label)}."""
    sources: dict[int, tuple[Image.Image, str]] = {}

    # Case 1: --sheet provided (with --boxes or --auto-slice)
    if args.sheet is not None:
        sheet_path = args.sheet.resolve()
        if not sheet_path.exists():
            raise FileNotFoundError(f"Sheet image not found: {sheet_path}")

        boxes_lookup: dict[str, tuple[int, int, int, int]] = {}
        if args.auto_slice:
            slice_dir = args.slice_dir or (args.out.parent / f"{args.out.name}_slices")
            ordered_boxes, boxes_json_path, preview_path = auto_slice_sheet(
                sheet_path=sheet_path,
                slice_dir=slice_dir,
                key_rgb=key_rgb,
                key_threshold=args.key_threshold,
                magenta_excess=args.magenta_excess,
                despill_radius=args.despill_radius,
                min_island_area=args.min_island_area,
                merge_gap=args.merge_gap,
            )
            print(f"[AUTO-SLICE] Detected {len(ordered_boxes)} poses -> {slice_dir}")
            print(f"[AUTO-SLICE] Boxes JSON : {boxes_json_path}")
            print(f"[AUTO-SLICE] Preview PNG: {preview_path}")
            for i, b in enumerate(ordered_boxes):
                boxes_lookup[str(i)] = b
            if args.map is None and args.boxes is None:
                return {}
        if args.boxes is not None:
            raw_boxes = _parse_json_arg(str(args.boxes))
            if isinstance(raw_boxes, list):
                for i, b in enumerate(raw_boxes):
                    boxes_lookup[str(i)] = _normalize_box(b)
            elif isinstance(raw_boxes, dict):
                for k, b in raw_boxes.items():
                    boxes_lookup[str(k)] = _normalize_box(b)
            else:
                raise ValueError("--boxes JSON must be a list of boxes or a dict {key: box}")

        with Image.open(sheet_path) as sheet_img:
            sheet_rgba = sheet_img.convert("RGBA")

        if args.map is not None:
            raw_map = _parse_map_arg(str(args.map))
            for k, v in raw_map.items():
                fidx = int(k)
                if isinstance(v, (int, str)) and str(v) in boxes_lookup:
                    box = boxes_lookup[str(v)]
                    label = f"{sheet_path.name}#box[{v}]"
                elif isinstance(v, (list, dict)):
                    box = _normalize_box(v)
                    label = f"{sheet_path.name}#box{box}"
                else:
                    raise ValueError(f"Frame {fidx}: map value {v!r} not found in boxes ({list(boxes_lookup)[:8]}...)")
                sources[fidx] = (sheet_rgba.crop(box), label)
        else:
            if not boxes_lookup:
                raise ValueError("--sheet requires --boxes, --auto-slice, or --map")
            for k, box in boxes_lookup.items():
                fidx = int(k)
                sources[fidx] = (sheet_rgba.crop(box), f"{sheet_path.name}#box[{k}]")
        return sources

    # Case 2: --map standalone JSON {frame_index: "path"}
    if args.map is not None:
        map_is_file = Path(args.map).exists()
        base_dir = Path(args.map).resolve().parent if map_is_file else Path.cwd()
        raw_map = _parse_map_arg(args.map)
        for k, v in raw_map.items():
            fidx = int(k)
            if isinstance(v, dict):
                p = _resolve_rel_path(str(v["path"]), base_dir)
                with Image.open(p) as im:
                    crop = im.convert("RGBA")
                if "box" in v:
                    crop = crop.crop(_normalize_box(v["box"]))
                sources[fidx] = (crop, str(p))
            else:
                p = _resolve_rel_path(str(v), base_dir)
                with Image.open(p) as im:
                    sources[fidx] = (im.convert("RGBA"), str(p))
        return sources

    # Case 3: --input (folder of frame_XXXX.* OR JSON map file)
    if args.input is not None:
        in_path = args.input.resolve()
        if not in_path.exists():
            raise FileNotFoundError(f"Input path does not exist: {in_path}")
        if in_path.is_file() and in_path.suffix.lower() == ".json":
            raw_map = json.loads(in_path.read_text(encoding="utf-8"))
            if isinstance(raw_map, dict) and "frames" in raw_map and isinstance(raw_map["frames"], dict):
                raw_map = raw_map["frames"]
            if not isinstance(raw_map, dict):
                raise ValueError(f"JSON input map {in_path} must be an object {{frame_index: path}}")
            for k, v in raw_map.items():
                fidx = int(k)
                if isinstance(v, dict):
                    p = _resolve_rel_path(str(v["path"]), in_path.parent)
                    with Image.open(p) as im:
                        crop = im.convert("RGBA")
                    if "box" in v:
                        crop = crop.crop(_normalize_box(v["box"]))
                    sources[fidx] = (crop, str(p))
                else:
                    p = _resolve_rel_path(str(v), in_path.parent)
                    with Image.open(p) as im:
                        sources[fidx] = (im.convert("RGBA"), str(p))
            return sources

        if in_path.is_dir():
            for child in sorted(in_path.iterdir()):
                if not child.is_file() or child.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
                    continue
                m = FRAME_FILE_RE.match(child.name)
                if not m:
                    continue
                fidx = int(m.group(1))
                if fidx in sources:
                    raise ValueError(f"Duplicate input image for frame {fidx}: {sources[fidx][1]} and {child}")
                with Image.open(child) as im:
                    sources[fidx] = (im.convert("RGBA"), str(child))
            if not sources:
                raise ValueError(
                    f"No frame_XXXX.(png|jpg|webp|jfif) files found in directory: {in_path}"
                )
            return sources

        raise ValueError(f"--input must be a directory or .json map file: {in_path}")

    raise ValueError("Provide an input source: --input <dir|map.json>, --map <map.json>, or --sheet <img_path> ...")


# ---------------------------------------------------------------------------
# Fitting Pose to Original Frame BBox
# ---------------------------------------------------------------------------

def get_original_frame_info(
    frame_idx: int,
    meta: dict[str, Any],
    orig_frames_dir: Path,
    repo_root: Path,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Load original Rugal frame PNG and compute its opaque pixel bbox."""
    cand = orig_frames_dir / f"frame_{frame_idx:04d}.png"
    if not cand.exists() and meta.get("png_path"):
        cand = repo_root / str(meta["png_path"])
    if not cand.exists():
        raise FileNotFoundError(
            f"Original Rugal frame {frame_idx:04d} not found at {orig_frames_dir} or {cand}"
        )

    with Image.open(cand) as im:
        orig_copy = im.copy()
        arr = np.array(orig_copy, dtype=np.uint8)

    ys, xs = np.where(arr > 0)
    w, h = orig_copy.size
    if len(ys) == 0:
        return orig_copy, (0, 0, w, h)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return orig_copy, bbox


def fit_pose_to_frame(
    pose_rgba: Image.Image,
    target_w: int,
    target_h: int,
    orig_bbox: tuple[int, int, int, int],
    fit_mode: str = "height",
    scale_factor: float = 1.0,
    frame_idx: int = 0,
    fixed_height: float | None = None,
    align_x: str = "center",
) -> tuple[Image.Image, dict[str, Any]]:
    """Scale pose with nearest-neighbour to match target height and anchor bottom-center."""
    src_w, src_h = pose_rgba.size
    ox0, oy0, ox1, oy1 = orig_bbox
    orig_bbox_h = max(1, oy1 - oy0)
    anchor_x = (ox0 + ox1) / 2.0
    anchor_y = float(oy1)

    desired_h = float(fixed_height) if fixed_height is not None else float(orig_bbox_h)
    scale = (desired_h / float(src_h)) * scale_factor
    if fit_mode == "contain":
        max_w_scale = target_w / float(src_w)
        max_h_scale = target_h / float(src_h)
        scale = min(scale, max_w_scale, max_h_scale)
    elif fit_mode == "lock_height":
        # Keep consistent character height; only shrink height if taller than canvas,
        # never crushing body size because of wide punch/spark effects.
        max_h_scale = target_h / float(src_h)
        scale = min(scale, max_h_scale)

    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    if fit_mode == "contain" and new_w > target_w:
        new_w = target_w
    if fit_mode in ("contain", "lock_height") and new_h > target_h:
        new_h = target_h
    scaled = pose_rgba.resize((new_w, new_h), resample=Image.Resampling.NEAREST)

    if align_x == "right":
        # Keep right side (fighter body when facing left!) inside canvas; let left-side attack spark clip if wider
        paste_x = target_w - new_w
    elif align_x == "left":
        paste_x = 0
    else:
        paste_x = int(round(anchor_x - new_w / 2.0))
        if fit_mode == "contain":
            paste_x = max(0, min(target_w - new_w, paste_x))
        elif fit_mode == "lock_height":
            if new_w <= target_w:
                paste_x = max(0, min(target_w - new_w, paste_x))
    paste_y = int(round(anchor_y - new_h))
    if fit_mode in ("contain", "lock_height"):
        paste_y = max(0, min(target_h - new_h, paste_y))

    # Clip cleanly if scaled sprite overhangs target canvas
    src_x0 = max(0, -paste_x)
    src_y0 = max(0, -paste_y)
    dst_x0 = max(0, paste_x)
    dst_y0 = max(0, paste_y)
    dst_x1 = min(target_w, paste_x + new_w)
    dst_y1 = min(target_h, paste_y + new_h)

    clipped = (
        paste_x < 0
        or paste_y < 0
        or (paste_x + new_w) > target_w
        or (paste_y + new_h) > target_h
    )
    if clipped:
        print(
            f"[WARN] frame_{frame_idx:04d}: scaled pose {new_w}x{new_h} at ({paste_x},{paste_y}) "
            f"exceeds {target_w}x{target_h} canvas; clipping to fit."
        )

    canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    if dst_x1 > dst_x0 and dst_y1 > dst_y0:
        crop_w = dst_x1 - dst_x0
        crop_h = dst_y1 - dst_y0
        region = scaled.crop((src_x0, src_y0, src_x0 + crop_w, src_y0 + crop_h))
        canvas.paste(region, (dst_x0, dst_y0))

    info = {
        "src_size": [src_w, src_h],
        "scaled_size": [new_w, new_h],
        "orig_bbox": list(orig_bbox),
        "anchor_bottom_center": [anchor_x, anchor_y],
        "paste_xy": [paste_x, paste_y],
        "clipped": clipped,
    }
    return canvas, info


# ---------------------------------------------------------------------------
# Shared 15-Color Palette Building & Remapping
# ---------------------------------------------------------------------------

def _sort_palette_by_luminance(colors: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    return sorted(colors, key=lambda c: (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2], c[0], c[1], c[2]))


def build_shared_palette(
    fitted_frames: list[Image.Image],
    palette_from: Path | None = None,
    quantize_method: str = "mediancut",
    key_rgb: tuple[int, int, int] = (255, 0, 255),
    key_threshold: float = 95.0,
    magenta_excess: float = 45.0,
) -> list[tuple[int, int, int]]:
    """Build 16-entry palette: index 0 = (255,0,255) transparent, 1..15 = shared opaque colors."""
    if palette_from is not None:
        if not palette_from.exists():
            raise FileNotFoundError(f"--palette-from file not found: {palette_from}")
        if palette_from.suffix.lower() == ".json":
            pdata = json.loads(palette_from.read_text(encoding="utf-8"))
            cols = [tuple(int(c) for c in rgb[:3]) for rgb in pdata["palette_rgb"][1:16]]
            while len(cols) < 15:
                cols.append((0, 0, 0))
            return [(255, 0, 255)] + cols[:15]

        with Image.open(palette_from) as ref:
            if "kof_palette_rgb" in ref.info:
                cols_raw = json.loads(ref.info["kof_palette_rgb"])
                cols = [tuple(int(c) for c in rgb[:3]) for rgb in cols_raw[1:16]]
                while len(cols) < 15:
                    cols.append((0, 0, 0))
                return [(255, 0, 255)] + cols[:15]
            if ref.mode == "P":
                raw_pal = ref.getpalette() or []
                arr = np.array(ref, dtype=np.uint8)
                max_idx = int(arr.max()) if arr.size else 0
                if max_idx <= 15 and len(raw_pal) >= 48:
                    cols = [
                        (int(raw_pal[i * 3]), int(raw_pal[i * 3 + 1]), int(raw_pal[i * 3 + 2]))
                        for i in range(1, 16)
                    ]
                    return [(255, 0, 255)] + cols
            ref_keyed = remove_magenta_key(
                ref,
                key_rgb=key_rgb,
                key_threshold=key_threshold,
                magenta_excess=magenta_excess,
                despill_radius=1,
                min_island_area=1,
            )
            ref_arr = np.array(ref_keyed, dtype=np.uint8)
            opaque_pixels = ref_arr[ref_arr[:, :, 3] > 0][:, :3]
    else:
        chunks = []
        for frame in fitted_frames:
            arr = np.array(frame, dtype=np.uint8)
            fg = arr[arr[:, :, 3] > 0][:, :3]
            if len(fg):
                chunks.append(fg)
        if not chunks:
            raise ValueError("All input frames are completely transparent after keying; cannot build palette.")
        opaque_pixels = np.vstack(chunks)

    # Check if <= 15 unique colors already exist (exact preservation for pixel art)
    unique_colors = np.unique(opaque_pixels, axis=0)
    if len(unique_colors) <= 15:
        cols = _sort_palette_by_luminance([tuple(int(v) for v in row) for row in unique_colors])
    else:
        # Balance 5-bit RGB color bins via sqrt-frequency capping so large dark
        # outlines do not consume 4-5 slots and metallic/highlight accents stay crisp.
        q_bins = opaque_pixels >> 3
        _, inv, counts = np.unique(q_bins, axis=0, return_inverse=True, return_counts=True)
        rng = np.random.default_rng(0)
        bal_chunks = []
        for bin_idx in range(len(counts)):
            idxs = np.flatnonzero(inv == bin_idx)
            take = min(len(idxs), max(1, int(np.sqrt(len(idxs)))))
            if len(idxs) > take:
                idxs = rng.choice(idxs, size=take, replace=False)
            bal_chunks.append(opaque_pixels[idxs])
        sampled = np.vstack(bal_chunks)

        n = len(sampled)
        side_w = max(1, int(np.ceil(np.sqrt(n))))
        side_h = max(1, int(np.ceil(n / side_w)))
        packed = np.empty((side_w * side_h, 3), dtype=np.uint8)
        packed[:n] = sampled
        if side_w * side_h > n:
            # Repeat existing pixels so padding introduces zero synthetic colors
            rem = side_w * side_h - n
            reps = (rem + n - 1) // n
            packed[n:] = np.tile(sampled, (reps, 1))[:rem]
        montage = Image.fromarray(packed.reshape(side_h, side_w, 3), mode="RGB")
        q_img = montage.quantize(
            colors=15,
            method=QUANTIZE_METHODS[quantize_method],
            dither=Image.Dither.NONE,
        )
        raw_q = q_img.getpalette() or []
        used_indices = sorted(int(v) for v in np.unique(np.array(q_img, dtype=np.uint8)))
        cols = [
            (int(raw_q[idx * 3]), int(raw_q[idx * 3 + 1]), int(raw_q[idx * 3 + 2]))
            for idx in used_indices
            if idx * 3 + 2 < len(raw_q)
        ][:15]
        cols = _sort_palette_by_luminance(cols)

    while len(cols) < 15:
        cols.append((0, 0, 0))
    return [(255, 0, 255)] + cols[:15]


def remap_frame_to_palette(rgba_img: Image.Image, palette_16: list[tuple[int, int, int]]) -> np.ndarray:
    """Remap RGBA frame to palette indices 0..15 with zero dithering.

    Transparent pixels (alpha == 0) always map to index 0; opaque pixels map
    strictly to indices 1..15 by nearest Euclidean RGB distance.
    """
    arr = np.array(rgba_img, dtype=np.uint8)
    h, w = arr.shape[:2]
    indices = np.zeros((h, w), dtype=np.uint8)
    fg_mask = arr[:, :, 3] > 0
    if not fg_mask.any():
        return indices

    pal_15 = np.array(palette_16[1:16], dtype=np.float32)  # shape (15, 3)
    fg_rgb = arr[fg_mask, :3].astype(np.float32)  # shape (M, 3)
    dists = np.sum((fg_rgb[:, None, :] - pal_15[None, :, :]) ** 2, axis=2)
    indices[fg_mask] = (np.argmin(dists, axis=1) + 1).astype(np.uint8)
    return indices


def save_indexed_png(indices: np.ndarray, palette_16: list[tuple[int, int, int]], out_path: Path) -> None:
    """Write a mode='P' 16-color PNG with PLTE[0]=(255,0,255) and tRNS transparency=0."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(indices, mode="P")
    flat_pal = [ch for rgb in palette_16[:16] for ch in rgb]
    img.putpalette(flat_pal)
    img.save(out_path, format="PNG", transparency=0, optimize=False)


# ---------------------------------------------------------------------------
# Validation, Palette Artifacts, and Comparison Preview Sheet
# ---------------------------------------------------------------------------

def validate_output_png(
    png_path: Path,
    expected_w: int,
    expected_h: int,
    expected_palette_16: list[tuple[int, int, int]],
) -> None:
    """Validate an output PNG against scripts/03_build_mod_iso.py + palette rules."""
    with Image.open(png_path) as img:
        if img.format != "PNG":
            raise ValueError(f"{png_path}: format is '{img.format}', expected 'PNG'")
        if img.mode != "P":
            raise ValueError(f"{png_path}: mode is '{img.mode}', expected 'P'")
        if img.size != (expected_w, expected_h):
            raise ValueError(f"{png_path}: size is {img.size}, expected {(expected_w, expected_h)}")
        if img.info.get("transparency") != 0:
            raise ValueError(f"{png_path}: tRNS transparency is {img.info.get('transparency')!r}, expected 0")
        arr = np.array(img, dtype=np.uint8)
        max_idx = int(arr.max()) if arr.size else 0
        if max_idx > 15:
            raise ValueError(f"{png_path}: max palette index is {max_idx}, must be <= 15")
        raw_pal = img.getpalette() or []
        if len(raw_pal) < 48:
            raise ValueError(f"{png_path}: palette too short ({len(raw_pal)} bytes)")
        got_pal = [
            (int(raw_pal[i * 3]), int(raw_pal[i * 3 + 1]), int(raw_pal[i * 3 + 2]))
            for i in range(16)
        ]
        if got_pal != expected_palette_16:
            raise ValueError(f"{png_path}: palette mismatch against shared 16-color palette")


def _rgb8_to_rgb555(r: int, g: int, b: int) -> int:
    r5 = (r * 31 + 127) // 255
    g5 = (g * 31 + 127) // 255
    b5 = (b * 31 + 127) // 255
    return 0x8000 | (b5 << 10) | (g5 << 5) | r5


def write_palette_outputs(
    palette_16: list[tuple[int, int, int]],
    palette_png_path: Path,
    palette_json_path: Path,
    frame_records: list[dict[str, Any]],
) -> None:
    """Write palette swatch PNG and palette.json metadata."""
    swatch_w = 52
    swatch_h = 64
    gap = 6
    pad = 16
    header_h = 34
    img_w = pad * 2 + 16 * swatch_w + 15 * gap
    img_h = pad + header_h + swatch_h + 44

    canvas = Image.new("RGB", (img_w, img_h), (24, 24, 30))
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, pad), "Shared 16-Color Palette (Index 0 = Transparent #FF00FF, Indices 1..15 = Character)", fill=(235, 235, 240))

    pal_hex = []
    pal_555 = []
    for idx, (r, g, b) in enumerate(palette_16):
        x0 = pad + idx * (swatch_w + gap)
        y0 = pad + header_h
        if idx == 0:
            # Checkerboard + magenta corner for transparent index 0
            for cy in range(0, swatch_h, 8):
                for cx in range(0, swatch_w, 8):
                    col = (85, 85, 95) if ((cx // 8 + cy // 8) % 2 == 0) else (55, 55, 65)
                    draw.rectangle([x0 + cx, y0 + cy, min(x0 + swatch_w - 1, x0 + cx + 7), min(y0 + swatch_h - 1, y0 + cy + 7)], fill=col)
            draw.rectangle([x0, y0, x0 + swatch_w - 1, y0 + swatch_h - 1], outline=(255, 0, 255), width=2)
            draw.text((x0 + 4, y0 + 24), "TRANSP", fill=(255, 160, 255))
        else:
            draw.rectangle([x0, y0, x0 + swatch_w - 1, y0 + swatch_h - 1], fill=(r, g, b), outline=(180, 180, 195))

        hex_str = f"#{r:02X}{g:02X}{b:02X}"
        pal_hex.append(hex_str)
        pal_555.append(0x0000 if idx == 0 else _rgb8_to_rgb555(r, g, b))
        draw.text((x0 + 2, y0 + swatch_h + 6), f"[{idx:02d}]", fill=(220, 220, 230))
        draw.text((x0 + 2, y0 + swatch_h + 20), hex_str, fill=(160, 170, 185))

    palette_png_path.parent.mkdir(parents=True, exist_ok=True)
    png_info = PngInfo()
    png_info.add_text("kof_palette_rgb", json.dumps([list(c) for c in palette_16]))
    canvas.save(palette_png_path, format="PNG", pnginfo=png_info)

    payload = {
        "index_0_transparent": True,
        "palette_rgb": [list(c) for c in palette_16],
        "palette_rgba": [[c[0], c[1], c[2], 0 if i == 0 else 255] for i, c in enumerate(palette_16)],
        "palette_hex": pal_hex,
        "palette_rgb555": pal_555,
        "frames": frame_records,
    }
    palette_json_path.parent.mkdir(parents=True, exist_ok=True)
    palette_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _indexed_to_rgba(img_p: Image.Image) -> Image.Image:
    """Render a mode='P' sprite (index 0 transparent) as RGBA using its PLTE."""
    arr = np.array(img_p, dtype=np.uint8)
    raw_pal = img_p.getpalette() or []
    lut = np.zeros((256, 4), dtype=np.uint8)
    for i in range(1, min(256, len(raw_pal) // 3)):
        lut[i] = [raw_pal[i * 3], raw_pal[i * 3 + 1], raw_pal[i * 3 + 2], 255]
    return Image.fromarray(lut[arr], mode="RGBA")


def _make_checkerboard(w: int, h: int, cell: int = 8) -> Image.Image:
    ys = (np.arange(h) // cell)[:, None]
    xs = (np.arange(w) // cell)[None, :]
    dark = ((xs + ys) % 2) == 0
    rgb = np.empty((h, w, 3), dtype=np.uint8)
    rgb[dark] = (46, 48, 56)
    rgb[~dark] = (68, 72, 82)
    return Image.fromarray(rgb, mode="RGB")


def build_preview_comparison_sheet(
    pairs: list[tuple[int, Image.Image, Path, dict[str, Any]]],
    palette_16: list[tuple[int, int, int]],
    out_path: Path,
    zoom: int = 2,
    cols: int = 2,
) -> Path:
    """Generate a zoomed side-by-side contact sheet comparing Original Rugal vs Custom frames."""
    if not pairs:
        raise ValueError("No frames to build preview sheet.")

    max_w = max(orig.size[0] for _, orig, _, _ in pairs)
    max_h = max(orig.size[1] for _, orig, _, _ in pairs)
    zw = max_w * zoom
    zh = max_h * zoom

    pair_gap = 12
    cell_pad = 16
    header_h = 28
    footer_h = 26
    cell_w = cell_pad * 2 + zw * 2 + pair_gap
    cell_h = header_h + zh + footer_h + 12

    n = len(pairs)
    grid_cols = min(cols, n)
    grid_rows = (n + grid_cols - 1) // grid_cols

    top_bar_h = 68
    margin = 16
    sheet_w = margin * 2 + grid_cols * cell_w + (grid_cols - 1) * 14
    sheet_h = margin * 2 + top_bar_h + grid_rows * cell_h + (grid_rows - 1) * 14

    sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 22, 28))
    draw = ImageDraw.Draw(sheet)

    draw.text((margin, margin), f"Custom vs Original Rugal Comparison ({n} frames, {zoom}x nearest)", fill=(240, 242, 248))
    # Draw shared palette strip along top
    sw_size = 24
    for idx, (r, g, b) in enumerate(palette_16):
        sx = margin + idx * (sw_size + 4)
        sy = margin + 22
        if idx == 0:
            draw.rectangle([sx, sy, sx + sw_size - 1, sy + sw_size - 1], fill=(46, 48, 56), outline=(255, 0, 255), width=2)
            draw.text((sx + 7, sy + 6), "T", fill=(255, 160, 255))
        else:
            draw.rectangle([sx, sy, sx + sw_size - 1, sy + sw_size - 1], fill=(r, g, b), outline=(180, 180, 195))

    for i, (fidx, orig_img, custom_path, fit_info) in enumerate(pairs):
        col_i = i % grid_cols
        row_i = i // grid_cols
        cx0 = margin + col_i * (cell_w + 14)
        cy0 = margin + top_bar_h + row_i * (cell_h + 14)

        draw.rectangle([cx0, cy0, cx0 + cell_w - 1, cy0 + cell_h - 1], fill=(30, 33, 40), outline=(72, 78, 92))
        fw, fh = orig_img.size
        clip_tag = " [CLIPPED]" if fit_info.get("clipped") else ""
        draw.text((cx0 + 10, cy0 + 8), f"frame_{fidx:04d} ({fw}x{fh}){clip_tag}", fill=(255, 220, 120) if clip_tag else (225, 230, 240))

        orig_rgba = _indexed_to_rgba(orig_img).resize((fw * zoom, fh * zoom), Image.Resampling.NEAREST)
        with Image.open(custom_path) as cimg:
            custom_rgba = _indexed_to_rgba(cimg).resize((fw * zoom, fh * zoom), Image.Resampling.NEAREST)

        ox = cx0 + cell_pad
        oy = cy0 + header_h
        nx = ox + zw + pair_gap
        ny = oy

        cb = _make_checkerboard(fw * zoom, fh * zoom, cell=8)
        orig_comp = cb.copy()
        orig_comp.paste(orig_rgba, (0, 0), orig_rgba)
        new_comp = cb.copy()
        new_comp.paste(custom_rgba, (0, 0), custom_rgba)

        sheet.paste(orig_comp, (ox, oy))
        sheet.paste(new_comp, (nx, ny))

        # Draw subtle feet baseline across both panels at original bbox bottom y1
        foot_y = int(fit_info["anchor_bottom_center"][1] * zoom) - 1
        foot_y = max(0, min(fh * zoom - 1, foot_y))
        draw.line([(ox, oy + foot_y), (ox + fw * zoom - 1, oy + foot_y)], fill=(0, 230, 140), width=1)
        draw.line([(nx, ny + foot_y), (nx + fw * zoom - 1, ny + foot_y)], fill=(0, 230, 140), width=1)

        draw.text((ox, oy + fh * zoom + 5), "Original Rugal", fill=(165, 175, 190))
        draw.text((nx, ny + fh * zoom + 5), "Custom Indexed", fill=(130, 230, 175))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG")
    return out_path


# ---------------------------------------------------------------------------
# CLI & Main Pipeline
# ---------------------------------------------------------------------------

def build_arg_parser(default_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare custom AI/RGB character art into 16-color KOF 2002 PS2 sprite PNGs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Input directory of frame_XXXX.(png|jpg|webp|jfif) OR a JSON map {frame_index: path}.",
    )
    parser.add_argument(
        "--map",
        type=str,
        default=None,
        help="JSON map file path (or inline JSON) mapping frame_index -> image path or crop id.",
    )
    parser.add_argument(
        "--sheet",
        type=Path,
        default=None,
        help="Sprite sheet image path (combine with --boxes or --auto-slice).",
    )
    parser.add_argument(
        "--boxes",
        type=str,
        default=None,
        help="JSON file (or inline JSON) of crop boxes {id: [x0,y0,x1,y1]} or [[x0,y0,x1,y1], ...].",
    )
    parser.add_argument(
        "--auto-slice",
        action="store_true",
        help="Detect poses on --sheet via connected components and export numbered crops + boxes.json.",
    )
    parser.add_argument(
        "--slice-dir",
        type=Path,
        default=None,
        help="Directory for --auto-slice outputs (default: <out>_slices, kept outside --out for 03_build_mod_iso compatibility).",
    )
    parser.add_argument(
        "--offsets-map",
        type=Path,
        default=default_root / "sprites_dump" / "offsets_map.json",
        help="Path to sprites_dump/offsets_map.json.",
    )
    parser.add_argument(
        "--orig-frames-dir",
        type=Path,
        default=default_root / "sprites_dump" / "rugal_P27" / "frames",
        help="Directory of decoded original Rugal frames (used for bbox/feet alignment & preview).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=default_root / "custom_character",
        help="Output directory for prepared frame_XXXX.png files.",
    )
    parser.add_argument(
        "--palette-png",
        type=Path,
        default=None,
        help="Output path for palette swatch PNG (default: <out>/palettes_A.png so 03_build_mod_iso skips it).",
    )
    parser.add_argument(
        "--palette-json",
        type=Path,
        default=None,
        help="Output path for palette.json (default: <out>/palette.json).",
    )
    parser.add_argument(
        "--preview-png",
        type=Path,
        default=None,
        help="Output path for side-by-side preview sheet (default: <out>/sheet_preview.png).",
    )
    parser.add_argument(
        "--key-color",
        type=str,
        default="255,0,255",
        help="Chroma-key background color as 'R,G,B' or 'auto' (corner median).",
    )
    parser.add_argument(
        "--key-threshold",
        type=float,
        default=95.0,
        help="Euclidean RGB distance threshold from --key-color to mark background transparent.",
    )
    parser.add_argument(
        "--magenta-excess",
        type=float,
        default=45.0,
        help="Threshold on min(R,B)-G (with R~=B) to strip dark magenta anti-aliased fringes (0 disables).",
    )
    parser.add_argument(
        "--despill-radius",
        type=int,
        default=2,
        help="Pixel radius around background to remove magenta spill on remaining sprite edges (0 disables).",
    )
    parser.add_argument(
        "--min-island-area",
        type=int,
        default=8,
        help="Remove isolated foreground specks smaller than this many pixels before cropping.",
    )
    parser.add_argument(
        "--merge-gap",
        type=int,
        default=24,
        help="Merge connected components within this pixel gap during --auto-slice.",
    )
    parser.add_argument(
        "--fit-mode",
        choices=("height", "contain", "lock_height"),
        default="height",
        help="'height' scales to original bbox height; 'contain' shrinks to fit canvas; 'lock_height' preserves consistent character height without shrinking for wide effects.",
    )
    parser.add_argument(
        "--pose-heights",
        type=Path,
        default=None,
        help="Optional JSON mapping pose box ID (string) -> target pixel height (or {height, align_x}) for consistent world-scale sizing.",
    )
    parser.add_argument(
        "--scale-factor",
        type=float,
        default=1.0,
        help="Multiplier applied on top of original bbox height scale.",
    )
    parser.add_argument(
        "--flip-x",
        action="store_true",
        help="Horizontally mirror input poses (use when source art faces right; KOF 2002 PXX.BIN sprites natively face left).",
    )
    parser.add_argument(
        "--palette-from",
        type=Path,
        default=None,
        help="Optional reference PNG or palette.json to reuse/extract the 15-color palette.",
    )
    parser.add_argument(
        "--quantize-method",
        choices=tuple(QUANTIZE_METHODS.keys()),
        default="mediancut",
        help="Pillow quantization algorithm for building the shared 15-color palette.",
    )
    parser.add_argument(
        "--preview-zoom",
        type=int,
        default=2,
        help="Nearest-neighbour zoom factor for the preview comparison sheet.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip generating the side-by-side preview contact sheet.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = build_arg_parser(repo_root)
    args = parser.parse_args(argv)

    # Determine key color (support 'auto' from sheet or first input)
    sample_rgb: np.ndarray | None = None
    if args.key_color.strip().lower() == "auto" and args.sheet is not None and args.sheet.exists():
        with Image.open(args.sheet) as im:
            sample_rgb = np.array(im.convert("RGB"), dtype=np.uint8)
    key_rgb = parse_key_color(args.key_color, sample_rgb)

    sources = resolve_input_sources(args, key_rgb=key_rgb)
    if not sources:
        # --auto-slice ran standalone without --map/--boxes
        return 0

    offsets_by_idx = load_offsets_map(args.offsets_map)
    pose_heights_cfg: dict[str, Any] = {}
    if args.pose_heights is not None:
        pose_heights_cfg = json.loads(args.pose_heights.read_text(encoding="utf-8"))

    # Stage 1 & 2: Key out magenta, crop to tight bbox, fit to original Rugal bbox
    prepared: list[tuple[int, Image.Image, Image.Image, dict[str, Any], str]] = []
    for frame_idx in sorted(sources.keys()):
        if frame_idx not in offsets_by_idx:
            raise ValueError(
                f"frame_{frame_idx:04d} is not present in {args.offsets_map} "
                f"(valid range 0..{max(offsets_by_idx)})"
            )
        meta = offsets_by_idx[frame_idx]
        target_w = int(meta["width_px"])
        target_h = int(meta["height_px"])

        raw_img, src_label = sources[frame_idx]
        if args.key_color.strip().lower() == "auto" and args.sheet is None:
            per_key = parse_key_color("auto", np.array(raw_img.convert("RGB"), dtype=np.uint8))
        else:
            per_key = key_rgb

        keyed = remove_magenta_key(
            raw_img,
            key_rgb=per_key,
            key_threshold=args.key_threshold,
            magenta_excess=args.magenta_excess,
            despill_radius=args.despill_radius,
            min_island_area=args.min_island_area,
        )
        cropped, crop_bbox = crop_to_content_bbox(keyed)
        if crop_bbox is None:
            raise ValueError(f"frame_{frame_idx:04d} ({src_label}): image became completely empty after keying.")
        if args.flip_x:
            cropped = cropped.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

        orig_img, orig_bbox = get_original_frame_info(
            frame_idx, meta, args.orig_frames_dir, repo_root
        )
        fixed_h: float | None = None
        align_x = "center"
        m_box = re.search(r"#box\[(\d+)\]$", src_label)
        if m_box and m_box.group(1) in pose_heights_cfg:
            entry = pose_heights_cfg[m_box.group(1)]
            if isinstance(entry, dict):
                fixed_h = float(entry["height"]) if "height" in entry else None
                align_x = str(entry.get("align_x", "center"))
            else:
                fixed_h = float(entry)

        fitted, fit_info = fit_pose_to_frame(
            pose_rgba=cropped,
            target_w=target_w,
            target_h=target_h,
            orig_bbox=orig_bbox,
            fit_mode=args.fit_mode,
            scale_factor=args.scale_factor,
            frame_idx=frame_idx,
            fixed_height=fixed_h,
            align_x=align_x,
        )
        prepared.append((frame_idx, fitted, orig_img, fit_info, src_label))

    # Stage 3: Build ONE shared 15-color palette (+ index 0 transparent) across all frames
    palette_16 = build_shared_palette(
        fitted_frames=[item[1] for item in prepared],
        palette_from=args.palette_from,
        quantize_method=args.quantize_method,
        key_rgb=key_rgb,
        key_threshold=args.key_threshold,
        magenta_excess=args.magenta_excess,
    )

    # Stage 4: Remap without dithering, write frame_XXXX.png, and validate against 03 rules
    args.out.mkdir(parents=True, exist_ok=True)
    preview_pairs: list[tuple[int, Image.Image, Path, dict[str, Any]]] = []
    frame_records: list[dict[str, Any]] = []
    clipped_count = 0

    for frame_idx, fitted_rgba, orig_img, fit_info, src_label in prepared:
        target_w, target_h = orig_img.size
        indices = remap_frame_to_palette(fitted_rgba, palette_16)
        out_png = args.out / f"frame_{frame_idx:04d}.png"
        save_indexed_png(indices, palette_16, out_png)
        validate_output_png(out_png, target_w, target_h, palette_16)

        used_colors = sorted(int(v) for v in np.unique(indices))
        if fit_info["clipped"]:
            clipped_count += 1
        frame_records.append(
            {
                "frame_index": frame_idx,
                "output_png": str(out_png),
                "source": src_label,
                "size": [target_w, target_h],
                "used_indices": used_colors,
                "fit": fit_info,
            }
        )
        preview_pairs.append((frame_idx, orig_img, out_png, fit_info))

    palette_png_path = args.palette_png or (args.out / "palettes_A.png")
    palette_json_path = args.palette_json or (args.out / "palette.json")
    write_palette_outputs(palette_16, palette_png_path, palette_json_path, frame_records)

    preview_path: Path | None = None
    if not args.no_preview:
        preview_path = args.preview_png or (args.out / "sheet_preview.png")
        build_preview_comparison_sheet(
            pairs=preview_pairs,
            palette_16=palette_16,
            out_path=preview_path,
            zoom=args.preview_zoom,
        )

    print("=== Custom Frame Preparation Complete ===")
    print(f"Frames processed : {len(prepared)} (clipped: {clipped_count})")
    print(f"Output directory : {args.out.resolve()}")
    print(f"Shared palette   : 16 entries (index 0=#FF00FF transparent, 1..15 shared)")
    print(f"Palette swatch   : {palette_png_path.resolve()}")
    print(f"Palette JSON     : {palette_json_path.resolve()}")
    if preview_path is not None:
        print(f"Preview sheet    : {preview_path.resolve()}")
    print("Validation       : ALL frames passed 03_build_mod_iso rules (PNG, mode='P', max_idx<=15, tRNS=0, exact WxH, identical palette)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

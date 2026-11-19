#!/usr/bin/env python3
"""Decode KOF 2002 (PS2 SLES_533.81) character sprite banks and palettes.

Implements the verified KOF 2002 PS2 sprite container, column-major 4bpp
tile layout, LZ decompressor, and FIRST.GRC RGB555 palette decoder.
Exports individual 16-color Indexed PNGs (mode='P', transparency=0),
visual contact sheets, an A/B/C/D palette overview, and offsets_map.json.

Strictly read-only with respect to disc images and extracted/ assets.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

PALETTE_TABLE_OFFSET = 0x3E6960
FIRST_GRC_MAGIC = 0x00000100
FIRST_GRC_OFFSET_COUNT = 64
BYTES_PER_PALETTE = 32
COLORS_PER_PALETTE = 16
TRANSPARENT_PREVIEW_RGB = (255, 0, 255)

BTN_NAMES = ("A", "B", "C", "D")

CHAR_ALIASES: dict[str, int] = {
    "kyo": 0x00,
    "rugal": 0x27,
    "omega_rugal": 0x27,
    "omegarugal": 0x27,
    "omega-rugal": 0x27,
    "shingo": 0x2E,
    "king": 0x2F,
    "orochi_iori": 0x30,
    "orochi-iori": 0x30,
    "iori_orochi": 0x30,
    "geese": 0x31,
    "goenitz": 0x32,
}

CANONICAL_NAMES: dict[int, str] = {
    0x00: "kyo",
    0x27: "rugal",
    0x2E: "shingo",
    0x2F: "king",
    0x30: "orochi_iori",
    0x31: "geese",
    0x32: "goenitz",
}


def lz_decompress(data: bytes | memoryview) -> bytes:
    """Decompress a KOF 2002 byte-oriented LZ stream.

    Format:
      - b < 0xc0: emit literal byte b
      - b == 0xc0: end-of-stream terminator
      - b > 0xc0: read arg = next byte, l = b & 0x3f
        - l == 1: emit literal byte arg (escape for >= 0xc0)
        - l >= 2: copy l bytes sequentially from out[-arg]
    """
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
                raise ValueError("Truncated LZ back-reference argument")
            arg = data[i]
            i += 1
            length = b & 0x3F
            if length == 1:
                out.append(arg)
            else:
                if arg <= 0 or arg > len(out):
                    raise ValueError(
                        f"Invalid LZ back-reference offset -{arg} at output len {len(out)}"
                    )
                for _ in range(length):
                    out.append(out[-arg])
    return bytes(out)


def parse_char_spec(char_arg: str) -> tuple[int, str]:
    """Resolve --char argument (e.g., 'rugal', 'P27', '0x27') to (char_id, folder_slug)."""
    raw = char_arg.strip().lower()
    if raw in CHAR_ALIASES:
        cid = CHAR_ALIASES[raw]
    elif re.fullmatch(r"p[0-9a-f]{2}", raw):
        cid = int(raw[1:], 16)
    elif re.fullmatch(r"0x[0-9a-f]{1,2}", raw):
        cid = int(raw, 16)
    elif re.fullmatch(r"[0-9a-f]{2}", raw):
        cid = int(raw, 16)
    else:
        raise ValueError(
            f"Unrecognized --char value '{char_arg}'. "
            "Use 'rugal', 'P00'..'P32', or a hex char ID like '0x27'."
        )

    name = CANONICAL_NAMES.get(cid)
    slug = f"{name}_P{cid:02X}" if name else f"P{cid:02X}"
    return cid, slug


def find_char_bin(extracted_dir: Path, char_id: int, prefix: str = "PL") -> Path:
    """Locate PXX.BIN (or EPLX/PXX.BIN) inside extracted/DATA/{PL0,PL1} or {EPL0..EPL3}."""
    filename = f"P{char_id:02X}.BIN"
    if prefix == "PL":
        candidates = [
            extracted_dir / "DATA" / "PL0" / filename,
            extracted_dir / "DATA" / "PL1" / filename,
        ]
    else:
        candidates = [
            extracted_dir / "DATA" / f"{prefix}0" / filename,
            extracted_dir / "DATA" / f"{prefix}1" / filename,
        ]
    for cand in candidates:
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"Could not find {filename} in {[str(c.parent) for c in candidates]}"
    )


def load_manifest_lookup(manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Build clean_path -> manifest entry map from scripts/iso_manifest.json."""
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lookup: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        clean = entry.get("clean_path")
        if clean:
            lookup[clean] = entry
    return lookup


def read_char_base_palettes(elf_path: Path, char_id: int) -> dict[str, int]:
    """Read A/B/C/D base palette indices from SLES_533.81 at 0x3e6960."""
    offset = PALETTE_TABLE_OFFSET + char_id * 8
    elf_bytes = elf_path.read_bytes()
    if offset + 8 > len(elf_bytes):
        raise ValueError(f"Char ID 0x{char_id:02x} out of bounds for palette table")
    vals = struct.unpack_from("<4H", elf_bytes, offset)
    return {btn: vals[idx] for idx, btn in enumerate(BTN_NAMES)}


def load_first_grc_palettes(first_grc_path: Path) -> tuple[bytes, int, int]:
    """Decompress FIRST.GRC and extract sub-block [1] palette bank.

    Returns (pal_subblock_bytes, pal_subblock_base_offset, num_palettes).
    Note: FIRST.GRC stores its first offset entry (0x00000100) uncompressed
    in bytes [0:4], followed by the LZ-compressed remainder starting at [4:].
    Prepending those 4 bytes aligns the 64-entry offset table at [0:256].
    """
    raw = first_grc_path.read_bytes()
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic != FIRST_GRC_MAGIC:
        raise ValueError(
            f"Unexpected FIRST.GRC header 0x{magic:08x} (expected 0x{FIRST_GRC_MAGIC:08x})"
        )
    buf = raw[:4] + lz_decompress(raw[4:])
    offs = struct.unpack_from(f"<{FIRST_GRC_OFFSET_COUNT}I", buf, 0)
    pal_start = offs[1]
    pal_end = offs[2]
    pal_sub = buf[pal_start:pal_end]
    if len(pal_sub) % BYTES_PER_PALETTE != 0:
        raise ValueError(
            f"Palette sub-block [1] size {len(pal_sub)} is not a multiple of {BYTES_PER_PALETTE}"
        )
    return pal_sub, pal_start, len(pal_sub) // BYTES_PER_PALETTE


def decode_palette(
    pal_sub: bytes, pal_idx: int, num_palettes: int
) -> tuple[list[int], list[list[int]], list[tuple[int, int, int]]]:
    """Decode one 32-byte 16-color RGB555 palette from FIRST.GRC sub-block [1].

    Returns:
      - raw_words: 16 uint16 LE integers as stored in FIRST.GRC
      - palette_rgba: 16 [R, G, B, A] entries (index 0 forced [0, 0, 0, 0])
      - png_rgb_palette: 16 (R, G, B) entries for Indexed PNG PLTE (index 0 magenta)
    """
    if pal_idx < 0 or pal_idx >= num_palettes:
        raise IndexError(
            f"Palette index {pal_idx} (0x{pal_idx:04x}) out of range 0..{num_palettes - 1}"
        )
    words = list(struct.unpack_from("<16H", pal_sub, pal_idx * BYTES_PER_PALETTE))
    palette_rgba: list[list[int]] = [[0, 0, 0, 0]]
    png_rgb: list[tuple[int, int, int]] = [TRANSPARENT_PREVIEW_RGB]
    for w in words[1:]:
        r = ((w >> 10) & 0x1F) * 255 // 31
        g = ((w >> 5) & 0x1F) * 255 // 31
        b = (w & 0x1F) * 255 // 31
        palette_rgba.append([r, g, b, 255])
        png_rgb.append((r, g, b))
    return words, palette_rgba, png_rgb


def decode_tile_block(blk: bytes) -> tuple[dict[str, Any], np.ndarray | None]:
    """Decode a single KOF 2002 sprite block into header info and 2D index array."""
    if len(blk) < 8:
        raise ValueError(f"Sprite block too short ({len(blk)} bytes)")

    hdr_sz, pal_slot, w_tiles, h_tiles, flg = struct.unpack_from("<IBBBB", blk, 0)
    if flg == 0xFF:
        return {
            "is_empty_stub": True,
            "hdr_sz": hdr_sz,
            "pal_slot": pal_slot,
            "w_tiles": w_tiles,
            "h_tiles": h_tiles,
            "flg": flg,
            "width_px": 0,
            "height_px": 0,
        }, None

    if flg not in (0x00, 0x01, 0x03):
        raise ValueError(f"Unsupported sprite block flag 0x{flg:02x}")

    bpc = (h_tiles + 7) // 8
    mask_bytes = blk[8 : 8 + w_tiles * bpc]
    if len(mask_bytes) < w_tiles * bpc:
        raise ValueError("Truncated column-major tile presence bitmask")

    top_bit = 1 << (8 * bpc - 1)
    present_tiles: list[tuple[int, int]] = []
    for col in range(w_tiles):
        col_mask = int.from_bytes(
            mask_bytes[col * bpc : (col + 1) * bpc], "little"
        )
        for row in range(h_tiles):
            if col_mask & (top_bit >> row):
                present_tiles.append((col, row))

    payload = blk[hdr_sz:]
    if flg == 0x01:
        tile_bytes = 128
        tile_px = 16
        expected_len = len(present_tiles) * tile_bytes
        if len(payload) < expected_len:
            raise ValueError(
                f"Uncompressed block payload too short: {len(payload)} < {expected_len}"
            )
        raw_tiles = payload[:expected_len]
        src_stride = 8
    else:
        tile_bytes = 512
        tile_px = 32 if flg == 0x00 else 16
        raw_tiles = lz_decompress(payload)
        expected_len = len(present_tiles) * tile_bytes
        if len(raw_tiles) != expected_len:
            raise ValueError(
                f"Decompressed tile payload mismatch: got {len(raw_tiles)}, expected {expected_len}"
            )
        src_stride = 16

    width_px = w_tiles * tile_px
    height_px = h_tiles * tile_px
    canvas = np.zeros((height_px, width_px), dtype=np.uint8)

    if present_tiles:
        tile_arr = np.frombuffer(raw_tiles, dtype=np.uint8).reshape(
            len(present_tiles), 16 if flg == 0x01 else 32, src_stride
        )
        # Extract the active rows/bytes per tile (16x8 for 16x16, 32x16 for 32x32)
        active_xb = tile_px // 2
        active = tile_arr[:, :tile_px, :active_xb]
        # Unpack low nibble (even x) and high nibble (odd x)
        decoded_tiles = np.empty(
            (len(present_tiles), tile_px, tile_px), dtype=np.uint8
        )
        decoded_tiles[:, :, 0::2] = active & 0x0F
        decoded_tiles[:, :, 1::2] = (active >> 4) & 0x0F

        for idx, (col, row) in enumerate(present_tiles):
            x0 = col * tile_px
            y0 = row * tile_px
            canvas[y0 : y0 + tile_px, x0 : x0 + tile_px] = decoded_tiles[idx]

    info = {
        "is_empty_stub": False,
        "hdr_sz": hdr_sz,
        "pal_slot": pal_slot,
        "w_tiles": w_tiles,
        "h_tiles": h_tiles,
        "flg": flg,
        "width_px": width_px,
        "height_px": height_px,
    }
    return info, canvas


def save_indexed_png(
    indices: np.ndarray, png_rgb: list[tuple[int, int, int]], out_path: Path
) -> None:
    """Save a 16-color Indexed PNG (mode='P') with index 0 transparent (tRNS)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(indices, mode="P")
    flat_pal = [channel for rgb in png_rgb[:16] for channel in rgb]
    img.putpalette(flat_pal)
    img.save(out_path, format="PNG", transparency=0, optimize=False)


def render_rgba_from_indices(
    indices: np.ndarray, palette_rgba: list[list[int]]
) -> Image.Image:
    """Convert 4bpp index array to RGBA Pillow Image for contact sheets."""
    lut = np.array(palette_rgba, dtype=np.uint8)
    rgba = lut[indices]
    return Image.fromarray(rgba, mode="RGBA")


def build_contact_sheets(
    frame_images: list[tuple[int, Image.Image, dict[str, Any]]],
    out_dir: Path,
    prefix: str = "sheet",
    cols: int = 8,
    rows: int = 8,
) -> list[Path]:
    """Render browsable RGBA contact sheets with frame indices and dimensions."""
    if not frame_images:
        return []

    per_sheet = cols * rows
    generated: list[Path] = []
    pad = 8
    label_h = 22

    for sheet_idx in range(0, len(frame_images), per_sheet):
        chunk = frame_images[sheet_idx : sheet_idx + per_sheet]
        max_w = max(im.width for _, im, _ in chunk)
        max_h = max(im.height for _, im, _ in chunk)
        cell_w = max(max_w + pad * 2, 112)
        cell_h = max_h + label_h + pad * 2

        actual_rows = (len(chunk) + cols - 1) // cols
        sheet_w = cols * cell_w + pad
        sheet_h = actual_rows * cell_h + pad

        sheet = Image.new("RGBA", (sheet_w, sheet_h), (24, 27, 34, 255))
        draw = ImageDraw.Draw(sheet)

        for idx_in_chunk, (frame_idx, sprite_im, meta) in enumerate(chunk):
            gx = idx_in_chunk % cols
            gy = idx_in_chunk // cols
            cx = pad + gx * cell_w
            cy = pad + gy * cell_h

            # Subtle cell card
            draw.rectangle(
                [cx, cy, cx + cell_w - pad, cy + cell_h - pad],
                fill=(34, 38, 48, 255),
                outline=(58, 65, 80, 255),
            )
            # Sprite centered horizontally, aligned near bottom above label
            sx = cx + (cell_w - pad - sprite_im.width) // 2
            sy = cy + (cell_h - pad - label_h - sprite_im.height) // 2
            sheet.alpha_composite(sprite_im, (sx, sy))

            label = (
                f"#{frame_idx:04d} {meta['width_px']}x{meta['height_px']} "
                f"p{meta['pal_slot']:02x}"
            )
            if meta.get("shared_with_frame") is not None:
                label += f"={meta['shared_with_frame']}"
            draw.text(
                (cx + 4, cy + cell_h - pad - label_h + 4),
                label,
                fill=(215, 222, 235, 255),
            )

        out_path = out_dir / f"{prefix}_{sheet_idx // per_sheet:02d}.png"
        sheet.save(out_path, format="PNG")
        generated.append(out_path)

    return generated


def build_palettes_abcd_image(
    base_palettes: dict[str, int],
    pal_sub: bytes,
    num_palettes: int,
    sample_canvas: np.ndarray | None,
    out_path: Path,
) -> Path:
    """Generate palettes_ABCD.png showing A/B/C/D color swatches + sprite previews."""
    swatch = 28
    gap = 4
    left_margin = 130
    top_margin = 44
    row_h = 54
    swatches_w = COLORS_PER_PALETTE * (swatch + gap)

    preview_w = (sample_canvas.shape[1] + 16) if sample_canvas is not None else 0
    preview_h = (sample_canvas.shape[0] + 40) if sample_canvas is not None else 0

    img_w = max(left_margin + swatches_w + 24, 4 * max(preview_w, 96) + 40)
    img_h = top_margin + len(BTN_NAMES) * row_h + (preview_h + 24 if sample_canvas is not None else 16)

    im = Image.new("RGBA", (img_w, img_h), (22, 25, 32, 255))
    draw = ImageDraw.Draw(im)
    draw.text(
        (16, 12),
        "KOF 2002 Character Button Palettes (Index 0 = Transparent / Key, 1..15 = RGB555)",
        fill=(235, 240, 248, 255),
    )

    for b_idx, btn in enumerate(BTN_NAMES):
        pal_idx = base_palettes[btn]
        _, rgba, _ = decode_palette(pal_sub, pal_idx, num_palettes)
        y = top_margin + b_idx * row_h
        draw.text(
            (16, y + 6),
            f"Btn {btn}  0x{pal_idx:04x}",
            fill=(220, 226, 238, 255),
        )
        for c_idx in range(COLORS_PER_PALETTE):
            x = left_margin + c_idx * (swatch + gap)
            if c_idx == 0:
                fill_col = (*TRANSPARENT_PREVIEW_RGB, 255)
            else:
                fill_col = tuple(rgba[c_idx])
            draw.rectangle(
                [x, y, x + swatch, y + swatch],
                fill=fill_col,
                outline=(95, 102, 118, 255),
            )
            draw.text((x + 2, y + swatch + 2), f"{c_idx:X}", fill=(165, 174, 190, 255))

    if sample_canvas is not None:
        base_y = top_margin + len(BTN_NAMES) * row_h + 12
        col_w = max(preview_w, 96)
        for b_idx, btn in enumerate(BTN_NAMES):
            pal_idx = base_palettes[btn]
            _, rgba, _ = decode_palette(pal_sub, pal_idx, num_palettes)
            sprite_im = render_rgba_from_indices(sample_canvas, rgba)
            px = 16 + b_idx * col_w
            draw.rectangle(
                [px, base_y, px + col_w - 10, base_y + preview_h],
                fill=(34, 38, 48, 255),
                outline=(62, 70, 86, 255),
            )
            sx = px + (col_w - 10 - sprite_im.width) // 2
            sy = base_y + 24 + (preview_h - 32 - sprite_im.height) // 2
            im.alpha_composite(sprite_im, (sx, sy))
            draw.text(
                (px + 8, base_y + 5),
                f"Btn {btn} (0x{pal_idx:04x})",
                fill=(225, 232, 245, 255),
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, format="PNG")
    return out_path


def decode_bin_container(
    bin_path: Path,
    clean_iso_path: str,
    char_id: int,
    base_palettes: dict[str, int],
    btn: str,
    pal_sub: bytes,
    pal_sub_offset: int,
    num_palettes: int,
    manifest_lookup: dict[str, dict[str, Any]],
    frames_dir: Path,
    workspace_root: Path,
    palette_lba_start: int | None,
) -> tuple[list[dict[str, Any]], list[tuple[int, Image.Image, dict[str, Any]]], np.ndarray | None]:
    """Decode all frame slots in a character PXX.BIN / EPLX/PXX.BIN container."""
    data = bin_path.read_bytes()
    file_size = len(data)
    if file_size < 4:
        raise ValueError(f"{bin_path} is too small ({file_size} bytes)")

    first_off = struct.unpack_from("<I", data, 0)[0] & 0x7FFFFFFF
    n_frames = first_off // 4
    offs = struct.unpack_from(f"<{n_frames}I", data, 0)
    unique_offs = sorted({o & 0x7FFFFFFF for o in offs})

    manifest_entry = manifest_lookup.get(clean_iso_path, {})
    lba_start = manifest_entry.get("lba_start")
    base_pal_idx = base_palettes[btn]

    seen_offsets: dict[int, int] = {}
    records: list[dict[str, Any]] = []
    sheet_items: list[tuple[int, Image.Image, dict[str, Any]]] = []
    first_main_canvas: np.ndarray | None = None

    for frame_idx, raw_off in enumerate(offs):
        is_stub_bit = bool(raw_off & 0x80000000)
        off_int = raw_off & 0x7FFFFFFF
        next_idx = bisect_right(unique_offs, off_int)
        end_int = unique_offs[next_idx] if next_idx < len(unique_offs) else file_size
        block_size = end_int - off_int

        shared_with = seen_offsets.get(off_int)
        if shared_with is None:
            seen_offsets[off_int] = frame_idx

        blk = data[off_int:end_int]
        info, canvas = decode_tile_block(blk)

        pal_slot = info["pal_slot"]
        is_stub = is_stub_bit or info["is_empty_stub"]
        slot_delta = (pal_slot - 0x10) if pal_slot >= 0x10 else pal_slot
        pal_idx = base_pal_idx + slot_delta
        if pal_idx >= num_palettes:
            # Fallback safely to character base palette if out-of-range effect slot appears
            pal_idx = base_pal_idx

        words, palette_rgba, png_rgb = decode_palette(pal_sub, pal_idx, num_palettes)
        pal_abs_off = pal_sub_offset + pal_idx * BYTES_PER_PALETTE

        png_rel_path: str | None = None
        if not is_stub and canvas is not None:
            out_png = frames_dir / f"frame_{frame_idx:04d}.png"
            save_indexed_png(canvas, png_rgb, out_png)
            try:
                png_rel_path = out_png.resolve().relative_to(
                    workspace_root.resolve()
                ).as_posix()
            except ValueError:
                png_rel_path = out_png.as_posix()

            rgba_im = render_rgba_from_indices(canvas, palette_rgba)
            if first_main_canvas is None and pal_slot == 0x10:
                first_main_canvas = canvas

        rec: dict[str, Any] = {
            "char_id": f"0x{char_id:02x}",
            "char_id_int": char_id,
            "source_file": clean_iso_path,
            "lba_start": lba_start,
            "file_size_bytes": file_size,
            "frame_index": frame_idx,
            "offset_hex": f"0x{off_int:06x}",
            "offset_int": off_int,
            "block_size_bytes": block_size,
            "shared_with_frame": shared_with,
            "is_empty_stub": is_stub,
            "hdr_sz": info["hdr_sz"],
            "w_tiles": info["w_tiles"],
            "h_tiles": info["h_tiles"],
            "width_px": info["width_px"],
            "height_px": info["height_px"],
            "flg": info["flg"],
            "pal_slot": pal_slot,
            "pal_slot_hex": f"0x{pal_slot:02x}",
            "palette_file": "/DATA/BASE/FIRST.GRC",
            "palette_lba_start": palette_lba_start,
            "palette_index": pal_idx,
            "palette_index_hex": f"0x{pal_idx:04x}",
            "palette_subblock_offset_hex": f"0x{pal_abs_off:05x}",
            "palette_rgba": palette_rgba,
            "palette_words": words,
            "png_path": png_rel_path,
        }
        records.append(rec)

        if not is_stub and canvas is not None:
            sheet_items.append((frame_idx, rgba_im, rec))

    return records, sheet_items, first_main_canvas


def build_arg_parser(default_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decode KOF 2002 character sprites (mode='P' 16-color PNG) and offsets_map.json."
    )
    parser.add_argument(
        "--char",
        default="rugal",
        help="Character name or ID: 'rugal', 'P27', 'P00'..'P32', '0x27' (default: rugal).",
    )
    parser.add_argument(
        "--btn",
        default="A",
        choices=["A", "B", "C", "D", "a", "b", "c", "d"],
        help="Button palette A/B/C/D (default: A).",
    )
    parser.add_argument(
        "--all-buttons",
        action="store_true",
        help="Also export contact sheets for all 4 button palettes (A, B, C, D).",
    )
    parser.add_argument(
        "--include-epl",
        action="store_true",
        help="Also decode matching EPL0/EPL1 PXX.BIN subset sprites.",
    )
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=default_root / "extracted",
        help="Path to extracted disc files (default: <workspace>/extracted).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=default_root / "scripts" / "iso_manifest.json",
        help="Path to iso_manifest.json (default: <workspace>/scripts/iso_manifest.json).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=default_root / "sprites_dump",
        help="Destination directory for decoded sprites and offsets_map.json (default: <workspace>/sprites_dump).",
    )
    parser.add_argument(
        "--no-sheets",
        action="store_true",
        help="Skip generating contact sheets (still writes individual frames and palettes_ABCD.png).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent
    parser = build_arg_parser(default_root)
    args = parser.parse_args(argv)

    btn = args.btn.upper()
    char_id, char_slug = parse_char_spec(args.char)
    extracted_dir = args.extracted_dir
    out_dir = args.out_dir
    char_out_dir = out_dir / char_slug
    frames_dir = char_out_dir / "frames"

    elf_path = extracted_dir / "SLES_533.81"
    first_grc_path = extracted_dir / "DATA" / "BASE" / "FIRST.GRC"
    if not elf_path.is_file() or not first_grc_path.is_file():
        print(
            f"ERROR: Missing extracted files ({elf_path} or {first_grc_path}). "
            "Run scripts/01_inspect_and_extract.py first.",
            file=sys.stderr,
        )
        return 2

    manifest_lookup = load_manifest_lookup(args.manifest)
    palette_lba_start = manifest_lookup.get("/DATA/BASE/FIRST.GRC", {}).get("lba_start")

    base_palettes = read_char_base_palettes(elf_path, char_id)
    pal_sub, pal_sub_offset, num_palettes = load_first_grc_palettes(first_grc_path)
    bin_path = find_char_bin(extracted_dir, char_id, prefix="PL")
    rel_clean = "/" + bin_path.relative_to(extracted_dir).as_posix()

    records, sheet_items, sample_canvas = decode_bin_container(
        bin_path=bin_path,
        clean_iso_path=rel_clean,
        char_id=char_id,
        base_palettes=base_palettes,
        btn=btn,
        pal_sub=pal_sub,
        pal_sub_offset=pal_sub_offset,
        num_palettes=num_palettes,
        manifest_lookup=manifest_lookup,
        frames_dir=frames_dir,
        workspace_root=default_root,
        palette_lba_start=palette_lba_start,
    )

    sheets_written: list[Path] = []
    if not args.no_sheets:
        sheets_written = build_contact_sheets(sheet_items, char_out_dir, prefix="sheet")

    abcd_path = build_palettes_abcd_image(
        base_palettes=base_palettes,
        pal_sub=pal_sub,
        num_palettes=num_palettes,
        sample_canvas=sample_canvas,
        out_path=char_out_dir / "palettes_ABCD.png",
    )

    if args.all_buttons:
        for b_name in BTN_NAMES:
            if b_name == btn:
                continue
            b_dir = char_out_dir / f"btn_{b_name}"
            _, b_items, _ = decode_bin_container(
                bin_path=bin_path,
                clean_iso_path=rel_clean,
                char_id=char_id,
                base_palettes=base_palettes,
                btn=b_name,
                pal_sub=pal_sub,
                pal_sub_offset=pal_sub_offset,
                num_palettes=num_palettes,
                manifest_lookup=manifest_lookup,
                frames_dir=b_dir / "frames",
                workspace_root=default_root,
                palette_lba_start=palette_lba_start,
            )
            if not args.no_sheets:
                build_contact_sheets(b_items, b_dir, prefix=f"sheet_{b_name}")

    epl_records: list[dict[str, Any]] = []
    if args.include_epl:
        epl_path = find_char_bin(extracted_dir, char_id, prefix="EPL")
        epl_clean = "/" + epl_path.relative_to(extracted_dir).as_posix()
        epl_dir = char_out_dir / "epl"
        epl_records, epl_items, _ = decode_bin_container(
            bin_path=epl_path,
            clean_iso_path=epl_clean,
            char_id=char_id,
            base_palettes=base_palettes,
            btn=btn,
            pal_sub=pal_sub,
            pal_sub_offset=pal_sub_offset,
            num_palettes=num_palettes,
            manifest_lookup=manifest_lookup,
            frames_dir=epl_dir / "frames",
            workspace_root=default_root,
            palette_lba_start=palette_lba_start,
        )
        if not args.no_sheets and epl_items:
            build_contact_sheets(epl_items, epl_dir, prefix="epl_sheet")

    offsets_map: dict[str, Any] = {
        "char_id": f"0x{char_id:02x}",
        "char_id_int": char_id,
        "char_slug": char_slug,
        "button": btn,
        "source_file": rel_clean,
        "lba_start": manifest_lookup.get(rel_clean, {}).get("lba_start"),
        "file_size_bytes": bin_path.stat().st_size,
        "palette_file": "/DATA/BASE/FIRST.GRC",
        "palette_lba_start": palette_lba_start,
        "palette_subblock_offset_hex": f"0x{pal_sub_offset:05x}",
        "base_palettes": {k: f"0x{v:04x}" for k, v in base_palettes.items()},
        "base_palettes_int": base_palettes,
        "total_slots": len(records),
        "decoded_frames": sum(1 for r in records if not r["is_empty_stub"]),
        "frames": records,
    }
    if epl_records:
        offsets_map["epl_frames"] = epl_records

    out_dir.mkdir(parents=True, exist_ok=True)
    map_path = out_dir / "offsets_map.json"
    map_path.write_text(
        json.dumps(offsets_map, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    non_empty = offsets_map["decoded_frames"]
    print(f"Character      : {char_slug} (ID 0x{char_id:02x}, btn={btn})")
    print(f"Source BIN     : {rel_clean} ({offsets_map['file_size_bytes']:,} bytes, LBA {offsets_map['lba_start']})")
    print(f"Palettes       : { {k: f'0x{v:04x}' for k, v in base_palettes.items()} }")
    print(f"Decoded Frames : {non_empty}/{len(records)} -> {frames_dir}")
    print(f"Contact Sheets : {len(sheets_written)} sheets + {abcd_path.name} -> {char_out_dir}")
    print(f"Offsets Map    : {map_path}")
    if epl_records:
        epl_non_empty = sum(1 for r in epl_records if not r["is_empty_stub"])
        print(f"EPL Subset     : {epl_non_empty}/{len(epl_records)} non-empty -> {char_out_dir / 'epl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

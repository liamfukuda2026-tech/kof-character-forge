#!/usr/bin/env python3
"""Build a modded KOF 2002 (PS2) disc image from edited 16-color indexed PNGs.

Reads sprites_dump/offsets_map.json and scripts/iso_manifest.json, validates
edited sprite PNGs in custom_character/, re-encodes them into KOF 2002 4bpp
column-major 16x16 tiles (flg=0x01 uncompressed or flg=0x03 LZ-compressed),
patches copies of the target .BIN (and optionally FIRST.GRC palettes) into
build/patched_files/, and writes a bootable Mode 2 / 2352 .bin + .cue
(or 2048B .iso) with recalculated ECMA-130 EDC and Reed-Solomon P/Q ECC.

Never modifies the original disc image or files inside extracted/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

LOGICAL_SECTOR_SIZE = 2048
RAW_SECTOR_SIZE = 2352
MODE2_SYNC = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"
FRAME_NAME_RE = re.compile(r"^frame_(\d+)(?:_.*)?\.png$", re.IGNORECASE)
CUE_FILE_RE = re.compile(r'^(\s*FILE\s+)"([^"]+)"(\s+.*)$', re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# ECMA-130 Mode 2 Form 1 EDC & Reed-Solomon P/Q ECC
# ---------------------------------------------------------------------------

def _build_edc_ecc_tables() -> tuple[list[int], list[int], list[int]]:
    edc_lut = [0] * 256
    for i in range(256):
        edc = i
        for _ in range(8):
            edc = (edc >> 1) ^ (0xD8018001 if (edc & 1) else 0)
        edc_lut[i] = edc

    ecc_f_lut = [((i << 1) ^ (0x11D if (i & 0x80) else 0)) & 0xFF for i in range(256)]
    ecc_b_lut = [0] * 256
    for i in range(256):
        ecc_b_lut[i ^ ecc_f_lut[i]] = i
    return edc_lut, ecc_f_lut, ecc_b_lut


_EDC_LUT, _ECC_F_LUT, _ECC_B_LUT = _build_edc_ecc_tables()


def compute_mode2_form1_edc(data_2056: bytes | bytearray | memoryview) -> bytes:
    """Compute 4-byte little-endian ECMA-130 EDC over sector[16:2072]."""
    edc = 0
    lut = _EDC_LUT
    for b in data_2056:
        edc = (edc >> 8) ^ lut[(edc ^ b) & 0xFF]
    return edc.to_bytes(4, "little")


def _compute_rs_pq(
    src: bytes | bytearray,
    major_count: int,
    minor_count: int,
    major_mult: int,
    minor_inc: int,
) -> bytes:
    f_lut = _ECC_F_LUT
    b_lut = _ECC_B_LUT
    size = major_count * minor_count
    out = bytearray(major_count * 2)
    for major in range(major_count):
        idx = (major >> 1) * major_mult + (major & 1)
        ecc_a = 0
        ecc_b = 0
        for _ in range(minor_count):
            temp = src[idx]
            idx += minor_inc
            if idx >= size:
                idx -= size
            ecc_a ^= temp
            ecc_b ^= temp
            ecc_a = f_lut[ecc_a]
        ecc_a = b_lut[f_lut[ecc_a] ^ ecc_b]
        out[major] = ecc_a
        out[major + major_count] = ecc_a ^ ecc_b
    return bytes(out)


def compute_mode2_form1_ecc(sector_2076: bytes | bytearray) -> tuple[bytes, bytes]:
    """Compute 172B P-parity and 104B Q-parity for a Mode 2 Form 1 sector."""
    # Mode 2 Form 1 zeros the 4-byte MSF+Mode header [12:16] during ECC calculation.
    src = bytearray(2064)
    src[4:] = sector_2076[16:2076]
    p_ecc = _compute_rs_pq(src, 86, 24, 2, 86)
    q_ecc = _compute_rs_pq(bytes(src) + p_ecc, 52, 43, 86, 88)
    return p_ecc, q_ecc


def rebuild_mode2_form1_sector(orig_sector: bytes, user_data_2048: bytes) -> bytes:
    """Rebuild a 2352-byte Mode 2 Form 1 sector preserving sync, header, subheader."""
    if len(orig_sector) != RAW_SECTOR_SIZE:
        raise ValueError(f"Expected {RAW_SECTOR_SIZE}-byte sector, got {len(orig_sector)}")
    if len(user_data_2048) != LOGICAL_SECTOR_SIZE:
        raise ValueError(f"Expected {LOGICAL_SECTOR_SIZE}-byte user data, got {len(user_data_2048)}")
    if orig_sector[:12] != MODE2_SYNC:
        raise ValueError(f"Invalid CD-ROM sync pattern in sector: {orig_sector[:12].hex()}")
    if orig_sector[15] != 0x02:
        raise ValueError(f"Expected Mode 2 sector (0x02), got 0x{orig_sector[15]:02x}")
    if (orig_sector[18] & 0x20) != 0:
        raise ValueError(
            f"Sector subheader indicates Mode 2 Form 2 (submode=0x{orig_sector[18]:02x}); "
            "only Mode 2 Form 1 is supported."
        )

    sec = bytearray(RAW_SECTOR_SIZE)
    sec[:24] = orig_sector[:24]
    sec[24:2072] = user_data_2048
    sec[2072:2076] = compute_mode2_form1_edc(memoryview(sec)[16:2072])
    p_ecc, q_ecc = compute_mode2_form1_ecc(sec[:2076])
    sec[2076:2248] = p_ecc
    sec[2248:2352] = q_ecc
    return bytes(sec)


def verify_mode2_form1_sector(sector: bytes) -> tuple[bool, bool, bool]:
    """Return (edc_ok, p_ok, q_ok) for a 2352-byte Mode 2 Form 1 sector."""
    if len(sector) != RAW_SECTOR_SIZE or sector[:12] != MODE2_SYNC or sector[15] != 0x02:
        return False, False, False
    if (sector[18] & 0x20) != 0:
        return False, False, False
    edc_ok = compute_mode2_form1_edc(memoryview(sector)[16:2072]) == sector[2072:2076]
    p_ecc, q_ecc = compute_mode2_form1_ecc(sector[:2076])
    return edc_ok, p_ecc == sector[2076:2248], q_ecc == sector[2248:2352]


# ---------------------------------------------------------------------------
# KOF 2002 LZ Compression & Decompression
# ---------------------------------------------------------------------------

def lz_decompress(src: bytes | bytearray | memoryview) -> bytes:
    """Decompress KOF 2002 byte-oriented LZ stream (0xC0 terminator)."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        b = src[i]
        i += 1
        if b < 0xC0:
            out.append(b)
        elif b == 0xC0:
            break
        else:
            if i >= n:
                raise ValueError("Truncated LZ stream after control byte")
            arg = src[i]
            i += 1
            length = b & 0x3F
            if length == 1:
                out.append(arg)
            else:
                if arg == 0 or arg > len(out):
                    raise ValueError(f"Invalid LZ back-reference arg={arg} at out_len={len(out)}")
                for _ in range(length):
                    out.append(out[-arg])
    return bytes(out)


def lz_compress(data: bytes) -> bytes:
    """Compress bytes into KOF 2002 LZ stream (b < 0xC0, l==1 escape, l in 3..63, d in 3..255).

    Uses lazy 1-step lookahead matching via C-backed bytes.rfind().
    """
    n = len(data)
    out = bytearray()
    i = 0

    def find_best(pos: int) -> tuple[int, int]:
        if pos < 3 or n - pos < 3:
            return 0, 0
        min_start = max(0, pos - 255)
        max_len = min(63, n - pos)
        pat = data[pos : pos + 3]
        best_l = 0
        best_d = 0
        search_end = pos
        while search_end >= min_start + 3:
            idx = data.rfind(pat, min_start, search_end)
            if idx < 0:
                break
            d = pos - idx
            if d >= 3:
                length = 3
                while length < max_len and data[pos + length] == data[pos + length - d]:
                    length += 1
                if length > best_l:
                    best_l = length
                    best_d = d
                    if best_l == max_len:
                        return best_l, best_d
            search_end = idx + 2
        return best_l, best_d

    def emit_literal(byte_val: int) -> None:
        if byte_val < 0xC0:
            out.append(byte_val)
        else:
            out.append(0xC1)
            out.append(byte_val)

    while i < n:
        length, dist = find_best(i)
        if length >= 3:
            lit_cost = 2 if data[i] >= 0xC0 else 1
            next_len, _ = find_best(i + 1)
            if next_len > length + lit_cost:
                emit_literal(data[i])
                i += 1
                continue
            out.append(0xC0 | length)
            out.append(dist)
            i += length
        else:
            emit_literal(data[i])
            i += 1

    out.append(0xC0)
    return bytes(out)


# ---------------------------------------------------------------------------
# Sprite PNG Validation & 4bpp Column-Major Tile Encoding
# ---------------------------------------------------------------------------

def load_and_validate_png(png_path: Path, expected_w: int, expected_h: int) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """Validate an edited sprite PNG and return (indexed_pixels, palette_rgb_16)."""
    try:
        with Image.open(png_path) as img:
            if img.format != "PNG":
                raise ValueError(
                    f"{png_path}: expected PNG format, got '{img.format}'. Save as a PNG file."
                )
            if img.mode != "P":
                raise ValueError(
                    f"{png_path}: invalid color mode '{img.mode}'. "
                    "Must be Indexed Color (mode='P', 16 colors). "
                    "Convert to Indexed 16-color PNG in LibreSprite/Aseprite/GIMP before building."
                )
            if img.size != (expected_w, expected_h):
                raise ValueError(
                    f"{png_path}: dimension mismatch — got {img.size[0]}x{img.size[1]}, "
                    f"expected {expected_w}x{expected_h} px."
                )
            arr = np.array(img, dtype=np.uint8)
            max_idx = int(arr.max()) if arr.size else 0
            if max_idx > 15:
                used = sorted(int(v) for v in np.unique(arr) if v > 15)
                raise ValueError(
                    f"{png_path}: uses palette indices > 15 ({used[:8]}{'...' if len(used) > 8 else ''}). "
                    "KOF 2002 sprites support at most 16 colors (indices 0..15, where 0 is transparent)."
                )

            raw_pal = img.getpalette() or []
            pal_rgb: list[tuple[int, int, int]] = []
            for idx in range(16):
                base = idx * 3
                if base + 2 < len(raw_pal):
                    pal_rgb.append((int(raw_pal[base]), int(raw_pal[base + 1]), int(raw_pal[base + 2])))
                else:
                    pal_rgb.append((0, 0, 0))
    except OSError as exc:
        raise ValueError(f"{png_path}: failed to read PNG ({exc})") from exc

    return arr, pal_rgb


def encode_sprite_block(
    pixels: np.ndarray,
    w_tiles: int,
    h_tiles: int,
    pal_slot: int,
    target_flg: int,
    orig_block_size: int | None = None,
) -> tuple[bytes, int]:
    """Encode 4bpp indexed pixels into a KOF 2002 sprite block (header + payload).

    Returns (block_bytes, actual_flg_used).
    """
    if target_flg not in (0x01, 0x03):
        raise ValueError(f"Unsupported sprite block flag 0x{target_flg:02x} (only 0x01 and 0x03 supported)")

    bpc = (h_tiles + 7) // 8
    hdr_sz = ((8 + w_tiles * bpc + 7) // 8) * 8

    mask_bytes = bytearray(w_tiles * bpc)
    tiles_128: list[bytes] = []
    tiles_512: list[bytes] = []

    top_bit = 1 << (8 * bpc - 1)
    for col in range(w_tiles):
        col_mask = 0
        x0 = col * 16
        for row in range(h_tiles):
            y0 = row * 16
            tile = pixels[y0 : y0 + 16, x0 : x0 + 16]
            if not np.any(tile):
                continue
            col_mask |= top_bit >> row

            # Pack 16x16 4bpp low-nibble-first: pixel x0 in low nibble, x1 in high nibble.
            packed = ((tile[:, 0::2] & 0x0F) | ((tile[:, 1::2] & 0x0F) << 4)).astype(np.uint8)
            tile_128 = packed.tobytes()
            tiles_128.append(tile_128)
            if target_flg == 0x03:
                buf512 = bytearray(512)
                for y in range(16):
                    buf512[y * 16 : y * 16 + 8] = tile_128[y * 8 : (y + 1) * 8]
                tiles_512.append(bytes(buf512))

        mask_bytes[col * bpc : (col + 1) * bpc] = col_mask.to_bytes(bpc, "little")

    actual_flg = target_flg
    if target_flg == 0x01:
        payload = b"".join(tiles_128)
    else:
        raw_512 = b"".join(tiles_512)
        payload = lz_compress(raw_512)
        # Optional fallback to uncompressed flg=0x01 if LZ exceeds original slot and 0x01 fits.
        if orig_block_size is not None and (hdr_sz + len(payload)) > orig_block_size:
            raw_128 = b"".join(tiles_128)
            if (hdr_sz + len(raw_128)) <= orig_block_size:
                actual_flg = 0x01
                payload = raw_128

    header = bytearray(hdr_sz)
    struct.pack_into("<IBBBB", header, 0, hdr_sz, pal_slot, w_tiles, h_tiles, actual_flg)
    header[8 : 8 + len(mask_bytes)] = mask_bytes
    for i in range(8 + len(mask_bytes), hdr_sz):
        header[i] = 0xCD

    return bytes(header) + payload, actual_flg


# ---------------------------------------------------------------------------
# PXX.BIN Patching & Repacking
# ---------------------------------------------------------------------------

def patch_pxx_bin(
    orig_bin: bytes,
    edited_frames: dict[int, tuple[np.ndarray, dict[str, Any]]],
    source_label: str,
) -> tuple[bytes, dict[str, Any]]:
    """Patch edited sprite frames into a PXX.BIN container (in-place or repacked)."""
    if len(orig_bin) < 4:
        raise ValueError(f"{source_label}: file too small ({len(orig_bin)} bytes)")

    first_off = struct.unpack_from("<I", orig_bin, 0)[0] & 0x7FFFFFFF
    if first_off < 4 or first_off % 4 != 0 or first_off > len(orig_bin):
        raise ValueError(f"{source_label}: invalid offset table header 0x{first_off:x}")

    num_slots = first_off // 4
    orig_offs = list(struct.unpack_from(f"<{num_slots}I", orig_bin, 0))

    non_stub_offs = sorted({o for o in orig_offs if (o & 0x80000000) == 0})
    off_to_span: dict[int, int] = {}
    for idx, s in enumerate(non_stub_offs):
        e = non_stub_offs[idx + 1] if idx + 1 < len(non_stub_offs) else len(orig_bin)
        off_to_span[s] = e - s

    # Map each frame slot to a canonical block key. Shared offsets stay shared unless
    # two aliased slots are both explicitly edited with different pixel data.
    slot_to_key: dict[int, Any] = {}
    key_bytes: dict[Any, bytes] = {}
    key_orig_off: dict[Any, int] = {}
    edited_by_orig_off: dict[int, list[tuple[int, bytes]]] = {}

    for off in non_stub_offs:
        span = off_to_span[off]
        key_bytes[off] = orig_bin[off : off + span]
        key_orig_off[off] = off

    for slot, o in enumerate(orig_offs):
        if (o & 0x80000000) == 0:
            slot_to_key[slot] = o

    flags_used: dict[int, int] = {}
    for frame_idx, (pixels, meta) in sorted(edited_frames.items()):
        if frame_idx < 0 or frame_idx >= num_slots:
            raise ValueError(f"{source_label}: frame_index {frame_idx} out of range (0..{num_slots - 1})")
        orig_o = orig_offs[frame_idx]
        if orig_o & 0x80000000:
            raise ValueError(f"{source_label}: frame {frame_idx} is an empty stub (0x{orig_o:08x}) and cannot be edited")

        orig_span = off_to_span[orig_o]
        orig_hdr_sz, orig_pal_slot, orig_w, orig_h, orig_flg = struct.unpack_from("<IBBBB", orig_bin, orig_o)
        if orig_flg == 0xFF:
            raise ValueError(f"{source_label}: frame {frame_idx} has flg=0xff empty stub header")

        w_tiles = int(meta.get("w_tiles", orig_w))
        h_tiles = int(meta.get("h_tiles", orig_h))
        pal_slot = int(meta.get("pal_slot", orig_pal_slot))
        target_flg = int(meta.get("flg", orig_flg))

        encoded_blk, actual_flg = encode_sprite_block(
            pixels=pixels,
            w_tiles=w_tiles,
            h_tiles=h_tiles,
            pal_slot=pal_slot,
            target_flg=target_flg,
            orig_block_size=orig_span,
        )
        flags_used[frame_idx] = actual_flg
        edited_by_orig_off.setdefault(orig_o, []).append((frame_idx, encoded_blk))

    split_required = False
    for orig_o, edits in edited_by_orig_off.items():
        first_blk = edits[0][1]
        if all(blk == first_blk for _, blk in edits):
            key_bytes[orig_o] = first_blk
        else:
            split_required = True
            for frame_idx, blk in edits:
                new_key = ("split", frame_idx)
                slot_to_key[frame_idx] = new_key
                key_bytes[new_key] = blk

    fits_in_place = not split_required
    if fits_in_place:
        for orig_o in edited_by_orig_off:
            if len(key_bytes[orig_o]) > off_to_span[orig_o]:
                fits_in_place = False
                break

    if fits_in_place:
        out = bytearray(orig_bin)
        for orig_o in edited_by_orig_off:
            span = off_to_span[orig_o]
            blk = key_bytes[orig_o]
            out[orig_o : orig_o + len(blk)] = blk
            if len(blk) < span:
                out[orig_o + len(blk) : orig_o + span] = b"\x00" * (span - len(blk))
        return bytes(out), {
            "mode": "in_place",
            "orig_size": len(orig_bin),
            "used_size": len(orig_bin),
            "edited_count": len(edited_frames),
            "flags_used": flags_used,
        }

    # Repack mode: rebuild offset table + deduplicated 8-byte-aligned blocks.
    data_start = max(num_slots * 4, non_stub_offs[0] if non_stub_offs else num_slots * 4)
    data_start = ((data_start + 7) // 8) * 8

    # Preserve canonical ordering of original blocks, appending any split blocks at the end.
    ordered_keys: list[Any] = list(non_stub_offs)
    for k in key_bytes:
        if k not in key_orig_off:
            ordered_keys.append(k)

    def _aligned_len(blk: bytes) -> int:
        return ((len(blk) + 7) // 8) * 8

    total_used = data_start + sum(_aligned_len(key_bytes[k]) for k in ordered_keys)

    # P27.BIN has 0 trailing slack in the original image; if user edits grow past
    # orig_size, incrementally re-LZ unedited flg=0x03 blocks (largest first) until it fits.
    if total_used > len(orig_bin):
        unedited_lz_candidates = sorted(
            (
                off
                for off in non_stub_offs
                if off not in edited_by_orig_off and len(key_bytes[off]) >= 16 and key_bytes[off][7] == 0x03
            ),
            key=lambda off: len(key_bytes[off]),
            reverse=True,
        )
        for off in unedited_lz_candidates:
            if total_used <= len(orig_bin):
                break
            blk = key_bytes[off]
            hdr_sz = struct.unpack_from("<I", blk, 0)[0]
            dec = lz_decompress(memoryview(blk)[hdr_sz:])
            re_lz = lz_compress(dec)
            new_blk = blk[:hdr_sz] + re_lz
            old_alen = _aligned_len(blk)
            new_alen = _aligned_len(new_blk)
            if new_alen < old_alen:
                key_bytes[off] = new_blk
                total_used -= old_alen - new_alen

    if total_used > len(orig_bin):
        raise ValueError(
            f"{source_label}: repacked size ({total_used:,} bytes) exceeds original file size "
            f"({len(orig_bin):,} bytes) by {total_used - len(orig_bin):,} bytes! "
            "Simplify sprite details or reduce edited frame count so LZ output fits."
        )

    out_body = bytearray()
    key_new_off: dict[Any, int] = {}
    for key in ordered_keys:
        blk = key_bytes[key]
        cur_off = data_start + len(out_body)
        key_new_off[key] = cur_off
        out_body.extend(blk)
        rem = len(out_body) % 8
        if rem:
            out_body.extend(b"\xcd" * (8 - rem))

    out = bytearray(len(orig_bin))
    for slot, o in enumerate(orig_offs):
        new_o = o if (o & 0x80000000) else key_new_off[slot_to_key[slot]]
        struct.pack_into("<I", out, slot * 4, new_o)
    for idx in range(num_slots * 4, data_start):
        out[idx] = 0xCD
    out[data_start : data_start + len(out_body)] = out_body
    # Remainder up to len(orig_bin) stays 0x00 padded.

    return bytes(out), {
        "mode": "repacked",
        "orig_size": len(orig_bin),
        "used_size": total_used,
        "slack_bytes": len(orig_bin) - total_used,
        "edited_count": len(edited_frames),
        "flags_used": flags_used,
    }


# ---------------------------------------------------------------------------
# FIRST.GRC Palette Patching (--update-palette)
# ---------------------------------------------------------------------------

def _rgb8_to_rgb555_word(r: int, g: int, b: int) -> int:
    r5 = (r * 31 + 127) // 255
    g5 = (g * 31 + 127) // 255
    b5 = (b * 31 + 127) // 255
    return 0x8000 | (r5 << 10) | (g5 << 5) | b5


def _orig_palette_words(frame_meta: dict[str, Any]) -> list[int] | None:
    for key in ("palette_words", "palette_raw_words", "palette_uint16"):
        if key in frame_meta and isinstance(frame_meta[key], list) and len(frame_meta[key]) == 16:
            return [int(x) for x in frame_meta[key]]
    rgba = frame_meta.get("palette_rgba")
    if isinstance(rgba, list) and len(rgba) == 16:
        words = [0]
        for idx in range(1, 16):
            c = rgba[idx]
            words.append(_rgb8_to_rgb555_word(int(c[0]), int(c[1]), int(c[2])))
        return words
    return None


def patch_first_grc_palettes(
    orig_grc: bytes,
    palette_updates: dict[int, list[int]],
    grc_label: str,
) -> tuple[bytes, dict[str, Any]]:
    """Patch modified 15-color RGB555 palettes into FIRST.GRC sub-block [1] and re-LZ."""
    if len(orig_grc) < 8:
        raise ValueError(f"{grc_label}: invalid FIRST.GRC size ({len(orig_grc)})")
    hdr_val = struct.unpack_from("<I", orig_grc, 0)[0]
    if hdr_val != 0x00000100:
        raise ValueError(f"{grc_label}: unexpected header 0x{hdr_val:08x}, expected 0x00000100")

    # FIRST.GRC[:4] (0x00000100) is the uncompressed first entry of the 64-entry offset table.
    buf = bytearray(orig_grc[:4])
    buf.extend(lz_decompress(memoryview(orig_grc)[4:]))
    sub_offs = struct.unpack_from("<64I", buf, 0)
    sub1_off = sub_offs[1]
    sub1_end = sub_offs[2]
    num_palettes = (sub1_end - sub1_off) // 32

    for pal_idx, words_1_to_15 in sorted(palette_updates.items()):
        if pal_idx < 0 or pal_idx >= num_palettes:
            raise ValueError(f"{grc_label}: palette_index {pal_idx} out of range (0..{num_palettes - 1})")
        if len(words_1_to_15) != 15:
            raise ValueError(f"{grc_label}: expected 15 color words (indices 1..15), got {len(words_1_to_15)}")
        pal_off = sub1_off + pal_idx * 32
        # Preserve word 0 (sequential palette ID word) and update colors 1..15.
        struct.pack_into("<15H", buf, pal_off + 2, *words_1_to_15)

    recompressed = lz_compress(bytes(buf[4:]))
    new_len = 4 + len(recompressed)
    if new_len > len(orig_grc):
        raise ValueError(
            f"{grc_label}: recompressed FIRST.GRC ({new_len:,} bytes) exceeds original size "
            f"({len(orig_grc):,} bytes) by {new_len - len(orig_grc):,} bytes."
        )

    out = bytearray(len(orig_grc))
    out[:4] = buf[:4]
    out[4 : 4 + len(recompressed)] = recompressed
    return bytes(out), {
        "orig_size": len(orig_grc),
        "compressed_size": new_len,
        "slack_bytes": len(orig_grc) - new_len,
        "palettes_updated": sorted(palette_updates.keys()),
    }


# ---------------------------------------------------------------------------
# Offsets Map & Custom PNG Discovery
# ---------------------------------------------------------------------------

def load_offsets_map(offsets_map_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load sprites_dump/offsets_map.json regardless of list vs dict wrapper layout."""
    raw = json.loads(offsets_map_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {}, [dict(item) for item in raw]
    if isinstance(raw, dict):
        if "frames" in raw and isinstance(raw["frames"], list):
            return raw, [dict(item) for item in raw["frames"]]
        # Fallback: dict keyed by frame index string
        frames = []
        for k, v in raw.items():
            if isinstance(v, dict) and ("frame_index" in v or k.isdigit()):
                item = dict(v)
                item.setdefault("frame_index", int(k) if k.isdigit() else item.get("frame_index"))
                frames.append(item)
        if frames:
            return raw, frames
    raise ValueError(f"Unrecognized offsets_map.json schema in {offsets_map_path}")


def discover_custom_pngs(
    custom_dir: Path,
    frames: list[dict[str, Any]],
) -> list[tuple[Path, dict[str, Any]]]:
    """Scan custom_dir recursively for edited PNGs and match each to a frame entry."""
    if not custom_dir.exists():
        raise FileNotFoundError(f"Custom character directory does not exist: {custom_dir}")

    by_idx: dict[int, dict[str, Any]] = {}
    by_basename: dict[str, dict[str, Any]] = {}
    for entry in frames:
        fidx = int(entry["frame_index"])
        by_idx[fidx] = entry
        png_rel = entry.get("png_path")
        if png_rel:
            by_basename[Path(str(png_rel)).name.lower()] = entry

    all_pngs = sorted(custom_dir.rglob("*.png"))
    matched: list[tuple[Path, dict[str, Any]]] = []
    seen_frames: dict[int, Path] = {}

    for png_path in all_pngs:
        name_lower = png_path.name.lower()
        if name_lower.startswith("sheet_") or name_lower.startswith("palettes_"):
            continue

        target: dict[str, Any] | None = None
        if name_lower in by_basename:
            target = by_basename[name_lower]
        else:
            m = FRAME_NAME_RE.match(png_path.name)
            if m:
                fidx = int(m.group(1))
                if fidx not in by_idx:
                    raise ValueError(
                        f"{png_path}: frame index {fidx} does not exist in offsets_map.json "
                        f"(valid range 0..{max(by_idx) if by_idx else 0})."
                    )
                target = by_idx[fidx]

        if target is None:
            raise ValueError(
                f"{png_path}: filename does not match any frame in offsets_map.json. "
                "Name files as frame_0000.png or frame_0000_<label>.png."
            )

        fidx = int(target["frame_index"])
        if fidx in seen_frames:
            raise ValueError(
                f"Duplicate edited PNGs for frame {fidx}: {seen_frames[fidx]} and {png_path}"
            )
        seen_frames[fidx] = png_path
        matched.append((png_path, target))

    return matched


# ---------------------------------------------------------------------------
# Manifest & Disc Sector Patching / Verification
# ---------------------------------------------------------------------------

def build_manifest_lookup(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for bucket in ("files", "containers"):
        for entry in manifest.get(bucket, []):
            clean = str(entry.get("clean_path", "")).strip()
            if clean:
                norm = "/" + clean.lstrip("/").upper()
                lookup[norm] = entry
    return lookup


def norm_iso_path(path_str: str) -> str:
    return "/" + path_str.replace("\\", "/").lstrip("/").upper()


def copy_file_chunked(src: Path, dst: Path, chunk_size: int = 4 * 1024 * 1024) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            fout.write(chunk)


def write_cue_for_bin(src_cue: Path | None, dst_cue: Path, bin_filename: str) -> None:
    dst_cue.parent.mkdir(parents=True, exist_ok=True)
    if src_cue and src_cue.exists():
        text = src_cue.read_text(encoding="utf-8", errors="replace")
        new_text, count = CUE_FILE_RE.subn(rf'\1"{bin_filename}"\3', text, count=1)
        if count == 0:
            new_text = f'FILE "{bin_filename}" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n'
    else:
        new_text = f'FILE "{bin_filename}" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n'
    dst_cue.write_text(new_text, encoding="utf-8")


def patch_disc_image(
    disc_path: Path,
    sector_format: str,
    patches: list[tuple[dict[str, Any], bytes]],
) -> None:
    """Patch only the specified manifest LBA ranges in disc_path."""
    is_mode2_2352 = sector_format == "bin2352_mode2"
    if not is_mode2_2352 and sector_format != "iso2048":
        raise ValueError(f"Unsupported sector_format '{sector_format}' in manifest")

    with disc_path.open("r+b") as fp:
        for entry, payload in patches:
            lba_start = int(entry["lba_start"])
            lba_sectors = int(entry["lba_sectors"])
            expected_size = int(entry["size_bytes"])
            if len(payload) != expected_size:
                raise ValueError(
                    f"{entry['clean_path']}: payload size {len(payload)} != manifest size {expected_size}"
                )

            padded = payload.ljust(lba_sectors * LOGICAL_SECTOR_SIZE, b"\x00")
            for sec_i in range(lba_sectors):
                lba = lba_start + sec_i
                user_2048 = padded[sec_i * LOGICAL_SECTOR_SIZE : (sec_i + 1) * LOGICAL_SECTOR_SIZE]
                if is_mode2_2352:
                    phys_off = lba * RAW_SECTOR_SIZE
                    fp.seek(phys_off)
                    orig_sec = fp.read(RAW_SECTOR_SIZE)
                    new_sec = rebuild_mode2_form1_sector(orig_sec, user_2048)
                    fp.seek(phys_off)
                    fp.write(new_sec)
                else:
                    fp.seek(lba * LOGICAL_SECTOR_SIZE)
                    fp.write(user_2048)


def verify_disc_image(
    disc_path: Path,
    sector_format: str,
    expected_files: list[tuple[dict[str, Any], Path]],
    orig_disc_path: Path | None = None,
) -> dict[str, Any]:
    """Verify patched LBA sectors pass EDC + P/Q ECC and match patched files byte-for-byte."""
    if not disc_path.exists():
        raise FileNotFoundError(f"Output disc image not found: {disc_path}")

    is_mode2_2352 = sector_format == "bin2352_mode2"
    phys_size = RAW_SECTOR_SIZE if is_mode2_2352 else LOGICAL_SECTOR_SIZE
    patched_ranges: list[tuple[int, int]] = []
    verified_files: list[dict[str, Any]] = []

    with disc_path.open("rb") as fp:
        for entry, patched_file_path in expected_files:
            lba_start = int(entry["lba_start"])
            lba_sectors = int(entry["lba_sectors"])
            size_bytes = int(entry["size_bytes"])
            expected_bytes = patched_file_path.read_bytes()
            if len(expected_bytes) != size_bytes:
                raise ValueError(
                    f"Patched file {patched_file_path} size ({len(expected_bytes)}) != manifest ({size_bytes})"
                )

            extracted = bytearray()
            edc_failures = 0
            p_failures = 0
            q_failures = 0

            fp.seek(lba_start * phys_size)
            for _ in range(lba_sectors):
                sec = fp.read(phys_size)
                if len(sec) != phys_size:
                    raise ValueError(f"Unexpected EOF reading LBA sector in {disc_path}")
                if is_mode2_2352:
                    edc_ok, p_ok, q_ok = verify_mode2_form1_sector(sec)
                    if not edc_ok:
                        edc_failures += 1
                    if not p_ok:
                        p_failures += 1
                    if not q_ok:
                        q_failures += 1
                    extracted.extend(sec[24:2072])
                else:
                    extracted.extend(sec)

            if edc_failures or p_failures or q_failures:
                raise ValueError(
                    f"Sector verification failed for {entry['clean_path']} "
                    f"(LBA {lba_start}..{lba_start + lba_sectors - 1}): "
                    f"edc_failures={edc_failures}, p_failures={p_failures}, q_failures={q_failures}"
                )

            if bytes(extracted[:size_bytes]) != expected_bytes:
                raise ValueError(
                    f"Byte-for-byte mismatch between {disc_path} LBA {lba_start}..{lba_start + lba_sectors - 1} "
                    f"and {patched_file_path}"
                )

            patched_ranges.append((lba_start, lba_start + lba_sectors))
            verified_files.append(
                {
                    "clean_path": entry["clean_path"],
                    "lba_start": lba_start,
                    "lba_sectors": lba_sectors,
                    "size_bytes": size_bytes,
                    "edc_ecc_ok": True if is_mode2_2352 else "N/A (iso2048)",
                    "byte_exact": True,
                }
            )

    untouched_ok: bool | None = None
    if orig_disc_path and orig_disc_path.exists() and orig_disc_path.resolve() != disc_path.resolve():
        orig_sz = orig_disc_path.stat().st_size
        out_sz = disc_path.stat().st_size
        if orig_sz != out_sz:
            raise ValueError(f"Disc size mismatch: orig={orig_sz}, mod={out_sz}")

        # Verify byte equality outside patched sector byte offsets in 4 MB chunks.
        patched_byte_ranges = sorted((s * phys_size, e * phys_size) for s, e in patched_ranges)
        chunk_size = 4 * 1024 * 1024
        with orig_disc_path.open("rb") as f_orig, disc_path.open("rb") as f_mod:
            offset = 0
            while offset < orig_sz:
                b_orig = bytearray(f_orig.read(chunk_size))
                b_mod = bytearray(f_mod.read(chunk_size))
                end_off = offset + len(b_orig)
                for ps, pe in patched_byte_ranges:
                    ov_s = max(offset, ps)
                    ov_e = min(end_off, pe)
                    if ov_s < ov_e:
                        b_mod[ov_s - offset : ov_e - offset] = b_orig[ov_s - offset : ov_e - offset]
                if b_orig != b_mod:
                    raise ValueError(f"Unpatched disc bytes altered in range {offset}..{end_off}!")
                offset = end_off
        untouched_ok = True

    return {
        "disc_path": str(disc_path),
        "sector_format": sector_format,
        "verified_files": verified_files,
        "untouched_sectors_identical": untouched_ok,
    }


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def detect_sector_format(disc_path: Path, manifest: dict[str, Any]) -> str:
    size = disc_path.stat().st_size
    if size % RAW_SECTOR_SIZE == 0:
        with disc_path.open("rb") as fp:
            hdr = fp.read(16)
        if hdr[:12] == MODE2_SYNC and hdr[15] == 0x02:
            return "bin2352_mode2"
    if size % LOGICAL_SECTOR_SIZE == 0:
        with disc_path.open("rb") as fp:
            fp.seek(16 * LOGICAL_SECTOR_SIZE)
            if fp.read(6) == b"\x01CD001":
                return "iso2048"
    return str(manifest.get("sector_format", "bin2352_mode2"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build a modded KOF 2002 PS2 disc image from custom indexed PNG sprites."
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help="Path to original .bin or .iso disc image (default: from iso_manifest.json)",
    )
    parser.add_argument(
        "--cue",
        type=Path,
        default=None,
        help="Path to original .cue file (default: matching .cue alongside --iso)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "scripts" / "iso_manifest.json",
        help="Path to scripts/iso_manifest.json",
    )
    parser.add_argument(
        "--offsets-map",
        type=Path,
        default=root / "sprites_dump" / "offsets_map.json",
        help="Path to sprites_dump/offsets_map.json",
    )
    parser.add_argument(
        "--custom-dir",
        type=Path,
        default=root / "custom_character",
        help="Directory containing edited PNG frames (default: custom_character/)",
    )
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=root / "extracted",
        help="Read-only extracted disc files directory (default: extracted/)",
    )
    parser.add_argument(
        "--patched-dir",
        type=Path,
        default=root / "build" / "patched_files",
        help="Output directory for patched .BIN/.GRC files (default: build/patched_files/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output modded disc path (default: build/kof2002_mod.bin + .cue, or .iso for 2048B ISO)",
    )
    parser.add_argument(
        "--update-palette",
        action="store_true",
        help="If edited PNG palette colors 1..15 differ from offsets_map.json, update FIRST.GRC too",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing --output disc image against build/patched_files/ and EDC/ECC",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent

    if not args.manifest.exists():
        print(f"[ERROR] Manifest not found: {args.manifest}", file=sys.stderr)
        return 1

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_lookup = build_manifest_lookup(manifest)

    orig_disc = args.iso or Path(manifest["image_path"])
    if not orig_disc.exists():
        print(f"[ERROR] Original disc image not found: {orig_disc}", file=sys.stderr)
        return 1

    sector_format = detect_sector_format(orig_disc, manifest)
    if args.output is not None:
        out_disc = args.output
    else:
        ext = ".bin" if sector_format == "bin2352_mode2" else ".iso"
        out_disc = root / "build" / f"kof2002_mod{ext}"

    # Safety guard: NEVER overwrite the original disc image or extracted/
    if out_disc.resolve() == orig_disc.resolve():
        print("[ERROR] Refusing to overwrite the original disc image!", file=sys.stderr)
        return 1
    if args.extracted_dir.resolve() in args.patched_dir.resolve().parents or (
        args.patched_dir.resolve() == args.extracted_dir.resolve()
    ):
        print("[ERROR] Refusing to write patched files inside extracted/!", file=sys.stderr)
        return 1

    if args.verify_only:
        if not args.patched_dir.exists():
            print(f"[ERROR] Patched files directory not found: {args.patched_dir}", file=sys.stderr)
            return 1
        expected_files: list[tuple[dict[str, Any], Path]] = []
        for p in sorted(args.patched_dir.rglob("*")):
            if p.is_file():
                rel = "/" + p.relative_to(args.patched_dir).as_posix().upper()
                if rel not in manifest_lookup:
                    print(f"[ERROR] Patched file {p} ({rel}) not found in iso_manifest.json", file=sys.stderr)
                    return 1
                expected_files.append((manifest_lookup[rel], p))
        if not expected_files:
            print(f"[ERROR] No patched files found in {args.patched_dir} to verify.", file=sys.stderr)
            return 1
        report = verify_disc_image(out_disc, sector_format, expected_files, orig_disc_path=orig_disc)
        print(json.dumps(report, indent=2))
        return 0

    if not args.offsets_map.exists():
        print(
            f"[ERROR] Offsets map not found: {args.offsets_map}. Run scripts/02_decode_sprites.py first.",
            file=sys.stderr,
        )
        return 1

    map_meta, frames = load_offsets_map(args.offsets_map)
    matched = discover_custom_pngs(args.custom_dir, frames)
    if not matched:
        print(
            f"[ERROR] No edited sprite PNGs found in {args.custom_dir}. "
            "Copy edited 16-color indexed frame_XXXX.png files there and retry.",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(matched)} edited PNG(s) in {args.custom_dir}")

    # Group edited frames by target ISO source file (e.g. /DATA/PL1/P27.BIN)
    edits_by_source: dict[str, dict[int, tuple[np.ndarray, dict[str, Any]]]] = {}
    palette_updates_by_file: dict[str, dict[int, tuple[list[int], Path]]] = {}

    for png_path, meta in matched:
        expected_w = int(meta["width_px"])
        expected_h = int(meta["height_px"])
        pixels, pal_rgb = load_and_validate_png(png_path, expected_w, expected_h)

        src_file = norm_iso_path(str(meta.get("source_file") or map_meta.get("source_file") or "/DATA/PL1/P27.BIN"))
        frame_idx = int(meta["frame_index"])
        edits_by_source.setdefault(src_file, {})[frame_idx] = (pixels, meta)

        if args.update_palette:
            orig_words = _orig_palette_words(meta)
            if orig_words is None:
                raise ValueError(f"Frame {frame_idx}: missing palette_rgba/palette_words in offsets_map.json")
            new_words = [_rgb8_to_rgb555_word(*pal_rgb[i]) for i in range(1, 16)]
            if new_words != orig_words[1:16]:
                pal_file = norm_iso_path(str(meta.get("palette_file") or "/DATA/BASE/FIRST.GRC"))
                pal_idx = int(meta["palette_index"])
                f_map = palette_updates_by_file.setdefault(pal_file, {})
                if pal_idx in f_map and f_map[pal_idx][0] != new_words:
                    raise ValueError(
                        f"Conflicting palette edits for palette_index={pal_idx}: "
                        f"{f_map[pal_idx][1]} vs {png_path}"
                    )
                f_map[pal_idx] = (new_words, png_path)

    patches_to_apply: list[tuple[dict[str, Any], bytes]] = []
    verified_inputs: list[tuple[dict[str, Any], Path]] = []

    if args.patched_dir.exists():
        shutil.rmtree(args.patched_dir)
    args.patched_dir.mkdir(parents=True, exist_ok=True)

    # Patch each modified PXX.BIN
    for iso_file, frame_edits in sorted(edits_by_source.items()):
        if iso_file not in manifest_lookup:
            raise ValueError(f"Source file '{iso_file}' not found in {args.manifest}")
        m_entry = manifest_lookup[iso_file]
        src_extracted = args.extracted_dir / iso_file.lstrip("/")
        if not src_extracted.exists():
            raise FileNotFoundError(f"Extracted source file not found: {src_extracted}")

        orig_bytes = src_extracted.read_bytes()
        patched_bytes, bin_stats = patch_pxx_bin(orig_bytes, frame_edits, iso_file)

        out_patched = args.patched_dir / iso_file.lstrip("/")
        out_patched.parent.mkdir(parents=True, exist_ok=True)
        out_patched.write_bytes(patched_bytes)
        print(
            f"Patched {iso_file}: mode={bin_stats['mode']}, edited={bin_stats['edited_count']}, "
            f"used={bin_stats['used_size']:,}/{bin_stats['orig_size']:,} B -> {out_patched}"
        )

        patches_to_apply.append((m_entry, patched_bytes))
        verified_inputs.append((m_entry, out_patched))

    # Optionally patch FIRST.GRC palettes
    for pal_iso_file, pal_dict in sorted(palette_updates_by_file.items()):
        if pal_iso_file not in manifest_lookup:
            raise ValueError(f"Palette file '{pal_iso_file}' not found in {args.manifest}")
        m_entry = manifest_lookup[pal_iso_file]
        src_grc = args.extracted_dir / pal_iso_file.lstrip("/")
        if not src_grc.exists():
            raise FileNotFoundError(f"Extracted palette archive not found: {src_grc}")

        orig_grc = src_grc.read_bytes()
        clean_updates = {pal_idx: words for pal_idx, (words, _) in pal_dict.items()}
        patched_grc, grc_stats = patch_first_grc_palettes(orig_grc, clean_updates, pal_iso_file)

        out_grc = args.patched_dir / pal_iso_file.lstrip("/")
        out_grc.parent.mkdir(parents=True, exist_ok=True)
        out_grc.write_bytes(patched_grc)
        print(
            f"Patched {pal_iso_file}: palettes={grc_stats['palettes_updated']}, "
            f"compressed={grc_stats['compressed_size']:,}/{grc_stats['orig_size']:,} B "
            f"(slack={grc_stats['slack_bytes']:,} B) -> {out_grc}"
        )

        patches_to_apply.append((m_entry, patched_grc))
        verified_inputs.append((m_entry, out_grc))

    # Copy original disc image to out_disc and update .cue if Mode2/2352
    print(f"Copying {orig_disc} -> {out_disc} ...")
    copy_file_chunked(orig_disc, out_disc)

    out_cue: Path | None = None
    if sector_format == "bin2352_mode2" and out_disc.suffix.lower() == ".bin":
        src_cue = args.cue or orig_disc.with_suffix(".cue")
        out_cue = out_disc.with_suffix(".cue")
        write_cue_for_bin(src_cue, out_cue, out_disc.name)
        print(f"Wrote CUE sheet: {out_cue}")

    print(f"Patching {len(patches_to_apply)} file(s) in {out_disc} ({sector_format})...")
    patch_disc_image(out_disc, sector_format, patches_to_apply)

    print("Running post-patch self-verification (EDC + P/Q ECC + byte-exact + untouched sectors)...")
    verify_report = verify_disc_image(out_disc, sector_format, verified_inputs, orig_disc_path=orig_disc)
    print(json.dumps(verify_report, indent=2))
    print(f"DONE! Bootable modded image ready at: {out_cue or out_disc}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

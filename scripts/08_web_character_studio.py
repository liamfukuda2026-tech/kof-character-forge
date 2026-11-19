#!/usr/bin/env python3
"""KOF 2002 (PS2, SLES-53381) - Editor Visual Local no Navegador.

Interface web simples e intuitiva (click + drag-and-drop + Canvas 2D) para:
  - Visualizar todos os 46 personagens do disco com sprites reais
  - Editar paletas de cores A/B/C/D (FIRST.GRC) com preview ao vivo
  - Navegar pelos frames, pintar pixels no navegador ou arrastar PNG/JPG/WEBP
  - Inspecionar/patchear qualquer um dos 392 arquivos do disco (07_disc_editor)
  - Reconstruir e verificar build/kof2002_edit.bin + .cue (Mode 2 EDC/ECC)
  - Abrir direto no PCSX2

Uso:
  python scripts/08_web_character_studio.py [--port 8765] [--open]
  python scripts/08_web_character_studio.py --self-test
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import io
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
import webbrowser
from bisect import bisect_right
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.characters import CharacterSlot, list_character_slots, resolve_character_slot
from core.forge_project import ForgeProject
from core.skills import registry as skill_registry, execute_skill
from core.ai_assistant import ForgeAIAssistant


def _load(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Falha ao carregar {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M02 = _load("kof_decode", "02_decode_sprites.py")
M03 = _load("kof_build", "03_build_mod_iso.py")
M07 = _load("kof_disc", "07_disc_editor.py")

PALETTE_TABLE_OFFSET = M02.PALETTE_TABLE_OFFSET
BTN_NAMES = M02.BTN_NAMES

# Mapeamento verificado de nomes conhecidos (o card também exibe sempre o sprite real).
KNOWN_CHAR_NAMES: dict[int, str] = {
    0x00: "Kyo Kusanagi",
    0x01: "Benimaru Nikaido",
    0x02: "Goro Daimon",
    0x03: "Terry Bogard",
    0x04: "Andy Bogard",
    0x05: "Joe Higashi",
    0x06: "Ryo Sakazaki",
    0x07: "Robert Garcia",
    0x08: "Yuri Sakazaki",
    0x09: "Leona Heidern",
    0x0A: "Ralf Jones",
    0x0B: "Clark Still",
    0x0C: "Athena Asamiya",
    0x0D: "Sie Kensou",
    0x0E: "Chin Gentsai",
    0x0F: "Chizuru Kagura",
    0x10: "Mai Shiranui",
    0x11: "Kim Kaphwan",
    0x12: "Chang Koehan",
    0x13: "Choi Bounge",
    0x14: "Iori Yagami",
    0x15: "Mature",
    0x16: "Vice",
    0x17: "Ryuji Yamazaki",
    0x18: "Blue Mary",
    0x19: "Billy Kane",
    0x1A: "Yashiro Nanakase",
    0x1B: "Shermie",
    0x1C: "Chris",
    0x1D: "K'",
    0x1E: "Maxima",
    0x1F: "Whip",
    0x20: "Vanessa",
    0x21: "Seth",
    0x22: "Ramon",
    0x23: "Kula Diamond",
    0x24: "K9999",
    0x25: "Angel",
    0x26: "Bao / Extra",
    0x27: "Omega Rugal",
    0x28: "Kusanagi",
    0x2E: "Shingo Yabuki",
    0x2F: "King",
    0x30: "Orochi Iori",
    0x31: "Geese Howard",
    0x32: "Goenitz",
}


class StudioBackend:
    """Camada thread-safe sobre Project (07), Decoder (02), Patcher (03) e Audio AFS."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.proj = M07.Project()
        self._thumb_cache: dict[tuple[int, str], bytes] = {}
        self._first_grc_cache: tuple[bytes, bytes, int, int] | None = None
        self._afs_index_cache: dict[int, tuple[int, int, str]] | None = None
        self._wav_cache: dict[tuple[int, int], bytes] = {}
        self._build_lock = threading.Lock()
        self._last_build_status: dict[str, Any] = {
            "exists": M07.DEFAULT_OUT.exists(),
            "output": str(M07.DEFAULT_OUT),
            "pcsx2_path": str(M07.find_pcsx2() or ""),
        }

    # ----------------------------------------------------------- Audio CRI ADX v4 (BGM.AFS)
    def _bgm_afs_path(self) -> Path:
        entry = self.proj.entry("/ADX/BGM.AFS")
        ed = self.proj.edited_path(entry)
        return ed if ed.exists() else (M07.EXTRACTED / self.proj.rel(entry))

    def _get_afs_char_map(self) -> dict[int, tuple[int, int, str]]:
        if self._afs_index_cache is not None:
            return self._afs_index_cache
        afs_p = self._bgm_afs_path()
        mapping: dict[int, tuple[int, int, str]] = {}
        with afs_p.open("rb") as f:
            magic, count = struct.unpack("<4sI", f.read(8))
            if magic != b"AFS\x00":
                raise ValueError("BGM.AFS inválido")
            entries = [struct.unpack("<2I", f.read(8)) for _ in range(count + 1)]
            dir_off, _ = entries[-1]
            f.seek(dir_off)
            for idx in range(count):
                raw_name = f.read(48)[:32].split(b"\x00")[0].decode("ascii", "replace").strip()
                low = raw_name.lower()
                if low.startswith("p") and low.endswith(".acx") and len(low) == 7:
                    try:
                        cid = int(low[1:3], 16)
                        mapping[cid] = (entries[idx][0], entries[idx][1], raw_name)
                    except ValueError:
                        pass
        self._afs_index_cache = mapping
        return mapping

    def _read_char_acx(self, cid: int) -> tuple[str, list[bytes]]:
        amap = self._get_afs_char_map()
        if cid not in amap:
            return "", []
        off, sz, name = amap[cid]
        with self._bgm_afs_path().open("rb") as f:
            f.seek(off)
            acx = f.read(sz)
        if len(acx) < 8:
            return name, []
        magic, count = struct.unpack_from(">2I", acx, 0)
        if magic != 0 or count <= 0 or count > 256:
            return name, []
        clips: list[bytes] = []
        for i in range(count):
            co, cs = struct.unpack_from(">2I", acx, 8 + i * 8)
            clips.append(acx[co : co + cs])
        return name, clips

    @staticmethod
    def decode_adx_to_wav(adx: bytes) -> bytes:
        if len(adx) < 40 or adx[0] != 0x80 or adx[1] != 0x00:
            raise ValueError("Cabeçalho CRI ADX inválido")
        data_off = struct.unpack_from(">H", adx, 2)[0] + 4
        enc, blk_sz, bps, ch, sr, total_samples, cutoff = struct.unpack_from(">BBBBIIH", adx, 4)
        a = math.sqrt(2.0) - math.cos(2.0 * math.pi * cutoff / max(1, sr))
        b = math.sqrt(2.0) - 1.0
        c = (a - math.sqrt(max(0.0, (a + b) * (a - b)))) / b
        c1 = int(c * 2.0 * 4096.0 + 0.5)
        c2 = int(-(c * c) * 4096.0 - 0.5)
        s1 = s2 = 0
        out = bytearray()
        pos = data_off
        n_blocks = (len(adx) - data_off) // blk_sz
        nib_lut = [i - 16 if i >= 8 else i for i in range(16)]
        samples_left = total_samples
        for _ in range(n_blocks):
            if samples_left <= 0:
                break
            hdr = (adx[pos] << 8) | adx[pos + 1]
            if hdr & 0x8000:
                break
            scale = (hdr & 0x1FFF) + 1
            for b_i in range(2, blk_sz):
                byte = adx[pos + b_i]
                for nib in (nib_lut[byte >> 4], nib_lut[byte & 0x0F]):
                    if samples_left <= 0:
                        break
                    v = nib * scale + ((c1 * s1 + c2 * s2) >> 12)
                    if v > 32767:
                        v = 32767
                    elif v < -32768:
                        v = -32768
                    s2 = s1
                    s1 = v
                    out += struct.pack("<h", v)
                    samples_left -= 1
            pos += blk_sz
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(out)
        return buf.getvalue()

    def list_voices(self, cid: int) -> dict[str, Any]:
        with self.lock:
            acx_name, clips = self._read_char_acx(cid)
            items = []
            n = len(clips)
            sfx_start = max(0, n - 10)
            for idx, adx in enumerate(clips):
                sr = 24000
                samples = 0
                if len(adx) >= 18 and adx[:2] == b"\x80\x00":
                    _, _, _, _, sr, samples, _ = struct.unpack_from(">BBBBIIH", adx, 4)
                dur = round(samples / max(1, sr), 2)
                is_sfx = idx >= sfx_start
                kind = "sfx" if is_sfx else "voice"
                label = f"SFX #{idx:02d} ({dur}s)" if is_sfx else f"Voz #{idx:02d} ({dur}s)"
                items.append(
                    {
                        "clip_index": idx,
                        "label": label,
                        "kind": kind,
                        "duration_s": dur,
                        "sample_rate": sr,
                        "bytes": len(adx),
                    }
                )
            return {
                "cid": cid,
                "hex_id": f"P{cid:02X}",
                "acx_name": acx_name,
                "total_clips": n,
                "clips": items,
            }

    def get_voice_wav(self, cid: int, clip_idx: int) -> bytes:
        with self.lock:
            key = (cid, clip_idx)
            if key in self._wav_cache:
                return self._wav_cache[key]
            _, clips = self._read_char_acx(cid)
            if clip_idx < 0 or clip_idx >= len(clips):
                raise IndexError(f"Clip {clip_idx} fora do intervalo 0..{len(clips)-1}")
            wav = self.decode_adx_to_wav(clips[clip_idx])
            self._wav_cache[key] = wav
            return wav

    # ----------------------------------------------------------- utilidades GRC/ELF
    def _first_grc_entry(self) -> dict[str, Any]:
        return self.proj.entry("/DATA/BASE/FIRST.GRC")

    def _elf_entry(self) -> dict[str, Any]:
        return self.proj.entry("/SLES_533.81")

    def _get_pal_bank(self) -> tuple[bytes, int, int]:
        raw = self.proj.current_bytes(self._first_grc_entry())
        if self._first_grc_cache is not None and self._first_grc_cache[0] == raw:
            _, sub, sub_off, num_pals = self._first_grc_cache
            return sub, sub_off, num_pals
        magic = struct.unpack_from("<I", raw, 0)[0]
        if magic != M02.FIRST_GRC_MAGIC:
            raise ValueError(f"FIRST.GRC inválido: 0x{magic:08x}")
        buf = raw[:4] + M02.lz_decompress(raw[4:])
        offs = struct.unpack_from(f"<{M02.FIRST_GRC_OFFSET_COUNT}I", buf, 0)
        pal_start = offs[1]
        pal_end = offs[2]
        sub = buf[pal_start:pal_end]
        num_pals = len(sub) // M02.BYTES_PER_PALETTE
        self._first_grc_cache = (raw, sub, pal_start, num_pals)
        return sub, pal_start, num_pals

    def _char_entries(self) -> list[tuple[int, dict[str, Any]]]:
        chars: list[tuple[int, dict[str, Any]]] = []
        for path, entry in self.proj.files.items():
            if path.startswith("/DATA/PL0/P") or path.startswith("/DATA/PL1/P"):
                name = Path(path).name
                if name.startswith("P") and name.endswith(".BIN") and len(name) == 7:
                    try:
                        cid = int(name[1:3], 16)
                    except ValueError:
                        continue
                    chars.append((cid, entry))
        chars.sort(key=lambda item: item[0])
        return chars

    def _char_entry_by_id(self, cid: int) -> dict[str, Any]:
        for c, entry in self._char_entries():
            if c == cid:
                return entry
        raise KeyError(f"Personagem P{cid:02X} não encontrado no disco")

    def _base_palettes(self, cid: int) -> dict[str, int]:
        elf = self.proj.current_bytes(self._elf_entry())
        off = PALETTE_TABLE_OFFSET + cid * 8
        vals = struct.unpack_from("<4H", elf, off)
        return {btn: int(vals[i]) for i, btn in enumerate(BTN_NAMES)}

    def _palette_for_slot(self, cid: int, btn: str, pal_slot: int) -> tuple[int, list[int], list[list[int]], list[tuple[int, int, int]]]:
        base_pals = self._base_palettes(cid)
        pal_sub, _, num_pals = self._get_pal_bank()
        base_idx = base_pals[btn.upper()]
        slot_delta = (pal_slot - 0x10) if pal_slot >= 0x10 else pal_slot
        pal_idx = base_idx + slot_delta
        if pal_idx < 0 or pal_idx >= num_pals:
            pal_idx = base_idx
        words, rgba, png_rgb = M02.decode_palette(pal_sub, pal_idx, num_pals)
        return pal_idx, words, rgba, png_rgb

    # ----------------------------------------------------------- API: personagens
    def list_characters(self) -> list[dict[str, Any]]:
        with self.lock:
            grc_mod = self.proj.is_modified(self._first_grc_entry())
            elf = self.proj.current_bytes(self._elf_entry())
            out = []
            for cid, entry in self._char_entries():
                data = self.proj.current_bytes(entry)
                first_off = struct.unpack_from("<I", data, 0)[0] & 0x7FFFFFFF
                num_frames = first_off // 4
                vals = struct.unpack_from("<4H", elf, PALETTE_TABLE_OFFSET + cid * 8)
                # Verifica uma quote curta do SLES_533.81 (apenas referência)
                quote = ""
                q_off = 0x34E0E0 + cid * 32
                if q_off + 4 <= len(elf):
                    p0 = struct.unpack_from("<I", elf, q_off)[0]
                    str_off = p0 - 0xFFF00
                    if 0 <= str_off < len(elf):
                        quote = (
                            elf[str_off : str_off + 90]
                            .lstrip(b"\x00")
                            .split(b"\xff")[0]
                            .replace(b"\r", b" ")
                            .decode("ascii", "replace")
                            .strip()
                        )
                out.append(
                    {
                        "cid": cid,
                        "hex_id": f"P{cid:02X}",
                        "name": M02.CANONICAL_NAMES.get(cid, "").replace("_", " ").title() or KNOWN_CHAR_NAMES.get(cid, f"P{cid:02X}"),
                        "iso_path": entry["clean_path"],
                        "size_bytes": entry["size_bytes"],
                        "lba_start": entry["lba_start"],
                        "num_frames": num_frames,
                        "base_palettes": {btn: int(vals[i]) for i, btn in enumerate(BTN_NAMES)},
                        "bin_modified": self.proj.is_modified(entry),
                        "palette_bank_modified": grc_mod,
                        "quote_sample": quote[:72],
                    }
                )
            return out

    def get_char_thumb_png(self, cid: int, btn: str = "A") -> bytes:
        btn = btn.upper() if btn.upper() in BTN_NAMES else "A"
        with self.lock:
            key = (cid, btn)
            if key in self._thumb_cache:
                return self._thumb_cache[key]

            entry = self._char_entry_by_id(cid)
            data = self.proj.current_bytes(entry)
            first_off = struct.unpack_from("<I", data, 0)[0] & 0x7FFFFFFF
            num_slots = first_off // 4
            offs = struct.unpack_from(f"<{num_slots}I", data, 0)
            unique_offs = sorted({o & 0x7FFFFFFF for o in offs})

            chosen_canvas = None
            chosen_slot = 0x10
            for raw_off in offs:
                if raw_off & 0x80000000:
                    continue
                off_int = raw_off & 0x7FFFFFFF
                nxt = bisect_right(unique_offs, off_int)
                end_int = unique_offs[nxt] if nxt < len(unique_offs) else len(data)
                info, canvas = M02.decode_tile_block(data[off_int:end_int])
                if info["is_empty_stub"] or canvas is None or not np.any(canvas):
                    continue
                if chosen_canvas is None:
                    chosen_canvas = canvas
                    chosen_slot = info["pal_slot"]
                if info["pal_slot"] == 0x10 and info["width_px"] >= 32 and info["height_px"] >= 48:
                    chosen_canvas = canvas
                    chosen_slot = info["pal_slot"]
                    break

            if chosen_canvas is None:
                im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            else:
                _, _, rgba, _ = self._palette_for_slot(cid, btn, chosen_slot)
                im = M02.render_rgba_from_indices(chosen_canvas, rgba)
                bbox = im.getbbox()
                if bbox:
                    im = im.crop(bbox)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            png = buf.getvalue()
            self._thumb_cache[key] = png
            return png

    # ----------------------------------------------------------- API: paletas
    def get_palettes(self, cid: int) -> dict[str, Any]:
        with self.lock:
            base_pals = self._base_palettes(cid)
            pal_sub, _, num_pals = self._get_pal_bank()
            buttons = {}
            for btn, pal_idx in base_pals.items():
                words, rgba, _ = M02.decode_palette(pal_sub, pal_idx, num_pals)
                buttons[btn] = {
                    "pal_idx": pal_idx,
                    "pal_idx_hex": f"0x{pal_idx:04x}",
                    "words": words,
                    "rgba": rgba,
                    "hex": [f"#{r:02x}{g:02x}{b:02x}" for r, g, b, _ in rgba],
                }
            return {
                "cid": cid,
                "hex_id": f"P{cid:02X}",
                "first_grc_modified": self.proj.is_modified(self._first_grc_entry()),
                "buttons": buttons,
            }

    def save_palette(self, cid: int, btn: str, colors_hex: list[str], pal_idx_override: int | None = None) -> dict[str, Any]:
        btn = btn.upper()
        if btn not in BTN_NAMES:
            raise ValueError(f"Botão inválido: {btn}")
        if len(colors_hex) not in (15, 16):
            raise ValueError("Envie 15 cores (#RRGGBB para índices 1..15) ou 16 cores (0..15)")
        c15 = colors_hex[1:] if len(colors_hex) == 16 else colors_hex

        words_1_to_15: list[int] = []
        for hx in c15:
            hx = hx.strip().lstrip("#")
            if len(hx) != 6:
                raise ValueError(f"Cor inválida: #{hx}")
            r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            words_1_to_15.append(M03._rgb8_to_rgb555_word(r, g, b))

        with self.lock:
            base_pals = self._base_palettes(cid)
            target_idx = int(pal_idx_override) if pal_idx_override is not None else base_pals[btn]
            cur_grc = self.proj.current_bytes(self._first_grc_entry())
            patched_grc, stats = M03.patch_first_grc_palettes(
                cur_grc, {target_idx: words_1_to_15}, "/DATA/BASE/FIRST.GRC"
            )
            msg = self.proj.save_bytes(self._first_grc_entry(), patched_grc)
            self._first_grc_cache = None
            self._thumb_cache.clear()
            return {"status": msg, "pal_idx": target_idx, "stats": stats}

    def revert_character_palettes(self, cid: int) -> dict[str, Any]:
        with self.lock:
            grc_entry = self._first_grc_entry()
            if not self.proj.is_modified(grc_entry):
                return {"status": "já estava no original"}
            orig_raw = self.proj.original_bytes(grc_entry)
            orig_buf = orig_raw[:4] + M02.lz_decompress(orig_raw[4:])
            offs = struct.unpack_from(f"<{M02.FIRST_GRC_OFFSET_COUNT}I", orig_buf, 0)
            pal_start = offs[1]
            pal_end = offs[2]
            orig_sub = orig_buf[pal_start:pal_end]
            num_pals = len(orig_sub) // M02.BYTES_PER_PALETTE

            base_pals = self._base_palettes(cid)
            updates: dict[int, list[int]] = {}
            for pal_idx in base_pals.values():
                if 0 <= pal_idx < num_pals:
                    words = list(struct.unpack_from("<16H", orig_sub, pal_idx * 32))
                    updates[pal_idx] = words[1:]

            cur_grc = self.proj.current_bytes(grc_entry)
            patched_grc, _ = M03.patch_first_grc_palettes(cur_grc, updates, "/DATA/BASE/FIRST.GRC")
            # Se todas as paletas ficaram iguais às originais, reverte o override inteiro
            cur_buf = patched_grc[:4] + M02.lz_decompress(patched_grc[4:])
            if cur_buf == orig_buf:
                self.proj.revert(grc_entry)
                msg = "paletas do disco revertidas ao original"
            else:
                msg = self.proj.save_bytes(grc_entry, patched_grc)
            self._first_grc_cache = None
            self._thumb_cache.clear()
            return {"status": msg}

    # ----------------------------------------------------------- API: frames
    def list_frames(self, cid: int) -> dict[str, Any]:
        with self.lock:
            entry = self._char_entry_by_id(cid)
            data = self.proj.current_bytes(entry)
            first_off = struct.unpack_from("<I", data, 0)[0] & 0x7FFFFFFF
            num_slots = first_off // 4
            offs = struct.unpack_from(f"<{num_slots}I", data, 0)
            unique_offs = sorted({o & 0x7FFFFFFF for o in offs})

            seen: dict[int, int] = {}
            frames = []
            for idx, raw_o in enumerate(offs):
                stub_bit = bool(raw_o & 0x80000000)
                off_int = raw_o & 0x7FFFFFFF
                nxt = bisect_right(unique_offs, off_int)
                end_int = unique_offs[nxt] if nxt < len(unique_offs) else len(data)
                hdr_sz, pal_slot, w_t, h_t, flg = struct.unpack_from("<IBBBB", data, off_int)
                tile_px = 32 if flg == 0x00 else 16
                is_stub = stub_bit or flg == 0xFF
                shared = seen.get(off_int)
                if shared is None:
                    seen[off_int] = idx
                frames.append(
                    {
                        "frame_index": idx,
                        "offset_hex": f"0x{off_int:06x}",
                        "block_size": end_int - off_int,
                        "is_stub": is_stub,
                        "shared_with": shared,
                        "w_tiles": w_t,
                        "h_tiles": h_t,
                        "width_px": 0 if is_stub else w_t * tile_px,
                        "height_px": 0 if is_stub else h_t * tile_px,
                        "pal_slot": pal_slot,
                        "flg": flg,
                        "editable": (not is_stub) and flg in (0x01, 0x03),
                    }
                )
            return {
                "cid": cid,
                "hex_id": f"P{cid:02X}",
                "iso_path": entry["clean_path"],
                "modified": self.proj.is_modified(entry),
                "total_slots": num_slots,
                "frames": frames,
            }

    def list_sequences(self, cid: int) -> dict[str, Any]:
        """Segmenta automaticamente os frames do personagem em animações reproduzíveis."""
        with self.lock:
            fr_info = self.list_frames(cid)
            frames = fr_info["frames"]
            raw_runs: list[list[dict[str, Any]]] = []
            cur: list[dict[str, Any]] = []

            for f in frames:
                if f["is_stub"]:
                    if cur:
                        raw_runs.append(cur)
                        cur = []
                    continue
                if not cur:
                    cur = [f]
                    continue
                prev = cur[-1]
                gap = f["frame_index"] != prev["frame_index"] + 1
                pal_change = f["pal_slot"] != prev["pal_slot"]
                size_jump = (
                    len(cur) >= 5
                    and (abs(f["w_tiles"] - prev["w_tiles"]) >= 2 or abs(f["h_tiles"] - prev["h_tiles"]) >= 2)
                )
                max_len = len(cur) >= 14
                if gap or pal_change or size_jump or max_len:
                    raw_runs.append(cur)
                    cur = [f]
                else:
                    cur.append(f)
            if cur:
                raw_runs.append(cur)

            preset_names = [
                "Postura / Idle (Stance)",
                "Caminhada / Movimento",
                "Recuo / Guarda / Pulo",
                "Agachar / Transição",
                "Ataque Básico 1",
                "Ataque Básico 2",
                "Ataque Forte / Comando",
                "Golpe Aéreo / Chute",
            ]
            sequences = []
            body_idx = 0
            fx_idx = 0
            for seq_id, run in enumerate(raw_runs):
                s_idx = run[0]["frame_index"]
                e_idx = run[-1]["frame_index"]
                ps = run[0]["pal_slot"]
                if ps == 0x10:
                    if body_idx < len(preset_names):
                        label = f"{body_idx+1:02d}. {preset_names[body_idx]}"
                    else:
                        label = f"{body_idx+1:02d}. Golpe / Ação #{body_idx+1}"
                    kind = "body"
                    body_idx += 1
                else:
                    fx_idx += 1
                    label = f"FX {fx_idx:02d}. Efeito / Projétil (pal 0x{ps:02X})"
                    kind = "effect"
                sequences.append(
                    {
                        "seq_id": seq_id,
                        "label": f"{label} [#{s_idx}..#{e_idx}] ({len(run)}f)",
                        "start_frame": s_idx,
                        "end_frame": e_idx,
                        "frame_indices": [x["frame_index"] for x in run],
                        "count": len(run),
                        "pal_slot": ps,
                        "kind": kind,
                    }
                )
            return {
                "cid": cid,
                "hex_id": f"P{cid:02X}",
                "total_sequences": len(sequences),
                "sequences": sequences,
                "specials": self._build_special_presets(cid, sequences, frames),
            }

    def _build_special_presets(
        self, cid: int, sequences: list[dict[str, Any]], frames: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Monta botões rápidos (Idle, Ataques, Especial 1, 2, 3, 4, Super SDM, MAX2) + áudio sincronizado."""
        _, clips = self._read_char_acx(cid)
        n_clips = len(clips)
        sfx_base = max(0, n_clips - 10)

        def pick_clip(idx: int) -> int | None:
            return idx if 0 <= idx < n_clips else None

        body_seqs = [s for s in sequences if s["kind"] == "body" and s["count"] >= 4]
        fx_seqs = [s for s in sequences if s["kind"] == "effect" and s["count"] >= 2]

        presets: list[dict[str, Any]] = []
        if body_seqs:
            s0 = body_seqs[0]
            presets.append(
                {
                    "id": "idle",
                    "title": "🥋 Postura (Idle)",
                    "start_frame": s0["start_frame"],
                    "end_frame": s0["end_frame"],
                    "fx_start": None,
                    "fx_end": None,
                    "voice_clip": pick_clip(10),
                    "sfx_clip": None,
                }
            )
        if len(body_seqs) > 1:
            s1 = body_seqs[1]
            presets.append(
                {
                    "id": "walk",
                    "title": "👣 Caminhada",
                    "start_frame": s1["start_frame"],
                    "end_frame": s1["end_frame"],
                    "fx_start": None,
                    "fx_end": None,
                    "voice_clip": None,
                    "sfx_clip": None,
                }
            )
        if len(body_seqs) > 4:
            sn = body_seqs[4]
            presets.append(
                {
                    "id": "normal",
                    "title": "👊 Ataque Normal",
                    "start_frame": sn["start_frame"],
                    "end_frame": min(sn["start_frame"] + 16, body_seqs[min(6, len(body_seqs) - 1)]["end_frame"]),
                    "fx_start": None,
                    "fx_end": None,
                    "voice_clip": pick_clip(0),
                    "sfx_clip": pick_clip(sfx_base),
                }
            )

        # Seleciona os golpes especiais (segunda metade do arquivo PXX.BIN + pareamento com efeitos FX)
        total_f = max(1, len(frames))
        special_candidates: list[tuple[int, int, int | None, int | None]] = []

        # 1) Primeiro prioriza golpes que disparam efeitos dedicados (projéteis/magias/cortes)
        used_ranges: list[tuple[int, int]] = []
        for fx in fx_seqs:
            fxs, fxe = fx["start_frame"], fx["end_frame"]
            # Procura a animação de corpo imediatamente anterior (ou posterior) ao efeito
            prev_body = [b for b in body_seqs if b["end_frame"] <= fxs and fxs - b["end_frame"] <= 30]
            if prev_body:
                b = prev_body[-1]
                bs, be = b["start_frame"], b["end_frame"]
            else:
                next_body = [b for b in body_seqs if b["start_frame"] >= fxe and b["start_frame"] - fxe <= 30]
                if next_body:
                    b = next_body[0]
                    bs, be = b["start_frame"], b["end_frame"]
                else:
                    bs, be = max(0, fxs - 12), fxs - 1
            if any(abs(bs - us) < 12 for us, _ in used_ranges):
                continue
            used_ranges.append((bs, be))
            special_candidates.append((bs, be, fxs, fxe))

        # 2) Completa com grandes sequências de combate da metade final do PXX.BIN
        late_body = sorted(
            [b for b in body_seqs if b["start_frame"] >= total_f // 3 and b["count"] >= 6],
            key=lambda x: (x["count"], x["start_frame"]),
            reverse=True,
        )
        for b in late_body:
            bs, be = b["start_frame"], b["end_frame"]
            if any(abs(bs - us) < 14 for us, _ in used_ranges):
                continue
            used_ranges.append((bs, be))
            special_candidates.append((bs, be, None, None))

        special_candidates.sort(key=lambda item: item[0])

        sp_titles = [
            ("sp1", "🔥 Especial 1", 17, 0),
            ("sp2", "🔥 Especial 2", 20, 1),
            ("sp3", "🔥 Especial 3", 21, 2),
            ("sp4", "🔥 Especial 4", 22, 3),
            ("sdm", "⚡ Super Especial (SDM)", 24, 4),
            ("max2", "💥 Especial Secreto (MAX2)", 25, 5),
        ]
        for idx, (sp_id, title, v_pref, sfx_off) in enumerate(sp_titles):
            if idx >= len(special_candidates):
                break
            bs, be, fxs, fxe = special_candidates[idx]
            v_clip = pick_clip(v_pref) if v_pref < sfx_base else pick_clip((idx + 2) % max(1, sfx_base))
            s_clip = pick_clip(sfx_base + (sfx_off % max(1, n_clips - sfx_base)))
            presets.append(
                {
                    "id": sp_id,
                    "title": title,
                    "start_frame": bs,
                    "end_frame": be,
                    "fx_start": fxs,
                    "fx_end": fxe,
                    "voice_clip": v_clip,
                    "sfx_clip": s_clip,
                }
            )
        return presets

    def _decode_frame(self, cid: int, frame_idx: int) -> tuple[dict[str, Any], np.ndarray | None, dict[str, Any]]:
        entry = self._char_entry_by_id(cid)
        data = self.proj.current_bytes(entry)
        first_off = struct.unpack_from("<I", data, 0)[0] & 0x7FFFFFFF
        num_slots = first_off // 4
        if frame_idx < 0 or frame_idx >= num_slots:
            raise IndexError(f"Frame {frame_idx} fora do intervalo 0..{num_slots - 1}")
        offs = struct.unpack_from(f"<{num_slots}I", data, 0)
        unique_offs = sorted({o & 0x7FFFFFFF for o in offs})
        raw_o = offs[frame_idx]
        off_int = raw_o & 0x7FFFFFFF
        nxt = bisect_right(unique_offs, off_int)
        end_int = unique_offs[nxt] if nxt < len(unique_offs) else len(data)
        info, canvas = M02.decode_tile_block(data[off_int:end_int])
        info["is_empty_stub"] = bool(raw_o & 0x80000000) or info["is_empty_stub"]
        info["offset_hex"] = f"0x{off_int:06x}"
        info["block_size"] = end_int - off_int
        return info, canvas, entry

    def get_frame_png(self, cid: int, frame_idx: int, btn: str = "A", mode: str = "rgba") -> bytes:
        btn = btn.upper() if btn.upper() in BTN_NAMES else "A"
        with self.lock:
            info, canvas, _ = self._decode_frame(cid, frame_idx)
            if info["is_empty_stub"] or canvas is None:
                im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                return buf.getvalue()
            _, _, rgba, png_rgb = self._palette_for_slot(cid, btn, info["pal_slot"])
            buf = io.BytesIO()
            if mode == "indexed":
                im = Image.fromarray(canvas, mode="P")
                flat = [c for rgb in png_rgb[:16] for c in rgb]
                im.putpalette(flat)
                im.save(buf, format="PNG", transparency=0, optimize=False)
            else:
                im = M02.render_rgba_from_indices(canvas, rgba)
                im.save(buf, format="PNG")
            return buf.getvalue()

    def get_frame_data(self, cid: int, frame_idx: int, btn: str = "A") -> dict[str, Any]:
        btn = btn.upper() if btn.upper() in BTN_NAMES else "A"
        with self.lock:
            info, canvas, _ = self._decode_frame(cid, frame_idx)
            pal_idx, words, rgba, _ = self._palette_for_slot(cid, btn, info["pal_slot"])
            if info["is_empty_stub"] or canvas is None:
                return {
                    "frame_index": frame_idx,
                    "is_stub": True,
                    "width_px": 0,
                    "height_px": 0,
                    "pal_slot": info["pal_slot"],
                    "pal_idx": pal_idx,
                    "flg": info["flg"],
                    "rgba": rgba,
                    "pixels_b64": "",
                }
            return {
                "frame_index": frame_idx,
                "is_stub": False,
                "width_px": info["width_px"],
                "height_px": info["height_px"],
                "w_tiles": info["w_tiles"],
                "h_tiles": info["h_tiles"],
                "pal_slot": info["pal_slot"],
                "pal_idx": pal_idx,
                "pal_idx_hex": f"0x{pal_idx:04x}",
                "flg": info["flg"],
                "offset_hex": info["offset_hex"],
                "block_size": info["block_size"],
                "rgba": rgba,
                "pixels_b64": base64.b64encode(canvas.astype(np.uint8).tobytes()).decode("ascii"),
            }

    def _fit_and_quantize_image(
        self,
        raw_bytes: bytes,
        target_w: int,
        target_h: int,
        orig_canvas: np.ndarray,
        palette_rgba: list[list[int]],
        remove_magenta: bool = True,
        auto_fit: bool = True,
    ) -> np.ndarray:
        """Converte qualquer PNG/JPG/WEBP enviado/arrastado para índices 0..15 exatos."""
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # Caso ideal: PNG já indexado 16 cores com dimensões exatas
            if img.format == "PNG" and img.mode == "P" and img.size == (target_w, target_h):
                arr = np.array(img, dtype=np.uint8)
                if arr.size == 0 or int(arr.max()) <= 15:
                    return arr

            rgba_im = img.convert("RGBA")
            arr = np.array(rgba_im, dtype=np.uint8)

        alpha = arr[:, :, 3]
        rgb = arr[:, :, :3].astype(np.int16)

        if remove_magenta:
            # Remove fundo magenta (#FF00FF +/- tolerância suave) se o usuário enviou chroma key
            dist_mag = np.sqrt(
                (rgb[:, :, 0] - 255) ** 2 + (rgb[:, :, 1] - 0) ** 2 + (rgb[:, :, 2] - 255) ** 2
            )
            arr[dist_mag < 55.0, 3] = 0

        opaque = arr[:, :, 3] >= 128
        if not np.any(opaque):
            return np.zeros((target_h, target_w), dtype=np.uint8)

        if auto_fit:
            ys, xs = np.where(opaque)
            crop_rgba = Image.fromarray(arr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1], mode="RGBA")
            # Encaixa na bounding box do sprite original (pés ancorados), ou no canvas se original vazio
            oys, oxs = np.where(orig_canvas > 0)
            if oys.size > 0:
                box_h = max(8, int(oys.max() - oys.min() + 1))
                box_w = max(8, int(oxs.max() - oxs.min() + 1))
                anchor_x = (int(oxs.min()) + int(oxs.max()) + 1) // 2
                anchor_bottom = int(oys.max()) + 1
            else:
                box_h, box_w = target_h, target_w
                anchor_x = target_w // 2
                anchor_bottom = target_h

            scale = min(box_w / crop_rgba.width, box_h / crop_rgba.height)
            new_w = max(1, min(target_w, int(round(crop_rgba.width * scale))))
            new_h = max(1, min(target_h, int(round(crop_rgba.height * scale))))
            resized = crop_rgba.resize((new_w, new_h), Image.Resampling.NEAREST)
            canvas_im = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            px = max(0, min(target_w - new_w, anchor_x - new_w // 2))
            py = max(0, min(target_h - new_h, anchor_bottom - new_h))
            canvas_im.paste(resized, (px, py), resized)
            arr = np.array(canvas_im, dtype=np.uint8)
        elif (arr.shape[1], arr.shape[0]) != (target_w, target_h):
            canvas_im = Image.fromarray(arr, mode="RGBA").resize((target_w, target_h), Image.Resampling.NEAREST)
            arr = np.array(canvas_im, dtype=np.uint8)

        # Quantiza pixels opacos diretamente para os 15 tons da paleta ativa do frame
        out = np.zeros((target_h, target_w), dtype=np.uint8)
        mask = arr[:, :, 3] >= 128
        if np.any(mask):
            pal_rgb = np.array([c[:3] for c in palette_rgba[1:16]], dtype=np.int32)  # (15, 3)
            pts = arr[mask][:, :3].astype(np.int32)  # (N, 3)
            d2 = ((pts[:, None, :] - pal_rgb[None, :, :]) ** 2).sum(axis=2)
            out[mask] = (np.argmin(d2, axis=1) + 1).astype(np.uint8)
        return out

    def save_frame(self, cid: int, frame_idx: int, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            info, orig_canvas, entry = self._decode_frame(cid, frame_idx)
            if info["is_empty_stub"] or orig_canvas is None:
                raise ValueError(f"Frame {frame_idx} é um stub vazio e não pode ser editado")
            if info["flg"] not in (0x01, 0x03):
                raise ValueError(f"Frame {frame_idx} usa flag 0x{info['flg']:02x} (apenas 0x01/0x03 editáveis)")

            w, h = info["width_px"], info["height_px"]
            btn = str(payload.get("btn", "A")).upper()
            if btn not in BTN_NAMES:
                btn = "A"

            if "pixels" in payload:
                arr = np.array(payload["pixels"], dtype=np.uint8)
                if arr.size != w * h:
                    raise ValueError(f"Esperado {w*h} pixels, recebido {arr.size}")
                if int(arr.max()) > 15:
                    raise ValueError("Índices precisam estar entre 0 e 15")
                new_pixels = arr.reshape((h, w))
            elif "image_base64" in payload:
                b64 = payload["image_base64"]
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                raw_img = base64.b64decode(b64)
                _, _, rgba, _ = self._palette_for_slot(cid, btn, info["pal_slot"])
                new_pixels = self._fit_and_quantize_image(
                    raw_img,
                    w,
                    h,
                    orig_canvas,
                    rgba,
                    remove_magenta=bool(payload.get("remove_magenta", True)),
                    auto_fit=bool(payload.get("auto_fit", True)),
                )
            else:
                raise ValueError("Envie 'pixels' ou 'image_base64'")

            cur_bin = self.proj.current_bytes(entry)
            patched_bin, stats = M03.patch_pxx_bin(
                cur_bin,
                {
                    frame_idx: (
                        new_pixels,
                        {
                            "w_tiles": info["w_tiles"],
                            "h_tiles": info["h_tiles"],
                            "pal_slot": info["pal_slot"],
                            "flg": info["flg"],
                        },
                    )
                },
                entry["clean_path"],
            )
            msg = self.proj.save_bytes(entry, patched_bin)
            for b in BTN_NAMES:
                self._thumb_cache.pop((cid, b), None)
            return {"status": msg, "stats": stats}

    def revert_character(self, cid: int) -> dict[str, Any]:
        with self.lock:
            entry = self._char_entry_by_id(cid)
            self.proj.revert(entry)
            for b in BTN_NAMES:
                self._thumb_cache.pop((cid, b), None)
            return {"status": f"{entry['clean_path']} revertido"}

    # ----------------------------------------------------------- API: arquivos & build
    def list_files(self, query: str = "", only_mod: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            q = query.strip().upper()
            out = []
            for path, e in sorted(self.proj.files.items()):
                m = self.proj.is_modified(e)
                if q and q not in path:
                    continue
                if only_mod and not m:
                    continue
                out.append(
                    {
                        "clean_path": e["clean_path"],
                        "size_bytes": e["size_bytes"],
                        "lba_start": e["lba_start"],
                        "lba_sectors": e["lba_sectors"],
                        "modified": m,
                    }
                )
            return out

    def get_file_hex(self, path: str, page: int = 0) -> dict[str, Any]:
        PAGE = 4096
        with self.lock:
            entry = self.proj.entry(path)
            data = self.proj.current_bytes(entry)
            total_pages = max(1, (len(data) + PAGE - 1) // PAGE)
            page = max(0, min(page, total_pages - 1))
            off = page * PAGE
            return {
                "clean_path": entry["clean_path"],
                "size_bytes": len(data),
                "lba_start": entry["lba_start"],
                "modified": self.proj.is_modified(entry),
                "page": page,
                "total_pages": total_pages,
                "offset_hex": f"0x{off:08X}",
                "hexdump": M07.hexdump(data[off : off + PAGE], off),
            }

    def patch_file(self, path: str, offset_str: str, hex_str: str) -> dict[str, Any]:
        with self.lock:
            entry = self.proj.entry(path)
            off = int(str(offset_str).strip(), 0)
            patch = M07.parse_hex(hex_str)
            if not patch:
                raise ValueError("Nenhum byte hex válido informado")
            msg = self.proj.patch_bytes(entry, off, patch)
            self._first_grc_cache = None
            self._thumb_cache.clear()
            return {"status": msg}

    def revert_file(self, path: str) -> dict[str, Any]:
        with self.lock:
            if path.lower() == "all":
                for e in self.proj.modified_entries():
                    self.proj.revert(e)
                msg = "todas as alterações revertidas"
            else:
                self.proj.revert(self.proj.entry(path))
                msg = f"{path} revertido"
            self._first_grc_cache = None
            self._thumb_cache.clear()
            return {"status": msg}

    def build_iso(self) -> dict[str, Any]:
        if not self._build_lock.acquire(blocking=False):
            raise RuntimeError("Um build já está em andamento")
        try:
            logs: list[str] = []
            t0 = time.perf_counter()
            out = self.proj.build(M07.DEFAULT_OUT, log=lambda m: logs.append(str(m)))
            dt = round(time.perf_counter() - t0, 2)
            self._last_build_status = {
                "exists": out.exists(),
                "output": str(out),
                "cue": str(out.with_suffix(".cue")),
                "elapsed_s": dt,
                "logs": logs,
                "pcsx2_path": str(M07.find_pcsx2() or ""),
            }
            return self._last_build_status
        finally:
            self._build_lock.release()

    def launch_pcsx2(self, pcsx2_path: str | None = None) -> dict[str, Any]:
        exe = M07.find_pcsx2(pcsx2_path) if pcsx2_path else M07.find_pcsx2()
        if not exe:
            raise FileNotFoundError(
                "pcsx2-qt.exe não encontrado automaticamente. Informe o caminho completo."
            )
        target = M07.DEFAULT_OUT if M07.DEFAULT_OUT.exists() else self.proj.src_bin
        subprocess.Popen([str(exe), "-batch", "--", str(target)])
        return {"launched": str(exe), "disc": str(target)}

    # ----------------------------------------------------------- Character Forge API
    def list_forge_projects(self) -> list[dict[str, Any]]:
        with self.lock:
            return ForgeProject.list_projects()

    def create_forge_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("Nome do projeto não pode ser vazio")
            base_slot = payload.get("base_slot") or payload.get("base_cid") or 0x03
            author = str(payload.get("author", "")).strip()
            description = str(payload.get("description", "")).strip()
            overwrite = bool(payload.get("overwrite", False))
            proj = ForgeProject.create(
                name=name,
                base_slot=base_slot,
                author=author,
                description=description,
                overwrite=overwrite,
            )
            return {"status": "ok", "project": proj.to_dict()}

    def get_forge_project(self, name: str) -> dict[str, Any]:
        with self.lock:
            proj = ForgeProject.load(name)
            return {
                "status": "ok",
                "project": proj.to_dict(),
                "frames": proj.list_frames(),
            }

    def save_forge_project(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            proj = ForgeProject.load(name)
            if "author" in payload:
                proj.data["author"] = str(payload["author"]).strip()
            if "description" in payload:
                proj.data["description"] = str(payload["description"]).strip()
            if "palettes" in payload and isinstance(payload["palettes"], dict):
                proj.data["palettes"] = payload["palettes"]
            proj.save()
            return {"status": "ok", "project": proj.to_dict()}

    def import_forge_sprite(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            proj = ForgeProject.load(name)
            frame_idx = int(payload.get("frame_idx", 0))
            btn = str(payload.get("btn", "A")).upper()
            remove_magenta = bool(payload.get("remove_magenta", True))
            auto_fit = bool(payload.get("auto_fit", True))

            base_cid = proj.base_slot.char_id
            info, orig_canvas, _ = self._decode_frame(base_cid, frame_idx)
            if info["is_empty_stub"] or orig_canvas is None:
                raise ValueError(f"Frame #{frame_idx} é um stub vazio no slot base {proj.base_slot.display_name}")

            w, h = info["width_px"], info["height_px"]
            b64 = str(payload.get("image_base64", ""))
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            if not b64:
                raise ValueError("Nenhuma imagem informada em image_base64")
            raw_bytes = base64.b64decode(b64)

            _, _, rgba, _ = self._palette_for_slot(base_cid, btn, info["pal_slot"])
            new_pixels = self._fit_and_quantize_image(
                raw_bytes,
                w,
                h,
                orig_canvas,
                rgba,
                remove_magenta=remove_magenta,
                auto_fit=auto_fit,
            )

            out_im = Image.fromarray(new_pixels, mode="P")
            flat_pal = [ch for rgb in rgba[:16] for ch in rgb[:3]]
            while len(flat_pal) < 768:
                flat_pal.extend([0, 0, 0])
            out_im.putpalette(flat_pal)

            dest_path = proj.add_frame(frame_idx, out_im)
            proj.update_validation(
                valid=False,
                errors=[],
                warnings=["Sprites alterados. Execute a validação antes de injetar no jogo."],
            )
            return {
                "status": "ok",
                "frame_idx": frame_idx,
                "path": str(dest_path),
                "width": w,
                "height": h,
                "stats": {
                    "width": w,
                    "height": h,
                    "colors": 16,
                    "size_bytes": dest_path.stat().st_size,
                },
                "project": proj.to_dict(),
            }

    def validate_forge_character(self, name: str) -> dict[str, Any]:
        with self.lock:
            res = execute_skill("skill_validate_character", project_name=name)
            proj = ForgeProject.load(name)
            return {
                "status": "ok" if res.success else "error",
                "valid": res.success,
                "message": res.message,
                "errors": res.errors,
                "warnings": res.warnings,
                "data": res.data,
                "project": proj.to_dict(),
            }

    def inject_forge_character(self, name: str) -> dict[str, Any]:
        with self.lock:
            proj = ForgeProject.load(name)
            res_val = execute_skill("skill_validate_character", project_name=name)
            if not res_val.success:
                raise ValueError(f"Personagem não aprovado na validação: {'; '.join(res_val.errors)}")

            base_cid = proj.base_slot.char_id
            entry = self._char_entry_by_id(base_cid)
            cur_bin = self.proj.current_bytes(entry)

            patches: dict[int, tuple[np.ndarray, dict[str, Any]]] = {}
            for f_info in proj.list_frames():
                f_idx = f_info["frame_index"]
                p_path = proj.get_frame_path(f_idx)
                if not p_path or not p_path.is_file():
                    continue
                with Image.open(p_path) as im:
                    pix = np.array(im, dtype=np.uint8)
                base_info, _, _ = self._decode_frame(base_cid, f_idx)
                patches[f_idx] = (
                    pix,
                    {
                        "w_tiles": base_info["w_tiles"],
                        "h_tiles": base_info["h_tiles"],
                        "pal_slot": base_info["pal_slot"],
                        "flg": base_info["flg"],
                    },
                )

            if patches:
                patched_bin, stats = M03.patch_pxx_bin(cur_bin, patches, entry["clean_path"])
                self.proj.save_bytes(entry, patched_bin)
            else:
                stats = {"mode": "no_custom_frames"}

            for b in BTN_NAMES:
                self._thumb_cache.pop((base_cid, b), None)

            proj.update_build(output_bin=entry["clean_path"], verified=True)
            return {
                "status": "ok",
                "injected_file": entry["clean_path"],
                "frames_injected": len(patches),
                "stats": stats,
                "rebuild_recommended": True,
                "message": f"Personagem injetado com sucesso no workspace: {entry['clean_path']}",
                "project": proj.to_dict(),
            }

    def forge_ai_chat(self, prompt: str, project_name: str | None = None) -> dict[str, Any]:
        with self.lock:
            assistant = ForgeAIAssistant(active_project=project_name)
            res = assistant.chat(prompt, context={"active_project": project_name})
            return res

    def export_forge_kofchar(self, name: str) -> bytes:
        with self.lock:
            proj = ForgeProject.load(name)
            pkg = proj.export_package()
            return pkg.read_bytes()

    def get_forge_frame_png(self, name: str, frame_idx: int) -> bytes:
        with self.lock:
            proj = ForgeProject.load(name)
            p = proj.get_frame_path(frame_idx)
            if p and p.is_file():
                return p.read_bytes()
            return self.get_frame_png(proj.base_slot.char_id, frame_idx, "A", "rgba")


# --------------------------------------------------------------------------- UI Web
HTML_APP = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>KOF 2002 (PS2) — Studio de Personagens & Disco</title>
<style>
  :root{
    --bg:#0d1017;--panel:#151a24;--panel2:#1c2331;--border:#2b3548;
    --text:#e6edf7;--muted:#8d9bb3;--accent:#ff6b35;--accent2:#38bdf8;
    --ok:#22c55e;--warn:#f59e0b;--err:#ef4444;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:Inter,Segoe UI,system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:#0a0d13;border-bottom:1px solid var(--border);flex-shrink:0}
  header h1{margin:0;font-size:16px;font-weight:700;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
  .pill{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--panel2);color:var(--accent2);border:1px solid var(--border)}
  .pill.mod{color:#fbbf24;border-color:#78350f;background:#2a1b08}
  .spacer{flex:1}
  button,.btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);padding:7px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;transition:.12s}
  button:hover{border-color:var(--accent2);background:#232d40}
  button.primary{background:var(--accent);border-color:#ff865a;color:#0b0e14}
  button.primary:hover{background:#ff7e4f}
  button.good{background:#15803d;border-color:#22c55e;color:#fff}
  button.danger{border-color:#7f1d1d;color:#fca5a5}
  button:disabled{opacity:.45;cursor:not-allowed}
  main{display:flex;flex:1;min-height:0}
  /* Barra lateral de personagens */
  aside{width:320px;border-right:1px solid var(--border);background:var(--panel);display:flex;flex-direction:column;min-height:0}
  .aside-head{padding:10px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-direction:column}
  input[type=text],input[type=search],select{background:#0d111a;border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:7px;font-size:12px;width:100%}
  .char-grid{flex:1;overflow-y:auto;padding:10px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-content:start}
  .char-card{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:8px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:6px;transition:.12s;position:relative}
  .char-card:hover{border-color:var(--accent2);transform:translateY(-1px)}
  .char-card.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);background:#231b1b}
  .char-card img{width:84px;height:84px;object-fit:contain;image-rendering:pixelated;background:repeating-conic-gradient(#131822 0% 25%,#181f2c 0% 50%) 50%/12px 12px;border-radius:6px}
  .char-card .cname{font-size:11px;font-weight:700;text-align:center;line-height:1.2}
  .char-card .cid{font-size:10px;color:var(--muted)}
  .badge-mod{position:absolute;top:5px;right:5px;font-size:9px;background:var(--warn);color:#111;padding:1px 5px;border-radius:99px;font-weight:800}

  /* Área principal */
  section.work{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
  .tabs{display:flex;gap:6px;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--panel)}
  .tab{padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;color:var(--muted);border:1px solid transparent}
  .tab.active{color:var(--text);background:var(--panel2);border-color:var(--border)}
  .tab-body{flex:1;min-height:0;overflow:auto;padding:14px;display:none}
  .tab-body.active{display:flex;flex-direction:column;gap:14px}

  .split{display:grid;grid-template-columns:1.1fr 1fr;gap:14px;flex:1;min-height:0}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:10px;min-height:0}
  .card h2{margin:0;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:space-between;gap:8px}
  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}

  /* Grid de frames */
  .frame-grid{flex:1;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:8px;align-content:start;padding-right:4px}
  .frame-item{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:6px;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer}
  .frame-item:hover{border-color:var(--accent2)}
  .frame-item.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .frame-item img{width:64px;height:64px;object-fit:contain;image-rendering:pixelated;background:repeating-conic-gradient(#12161f 0% 25%,#171d29 0% 50%) 50%/10px 10px;border-radius:4px}
  .frame-item span{font-size:10px;color:var(--muted)}

  /* Editor Canvas + Drag & Drop */
  .canvas-wrap{flex:1;min-height:260px;display:flex;align-items:center;justify-content:center;background:repeating-conic-gradient(#111620 0% 25%,#171e2b 0% 50%) 50%/16px 16px;border:2px dashed var(--border);border-radius:10px;overflow:auto;position:relative;padding:12px}
  .canvas-wrap.dragover{border-color:var(--accent);background:#251815}
  canvas#spriteCanvas{image-rendering:pixelated;cursor:crosshair;box-shadow:0 6px 24px rgba(0,0,0,.5)}
  .pal-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:6px}
  .swatch{display:flex;flex-direction:column;align-items:center;gap:3px;padding:4px;border-radius:7px;border:1px solid var(--border);background:var(--panel2);cursor:pointer}
  .swatch.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .swatch-box{width:28px;height:22px;border-radius:4px;border:1px solid rgba(255,255,255,.2)}
  .swatch input[type=color]{width:30px;height:20px;border:none;background:none;padding:0;cursor:pointer}
  .swatch span{font-size:10px;color:var(--muted)}
  .hint{font-size:11px;color:var(--muted)}
  pre.hex{margin:0;flex:1;overflow:auto;background:#0a0e14;border:1px solid var(--border);border-radius:8px;padding:10px;font-family:Consolas,monospace;font-size:11px;line-height:1.45}
  .toast{position:fixed;bottom:16px;right:16px;background:#101622;border:1px solid var(--accent2);padding:10px 14px;border-radius:10px;font-size:12px;box-shadow:0 10px 30px rgba(0,0,0,.5);z-index:99;max-width:460px;display:none}
  /* Character Forge Styles */
  .forge-wrap{display:grid;grid-template-columns:310px 1.2fr 380px;gap:12px;flex:1;min-height:0}
  .forge-badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700}
  .forge-badge.ok{background:#15803d;color:#86efac;border:1px solid #22c55e}
  .forge-badge.warn{background:#78350f;color:#fde047;border:1px solid #f59e0b}
  .forge-badge.err{background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444}
  .chip{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:3px 8px;border-radius:999px;font-size:10px;cursor:pointer;transition:.12s;white-space:nowrap}
  .chip:hover{border-color:var(--accent2);background:#232d40}
  .chat-box{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:8px;background:#0d111a;border:1px solid var(--border);border-radius:8px}
  .chat-msg{max-width:92%;padding:8px 10px;border-radius:8px;font-size:11px;line-height:1.4}
  .chat-msg.user{align-self:flex-end;background:#2563eb;color:#fff;border-bottom-right-radius:2px}
  .chat-msg.assistant{align-self:flex-start;background:var(--panel2);border:1px solid var(--border);border-bottom-left-radius:2px}
  .tool-card{background:#0a0e14;border:1px solid #3b82f6;border-radius:6px;padding:6px;font-family:Consolas,monospace;font-size:10px;color:#93c5fd;margin-top:4px}
  .check-item{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;font-size:11px}
  .check-item.pass{border-left:4px solid var(--ok)}
  .check-item.fail{border-left:4px solid var(--err)}
  .forge-proj-card{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:8px;cursor:pointer;display:flex;flex-direction:column;gap:4px;transition:.12s}
  .forge-proj-card:hover{border-color:var(--accent2)}
  .forge-proj-card.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
</style>
</head>
<body>
<header>
  <h1>KOF 2002 (PS2) <span class="pill">SLES-53381</span></h1>
  <span id="modBadge" class="pill mod">0 alterações</span>
  <span id="statusText" class="hint"></span>
  <button id="btnOpenForge" class="primary" style="background:linear-gradient(135deg, #8b5cf6, #6366f1);border-color:#a855f7;color:#fff;font-weight:700;display:flex;align-items:center;gap:6px">+ CRIAR PERSONAGEM</button>
  <div class="spacer"></div>
  <button id="btnRevertAll" class="danger">Reverter tudo</button>
  <button id="btnBuild" class="primary">Construir ISO (.bin/.cue)</button>
  <button id="btnLaunch" class="good">Abrir no PCSX2</button>
</header>

<main>
  <aside>
    <div class="aside-head">
      <input id="charSearch" type="search" placeholder="Buscar lutador (ex: Rugal, Kyo, P27)..." />
      <div class="row">
        <span class="hint">Botão paleta:</span>
        <select id="globalBtn" style="width:auto">
          <option value="A">Botão A</option>
          <option value="B">Botão B</option>
          <option value="C">Botão C</option>
          <option value="D">Botão D</option>
        </select>
        <span id="charCount" class="hint"></span>
      </div>
    </div>
    <div id="charGrid" class="char-grid"></div>
  </aside>

  <section class="work">
    <div class="tabs">
      <div class="tab active" data-tab="sprites">1. Sprites & Drag-and-Drop</div>
      <div class="tab" data-tab="palettes">2. Paletas ao Vivo (A/B/C/D)</div>
      <div class="tab" data-tab="files">3. Arquivos do Disco & Hex</div>
      <div class="tab" data-tab="forge" id="tabNavForge" style="color:#c084fc">✨ 4. Character Forge (Criador)</div>
    </div>

    <!-- TAB 1: SPRITES -->
    <div id="tab-sprites" class="tab-body active">
      <!-- PLAYER DE ANIMAÇÃO AO VIVO + GOLPES ESPECIAIS + SOM -->
      <div class="card" style="flex-shrink:0">
        <h2>
          <span>▶ Preview de Animação, Golpes Especiais & Som ao Vivo</span>
          <div class="row">
            <button id="btnPlayPause" class="primary">▶ Play</button>
            <button id="btnStepPrev">◀ Frame</button>
            <button id="btnStepNext">Frame ▶</button>
            <select id="seqSelect" style="width:250px"></select>
            <span class="hint">De</span>
            <input id="animStart" type="text" value="0" style="width:50px;text-align:center" />
            <span class="hint">até</span>
            <input id="animEnd" type="text" value="14" style="width:50px;text-align:center" />
            <label class="hint">FPS <input id="animFps" type="range" min="4" max="30" value="12" style="width:65px;vertical-align:middle" /> <span id="fpsVal">12</span></label>
            <label class="hint"><input id="chkPingPong" type="checkbox" /> Ping-Pong</label>
            <label class="hint"><input id="chkSound" type="checkbox" checked /> 🔊 Som</label>
            <button id="btnEditPlaying">Editar frame atual</button>
          </div>
        </h2>
        <!-- Botões rápidos de Especial 1, 2, 3, SDM, MAX2 para todos os personagens -->
        <div id="specialBtnsBar" class="row" style="gap:6px;padding:2px 0"></div>
        <div class="row" style="gap:14px;align-items:stretch">
          <div class="canvas-wrap" style="min-height:190px;max-height:210px;flex:1;border-style:solid">
            <canvas id="animStageCanvas" width="380" height="185" style="image-rendering:pixelated"></canvas>
          </div>
          <div style="width:290px;display:flex;flex-direction:column;justify-content:center;gap:7px">
            <div id="animFrameLabel" style="font-size:13px;font-weight:700;color:var(--accent2)">Frame #0</div>
            <div id="animSeqInfo" class="hint">Clique em Especial 1, 2, 3, Super SDM ou MAX2 acima para ver o golpe com voz e efeito sonoro!</div>
            <div class="row">
              <select id="voiceSelect" style="flex:1"></select>
              <button id="btnPlayVoice">🔊 Ouvir</button>
            </div>
            <div class="hint">Troque o <b>Botão paleta (A/B/C/D)</b> na barra lateral para ver a animação com cada roupa em tempo real.</div>
          </div>
        </div>
      </div>

      <div class="split">
        <div class="card">
          <h2>
            <span id="framesTitle">Frames do personagem</span>
            <div class="row">
              <button id="btnPrevPage">&lt;</button>
              <span id="pageLabel" class="hint">1/1</span>
              <button id="btnNextPage">&gt;</button>
            </div>
          </h2>
          <div class="row">
            <input id="frameFilter" type="search" placeholder="Filtrar frame # (ex: 0, 12, 100) ou 'editaveis'" style="flex:1" />
            <button id="btnRevertChar" class="danger">Reverter BIN</button>
          </div>
          <div id="frameGrid" class="frame-grid"></div>
        </div>

        <div class="card">
          <h2>
            <span id="editorTitle">Selecione um frame</span>
            <div class="row">
              <label class="hint">Zoom <input id="zoomRange" type="range" min="1" max="8" value="3" style="width:80px;vertical-align:middle" /></label>
              <button id="btnDownloadPng">Baixar PNG</button>
              <button id="btnUploadPick">Enviar imagem</button>
              <input id="fileInput" type="file" accept="image/*" style="display:none" />
              <button id="btnSaveFrame" class="primary">Salvar Frame</button>
            </div>
          </h2>
          <div id="dropZone" class="canvas-wrap">
            <canvas id="spriteCanvas" width="128" height="128"></canvas>
          </div>
          <div class="row" style="justify-content:space-between">
            <span class="hint">Clique/arraste no canvas para pintar. <b>Ou arraste qualquer PNG/JPG/WEBP aqui</b> para substituir o sprite automaticamente!</span>
            <label class="hint"><input id="chkMagenta" type="checkbox" checked /> Remover fundo magenta</label>
          </div>
          <div class="row">
            <span class="hint">Cor ativa (0 = transparente):</span>
            <button id="btnUndoPixel">Desfazer desenho</button>
          </div>
          <div id="drawPalette" class="pal-grid"></div>
        </div>
      </div>
    </div>

    <!-- TAB 2: PALETAS -->
    <div id="tab-palettes" class="tab-body">
      <div class="card">
        <h2>
          <span id="palTitle">Editor de Paleta (FIRST.GRC)</span>
          <div class="row">
            <button id="btnRevertPal" class="danger">Reverter Paleta deste Lutador</button>
            <button id="btnSavePal" class="primary">Salvar Paleta no FIRST.GRC</button>
          </div>
        </h2>
        <div class="hint">Clique em qualquer cor (índices 1..15) para mudar o uniforme/pele/efeito em tempo real no preview abaixo. Índice 0 é transparência.</div>
        <div id="palEditorGrid" class="pal-grid" style="grid-template-columns:repeat(8,minmax(90px,1fr))"></div>
      </div>
      <div class="card" style="flex:1">
        <h2><span>Preview ao vivo no frame atual</span><span id="palIndexInfo" class="hint"></span></h2>
        <div class="canvas-wrap"><canvas id="palPreviewCanvas" width="128" height="128"></canvas></div>
      </div>
    </div>

    <!-- TAB 3: ARQUIVOS -->
    <div id="tab-files" class="tab-body">
      <div class="split">
        <div class="card">
          <h2>
            <span>Arquivos do Disco (392)</span>
            <label class="hint"><input id="chkOnlyMod" type="checkbox" /> Só modificados</label>
          </h2>
          <input id="fileSearch" type="search" placeholder="Filtrar caminho (ex: SLES, FIRST, P27, BGM)..." />
          <div id="fileList" style="flex:1;overflow:auto;display:flex;flex-direction:column;gap:4px"></div>
        </div>
        <div class="card">
          <h2>
            <span id="hexTitle">Selecione um arquivo</span>
            <div class="row">
              <button id="btnHexPrev">&lt; Pág</button>
              <span id="hexPageInfo" class="hint"></span>
              <button id="btnHexNext">Pág &gt;</button>
              <button id="btnFileRevert" class="danger">Reverter</button>
            </div>
          </h2>
          <div class="row">
            <input id="patchOffset" type="text" placeholder="Offset (ex: 0x3E6960)" style="width:160px" />
            <input id="patchHex" type="text" placeholder="Bytes hex (ex: DE AD BE EF)" style="flex:1" />
            <button id="btnApplyPatch" class="primary">Aplicar Patch</button>
          </div>
          <pre id="hexView" class="hex"></pre>
        </div>
      </div>
    </div>

    <!-- TAB 4: CHARACTER FORGE -->
    <div id="tab-forge" class="tab-body">
      <div class="forge-wrap">
        <!-- COLUNA ESQUERDA: WIZARD MODO B + PROJETOS -->
        <div style="display:flex;flex-direction:column;gap:12px;min-height:0">
          <div class="card">
            <h2>
              <span>✨ Criar Novo Personagem</span>
              <span class="forge-badge ok">Modo B: Base</span>
            </h2>
            <div class="hint">Clone um lutador existente para herdar moveset, física e colisões, personalizando aparência e sprites.</div>
            <label class="hint" style="margin-top:4px">Lutador Base (Slot original):
              <select id="forgeBaseSelect" style="margin-top:3px"></select>
            </label>
            <label class="hint">Nome do Novo Personagem:
              <input id="forgeProjName" type="text" value="Terry Legendary" placeholder="Ex: Terry Legendary" style="margin-top:3px" />
            </label>
            <label class="hint">Autor / Modder:
              <input id="forgeProjAuthor" type="text" value="KOF Modder" placeholder="Ex: KOF Modder" style="margin-top:3px" />
            </label>
            <label class="hint">Descrição:
              <input id="forgeProjDesc" type="text" value="Versão Lendária baseada em Terry Bogard" placeholder="Descrição do personagem" style="margin-top:3px" />
            </label>
            <button id="btnCreateForgeProj" class="primary" style="background:linear-gradient(135deg, #8b5cf6, #6366f1);border-color:#a855f7;color:#fff;margin-top:4px">🚀 Criar Projeto no Forge</button>
          </div>

          <div class="card" style="flex:1;min-height:0">
            <h2>
              <span>Projetos em workspace/forge/</span>
              <button id="btnRefreshProjects" style="padding:2px 8px;font-size:11px">🔄</button>
            </h2>
            <div class="row">
              <button id="btnImportKofchar" style="flex:1">📥 Importar .kofchar</button>
              <input id="fileKofcharInput" type="file" accept=".kofchar,.zip" style="display:none" />
            </div>
            <div id="forgeProjectsList" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-top:4px"></div>
          </div>
        </div>

        <!-- COLUNA MEIO: STUDIO DO PROJETO ATIVO + VALIDAÇÃO + SPRITES -->
        <div style="display:flex;flex-direction:column;gap:12px;min-height:0">
          <div class="card">
            <h2>
              <span id="activeProjTitle">Nenhum projeto selecionado</span>
              <div class="row">
                <span id="activeProjBadge" class="forge-badge warn">NÃO SELECIONADO</span>
                <button id="btnForgeValidate" class="primary" style="background:#2563eb;border-color:#3b82f6">🔍 Validar Personagem</button>
                <button id="btnForgeInject" class="good">⚡ Injetar na ISO</button>
                <button id="btnForgeExport">📦 Exportar .kofchar</button>
              </div>
            </h2>
            <div id="activeProjMeta" class="hint">Crie um projeto acima ou selecione um projeto existente na lista.</div>
          </div>

          <!-- PAINEL DE RELATÓRIO DE VALIDAÇÃO -->
          <div class="card">
            <h2><span>📋 Relatório de Conformidade & Validação LBA-Safe</span></h2>
            <div id="validationChecklist" style="display:flex;flex-direction:column;gap:6px">
              <div class="check-item" id="chkSlot"><span>1. Slot Base & Arquivo PXX.BIN</span><span class="forge-badge ok">AGUARDANDO</span></div>
              <div class="check-item" id="chkDims"><span>2. Dimensões & Grade de Tiles (16x16)</span><span class="forge-badge ok">AGUARDANDO</span></div>
              <div class="check-item" id="chkPal"><span>3. Paleta 16 Cores & Transparência Índice 0</span><span class="forge-badge ok">AGUARDANDO</span></div>
              <div class="check-item" id="chkMem"><span>4. Orçamento de Memória e LBA Seguro</span><span class="forge-badge ok">AGUARDANDO</span></div>
            </div>
            <div id="valLogDetails" class="hint" style="margin-top:4px;background:#0d111a;padding:8px;border-radius:6px;border:1px solid var(--border)">Clique em 'Validar Personagem' para executar a inspeção determinística.</div>
          </div>

          <!-- SPRITES PERSONALIZADOS DO PROJETO -->
          <div class="card" style="flex:1;min-height:0">
            <h2>
              <span>🖼️ Importador de Sprites do Projeto</span>
              <div class="row">
                <span class="hint">Frame #:</span>
                <input id="forgeFrameIdx" type="number" min="0" max="650" value="0" style="width:65px;text-align:center" />
                <button id="btnForgePickSprite">Carregar Imagem</button>
                <input id="forgeSpriteInput" type="file" accept="image/*" style="display:none" />
                <button id="btnForgeImportSprite" class="primary">Importar para o Projeto</button>
              </div>
            </h2>
            <div class="row" style="gap:14px;align-items:center">
              <label class="hint"><input id="chkForgeMagenta" type="checkbox" checked /> Remover magenta (#FF00FF)</label>
              <label class="hint"><input id="chkForgeAutoFit" type="checkbox" checked /> Auto-fit (encaixar no corpo)</label>
              <div class="spacer"></div>
              <span id="forgeCustomCount" class="hint">0 frames personalizados</span>
            </div>
            <div id="forgeFramesGrid" style="flex:1;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:8px;align-content:start;margin-top:6px"></div>
          </div>
        </div>

        <!-- COLUNA DIREITA: FORGE AI ASSISTANT SIDEBAR -->
        <div class="card" style="display:flex;flex-direction:column;gap:10px;min-height:0">
          <h2>
            <span>🤖 Forge AI Assistant</span>
            <span class="pill" style="color:#22c55e;border-color:#15803d">Online • LBA-Safe</span>
          </h2>
          <div class="row" style="gap:4px">
            <span class="chip" onclick="quickPrompt('Crie um lutador baseado no Terry')">✨ Crie lutador (Terry)</span>
            <span class="chip" onclick="quickPrompt('Valide o personagem')">🔍 Validar</span>
            <span class="chip" onclick="quickPrompt('Injete na ISO')">⚡ Injetar</span>
            <span class="chip" onclick="quickPrompt('Listar projetos')">📋 Listar</span>
          </div>
          <div id="forgeChatBox" class="chat-box">
            <div class="chat-msg assistant">
              👋 <b>Olá! Eu sou o Forge AI Assistant.</b><br/>
              Posso criar lutadores baseados em slots existentes, importar frames, executar validações de conformidade LBA e injetar modificações com segurança.<br/>
              <i>Experimente clicar em uma das sugestões acima ou digite uma instrução!</i>
            </div>
          </div>
          <div class="row" style="gap:6px">
            <input id="forgeChatInput" type="text" placeholder="Ex: Crie um lutador baseado no Terry..." style="flex:1" />
            <button id="btnForgeChatSend" class="primary" style="background:#8b5cf6;border-color:#a855f7">Enviar</button>
          </div>
          <details style="font-size:11px;color:var(--muted)">
            <summary style="cursor:pointer;font-weight:700">📜 Action Log (Execução de Ferramentas)</summary>
            <div id="forgeActionLog" style="max-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;margin-top:6px;background:#0a0d13;padding:6px;border-radius:6px;border:1px solid var(--border)">
              <div class="hint">Nenhuma ação executada ainda.</div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </section>
</main>
<div id="toast" class="toast"></div>

<script>
const $ = s => document.querySelector(s);
const state = {
  chars: [],
  cid: 0x27,
  btn: 'A',
  frames: [],
  page: 0,
  perPage: 48,
  frameIdx: 0,
  frameData: null,
  pixels: null,
  origPixels: null,
  activeColor: 1,
  palettes: null,
  liveHex: [],
  selectedFile: null,
  hexPage: 0,
};

function toast(msg, isErr=false){
  const t = $('#toast');
  t.textContent = msg;
  t.style.borderColor = isErr ? 'var(--err)' : 'var(--accent2)';
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(()=>t.style.display='none', 4500);
}

async function api(url, opts={}){
  const res = await fetch(url, opts);
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if(!res.ok) throw new Error(data.error || data || res.statusText);
  return data;
}

// Tabs
document.querySelectorAll('.tab').forEach(tab=>{
  tab.onclick = ()=>{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab-body').forEach(b=>b.classList.remove('active'));
    tab.classList.add('active');
    $('#tab-'+tab.dataset.tab).classList.add('active');
    if(tab.dataset.tab==='palettes') renderPaletteEditor();
    if(tab.dataset.tab==='files') loadFiles();
    if(tab.dataset.tab==='forge'){ populateForgeBaseSelect(); loadForgeProjects(); }
  };
});

async function loadCharacters(){
  const res = await api('/api/characters');
  state.chars = res.characters;
  $('#modBadge').textContent = `${res.modified_files_count} arquivo(s) modificado(s)`;
  renderCharGrid();
}

function renderCharGrid(){
  const q = $('#charSearch').value.trim().toLowerCase();
  const grid = $('#charGrid');
  grid.innerHTML = '';
  const list = state.chars.filter(c => !q || c.name.toLowerCase().includes(q) || c.hex_id.toLowerCase().includes(q));
  $('#charCount').textContent = `${list.length} lutadores`;
  for(const c of list){
    const d = document.createElement('div');
    d.className = 'char-card' + (c.cid===state.cid?' active':'');
    d.onclick = ()=>selectChar(c.cid);
    const mod = c.bin_modified ? '<span class="badge-mod">MOD</span>' : '';
    d.innerHTML = `${mod}<img loading="lazy" src="/api/character/${c.cid}/thumb.png?btn=${state.btn}&v=${Date.now()%1e6}" /><div class="cname">${c.name}</div><div class="cid">${c.hex_id} • ${c.num_frames} frames</div>`;
    grid.appendChild(d);
  }
}

async function selectChar(cid){
  state.cid = cid;
  state.page = 0;
  renderCharGrid();
  const [fr, pal, seq, vc] = await Promise.all([
    api(`/api/character/${cid}/frames`),
    api(`/api/character/${cid}/palettes`),
    api(`/api/character/${cid}/sequences`),
    api(`/api/character/${cid}/voices`),
  ]);
  state.frames = fr.frames;
  state.palettes = pal;
  state.sequences = seq.sequences || [];
  state.specials = seq.specials || [];
  state.voices = vc.clips || [];
  const ch = state.chars.find(x=>x.cid===cid);
  $('#framesTitle').textContent = `${ch ? ch.name : fr.hex_id} (${fr.total_slots} slots • ${vc.acx_name || 'sem ACX'})`;
  renderSeqSelect();
  renderSpecialButtons();
  renderVoiceSelect();
  const firstEditable = state.frames.find(f=>f.editable) || state.frames[0];
  renderFramesGrid();
  renderPaletteEditor();
  if(firstEditable) selectFrame(firstEditable.frame_index);
}

// ---------------- Player de Animação ao Vivo + Especiais + Som ----------------
const anim = {
  playing: true,
  timer: null,
  cursor: 0,
  dir: 1,
  fxRange: [],
  activePresetId: 'idle',
  imgCache: new Map(),
};

function playClipAudio(clipIdx, delayMs=0){
  if(clipIdx===null || clipIdx===undefined || !$('#chkSound').checked) return;
  const run = ()=>{
    const a = new Audio(`/api/character/${state.cid}/voice/${clipIdx}.wav`);
    a.volume = 0.85;
    a.play().catch(()=>{});
  };
  if(delayMs > 0) setTimeout(run, delayMs);
  else run();
}

function renderVoiceSelect(){
  const sel = $('#voiceSelect');
  sel.innerHTML = '';
  for(const c of state.voices){
    const opt = document.createElement('option');
    opt.value = String(c.clip_index);
    opt.textContent = c.label;
    sel.appendChild(opt);
  }
}

$('#btnPlayVoice').onclick = ()=>{
  const idx = parseInt($('#voiceSelect').value, 10);
  if(!isNaN(idx)){
    const a = new Audio(`/api/character/${state.cid}/voice/${idx}.wav`);
    a.play().catch(()=>{});
  }
};

function renderSpecialButtons(){
  const bar = $('#specialBtnsBar');
  bar.innerHTML = '';
  for(const sp of state.specials){
    const b = document.createElement('button');
    if(sp.id === anim.activePresetId) b.className = 'primary';
    b.textContent = sp.title;
    b.onclick = ()=>triggerSpecialPreset(sp);
    bar.appendChild(b);
  }
}

function triggerSpecialPreset(sp){
  anim.activePresetId = sp.id;
  renderSpecialButtons();
  $('#animStart').value = sp.start_frame;
  $('#animEnd').value = sp.end_frame;
  if(sp.fx_start !== null && sp.fx_end !== null){
    anim.fxRange = state.frames.filter(f => !f.is_stub && f.frame_index >= sp.fx_start && f.frame_index <= sp.fx_end).map(f => f.frame_index);
  }else{
    anim.fxRange = [];
  }
  const fxNote = anim.fxRange.length ? ` + Efeito FX [#${sp.fx_start}..#${sp.fx_end}]` : '';
  $('#animSeqInfo').textContent = `${sp.title}: Frames #${sp.start_frame}..#${sp.end_frame}${fxNote}`;
  anim.cursor = 0;
  anim.dir = 1;
  anim.playing = true;
  preloadAnimRange();
  restartAnimLoop();
  drawAnimTick(false);
  if(sp.voice_clip !== null){
    $('#voiceSelect').value = String(sp.voice_clip);
    playClipAudio(sp.voice_clip, 0);
  }
  if(sp.sfx_clip !== null){
    playClipAudio(sp.sfx_clip, 140);
  }
}

function getAnimRange(){
  let s = parseInt($('#animStart').value, 10);
  let e = parseInt($('#animEnd').value, 10);
  if(isNaN(s)) s = 0;
  if(isNaN(e)) e = s;
  if(e < s){ const t = s; s = e; e = t; }
  const valid = state.frames.filter(f => !f.is_stub && f.frame_index >= s && f.frame_index <= e).map(f => f.frame_index);
  return valid.length ? valid : [0];
}

function renderSeqSelect(){
  const sel = $('#seqSelect');
  sel.innerHTML = '';
  state.sequences.forEach((sq, i)=>{
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = sq.label;
    sel.appendChild(opt);
  });
  if(state.sequences.length){
    sel.value = '0';
    applySequence(0);
  }
}

function applySequence(idx){
  const sq = state.sequences[idx];
  if(!sq) return;
  anim.fxRange = [];
  $('#animStart').value = sq.start_frame;
  $('#animEnd').value = sq.end_frame;
  $('#animSeqInfo').textContent = `${sq.label} • Paleta slot 0x${sq.pal_slot.toString(16)}`;
  anim.cursor = 0;
  anim.dir = 1;
  preloadAnimRange();
  drawAnimTick(false);
}

function preloadAnimRange(){
  const list = [...getAnimRange(), ...(anim.fxRange || [])];
  for(const fidx of list){
    const key = `${state.cid}:${state.btn}:${fidx}`;
    if(!anim.imgCache.has(key)){
      const im = new Image();
      im.src = `/api/character/${state.cid}/frame/${fidx}.png?btn=${state.btn}`;
      im.onload = ()=>{ if(getAnimRange()[anim.cursor]===fidx) renderStageFrame(fidx, im, null); };
      anim.imgCache.set(key, im);
    }
  }
}

function renderStageFrame(fidx, im, fxIm){
  const c = $('#animStageCanvas');
  const ctx = c.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, c.width, c.height);
  // Linha de chão do palco arcade
  const floorY = c.height - 18;
  ctx.strokeStyle = '#2b3548';
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(20, floorY); ctx.lineTo(c.width - 20, floorY); ctx.stroke();
  const baseX = anim.fxRange && anim.fxRange.length ? Math.round(c.width * 0.36) : Math.round(c.width * 0.5);
  if(im && im.complete && im.naturalWidth > 0){
    const scale = Math.min(1.35, (c.height - 26) / Math.max(80, im.naturalHeight));
    const dw = Math.round(im.naturalWidth * scale);
    const dh = Math.round(im.naturalHeight * scale);
    const dx = Math.round(baseX - dw / 2);
    const dy = Math.round(floorY - dh);
    ctx.drawImage(im, dx, dy, dw, dh);
  }
  if(fxIm && fxIm.complete && fxIm.naturalWidth > 0){
    const scale = Math.min(1.25, (c.height - 26) / Math.max(80, fxIm.naturalHeight));
    const fw = Math.round(fxIm.naturalWidth * scale);
    const fh = Math.round(fxIm.naturalHeight * scale);
    const fxX = Math.round(c.width * 0.64 - fw / 2);
    const fxY = Math.round(floorY - fh - 8);
    ctx.drawImage(fxIm, fxX, Math.max(4, fxY), fw, fh);
  }
  $('#animFrameLabel').textContent = `Frame #${fidx}`;
}

function drawAnimTick(advance=true){
  const list = getAnimRange();
  if(!list.length) return;
  if(anim.cursor >= list.length) anim.cursor = 0;
  const fidx = list[anim.cursor];
  const key = `${state.cid}:${state.btn}:${fidx}`;
  let im = anim.imgCache.get(key);
  if(!im){
    im = new Image();
    im.src = `/api/character/${state.cid}/frame/${fidx}.png?btn=${state.btn}`;
    anim.imgCache.set(key, im);
  }
  let fxIm = null;
  if(anim.fxRange && anim.fxRange.length){
    const fxIdx = anim.fxRange[anim.cursor % anim.fxRange.length];
    const fxKey = `${state.cid}:${state.btn}:${fxIdx}`;
    fxIm = anim.imgCache.get(fxKey);
    if(!fxIm){
      fxIm = new Image();
      fxIm.src = `/api/character/${state.cid}/frame/${fxIdx}.png?btn=${state.btn}`;
      anim.imgCache.set(fxKey, fxIm);
    }
  }
  renderStageFrame(fidx, im, fxIm);
  if(advance && list.length > 1){
    if($('#chkPingPong').checked){
      anim.cursor += anim.dir;
      if(anim.cursor >= list.length - 1){ anim.cursor = list.length - 1; anim.dir = -1; }
      else if(anim.cursor <= 0){ anim.cursor = 0; anim.dir = 1; }
    }else{
      anim.cursor = (anim.cursor + 1) % list.length;
    }
  }
}

function restartAnimLoop(){
  clearInterval(anim.timer);
  const fps = Number($('#animFps').value) || 12;
  $('#fpsVal').textContent = String(fps);
  $('#btnPlayPause').textContent = anim.playing ? '⏸ Pause' : '▶ Play';
  if(anim.playing){
    anim.timer = setInterval(()=>drawAnimTick(true), Math.round(1000 / fps));
  }
}

function filteredFrames(){
  const q = $('#frameFilter').value.trim().toLowerCase();
  return state.frames.filter(f=>{
    if(!q) return !f.is_stub;
    if(q==='editaveis') return f.editable;
    return String(f.frame_index).includes(q);
  });
}

function renderFramesGrid(){
  const list = filteredFrames();
  const pages = Math.max(1, Math.ceil(list.length / state.perPage));
  state.page = Math.max(0, Math.min(state.page, pages-1));
  $('#pageLabel').textContent = `${state.page+1}/${pages} (${list.length})`;
  const grid = $('#frameGrid');
  grid.innerHTML = '';
  const slice = list.slice(state.page*state.perPage, (state.page+1)*state.perPage);
  for(const f of slice){
    const el = document.createElement('div');
    el.className = 'frame-item' + (f.frame_index===state.frameIdx?' active':'');
    el.onclick = ()=>selectFrame(f.frame_index);
    el.innerHTML = `<img loading="lazy" src="/api/character/${state.cid}/frame/${f.frame_index}.png?btn=${state.btn}" /><span>#${f.frame_index} (${f.width_px}x${f.height_px})</span>`;
    grid.appendChild(el);
  }
}

async function selectFrame(idx){
  state.frameIdx = idx;
  const d = await api(`/api/character/${state.cid}/frame/${idx}/data?btn=${state.btn}`);
  state.frameData = d;
  if(d.is_stub){
    state.pixels = null;
    $('#editorTitle').textContent = `Frame #${idx} (stub vazio)`;
    return;
  }
  const raw = atob(d.pixels_b64);
  const arr = new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) arr[i]=raw.charCodeAt(i);
  state.pixels = arr;
  state.origPixels = new Uint8Array(arr);
  $('#editorTitle').textContent = `Frame #${idx} — ${d.width_px}×${d.height_px}px (pal 0x${d.pal_idx.toString(16)})`;
  renderFramesGrid();
  renderDrawPalette();
  drawSpriteCanvas(state.frameData.rgba);
}

function drawSpriteCanvas(rgbaOverride=null){
  const d = state.frameData;
  if(!d || d.is_stub || !state.pixels) return;
  const zoom = Number($('#zoomRange').value) || 3;
  const rgba = rgbaOverride || d.rgba;
  for(const cidCanvas of ['#spriteCanvas', '#palPreviewCanvas']){
    const c = $(cidCanvas);
    c.width = d.width_px;
    c.height = d.height_px;
    c.style.width = (d.width_px * zoom) + 'px';
    c.style.height = (d.height_px * zoom) + 'px';
    const ctx = c.getContext('2d');
    const img = ctx.createImageData(d.width_px, d.height_px);
    for(let i=0;i<state.pixels.length;i++){
      const col = rgba[state.pixels[i]] || [0,0,0,0];
      const o = i*4;
      img.data[o]=col[0]; img.data[o+1]=col[1]; img.data[o+2]=col[2]; img.data[o+3]=col[3];
    }
    ctx.putImageData(img, 0, 0);
  }
}

function renderDrawPalette(){
  const d = state.frameData;
  if(!d) return;
  const box = $('#drawPalette');
  box.innerHTML = '';
  d.rgba.forEach((c, idx)=>{
    const el = document.createElement('div');
    el.className = 'swatch' + (state.activeColor===idx?' active':'');
    el.onclick = ()=>{ state.activeColor=idx; renderDrawPalette(); };
    const bg = idx===0 ? 'repeating-conic-gradient(#333 0% 25%,#555 0% 50%) 50%/8px 8px' : `rgb(${c[0]},${c[1]},${c[2]})`;
    el.innerHTML = `<div class="swatch-box" style="background:${bg}"></div><span>${idx===0?'0 (Transp)':idx}</span>`;
    box.appendChild(el);
  });
}

// Pintura no Canvas
let painting = false;
function paintAt(ev){
  if(!state.pixels || !state.frameData || state.frameData.is_stub) return;
  const c = $('#spriteCanvas');
  const rect = c.getBoundingClientRect();
  const x = Math.floor(((ev.clientX - rect.left) / rect.width) * state.frameData.width_px);
  const y = Math.floor(((ev.clientY - rect.top) / rect.height) * state.frameData.height_px);
  if(x<0 || y<0 || x>=state.frameData.width_px || y>=state.frameData.height_px) return;
  state.pixels[y * state.frameData.width_px + x] = state.activeColor;
  drawSpriteCanvas();
}
$('#spriteCanvas').onmousedown = ev=>{ painting=true; paintAt(ev); };
window.addEventListener('mouseup', ()=>painting=false);
$('#spriteCanvas').onmousemove = ev=>{ if(painting) paintAt(ev); };
$('#zoomRange').oninput = ()=>drawSpriteCanvas();
$('#btnUndoPixel').onclick = ()=>{ if(state.origPixels){ state.pixels = new Uint8Array(state.origPixels); drawSpriteCanvas(); } };

// Drag and Drop de imagem
const dz = $('#dropZone');
['dragenter','dragover'].forEach(e=>dz.addEventListener(e, ev=>{ ev.preventDefault(); dz.classList.add('dragover'); }));
['dragleave','drop'].forEach(e=>dz.addEventListener(e, ev=>{ ev.preventDefault(); dz.classList.remove('dragover'); }));
dz.addEventListener('drop', ev=>{
  const f = ev.dataTransfer.files[0];
  if(f) uploadFrameFile(f);
});
$('#btnUploadPick').onclick = ()=>$('#fileInput').click();
$('#fileInput').onchange = ev=>{ if(ev.target.files[0]) uploadFrameFile(ev.target.files[0]); ev.target.value=''; };

async function uploadFrameFile(file){
  const reader = new FileReader();
  reader.onload = async ()=>{
    try{
      const res = await api(`/api/character/${state.cid}/frame/${state.frameIdx}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          image_base64: reader.result,
          btn: state.btn,
          remove_magenta: $('#chkMagenta').checked,
          auto_fit: true,
        }),
      });
      toast(`Frame #${state.frameIdx} substituído (${res.stats.mode})!`);
      await selectFrame(state.frameIdx);
      await loadCharacters();
    }catch(ex){ toast(ex.message, true); }
  };
  reader.readAsDataURL(file);
}

$('#btnSaveFrame').onclick = async ()=>{
  if(!state.pixels) return;
  try{
    const res = await api(`/api/character/${state.cid}/frame/${state.frameIdx}`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ pixels: Array.from(state.pixels), btn: state.btn }),
    });
    toast(`Frame #${state.frameIdx} salvo (${res.stats.mode})!`);
    await selectFrame(state.frameIdx);
    await loadCharacters();
  }catch(ex){ toast(ex.message, true); }
};

$('#btnDownloadPng').onclick = ()=>{
  window.open(`/api/character/${state.cid}/frame/${state.frameIdx}.png?btn=${state.btn}&mode=indexed`, '_blank');
};

$('#btnRevertChar').onclick = async ()=>{
  try{
    const r = await api(`/api/character/${state.cid}/revert`, {method:'POST'});
    toast(r.status);
    await loadCharacters();
    await selectChar(state.cid);
  }catch(ex){ toast(ex.message, true); }
};

// Editor de Paletas ao vivo
function renderPaletteEditor(){
  if(!state.palettes) return;
  const p = state.palettes.buttons[state.btn];
  $('#palTitle').textContent = `Paleta Botão ${state.btn} — ${state.palettes.hex_id} (índice ${p.pal_idx_hex})`;
  $('#palIndexInfo').textContent = `FIRST.GRC slot ${p.pal_idx_hex}`;
  state.liveHex = [...p.hex];
  const grid = $('#palEditorGrid');
  grid.innerHTML = '';
  state.liveHex.forEach((hx, idx)=>{
    const el = document.createElement('div');
    el.className = 'swatch';
    if(idx===0){
      el.innerHTML = `<div class="swatch-box" style="background:#f0f"></div><span>0 (Transp)</span>`;
    }else{
      el.innerHTML = `<input type="color" value="${hx}" data-i="${idx}" /><span>Cor ${idx} (${hx})</span>`;
      el.querySelector('input').oninput = ev=>{
        state.liveHex[idx] = ev.target.value;
        el.querySelector('span').textContent = `Cor ${idx} (${ev.target.value})`;
        livePreviewPalette();
      };
    }
    grid.appendChild(el);
  });
  livePreviewPalette();
}

function livePreviewPalette(){
  const rgba = state.liveHex.map((hx, idx)=>{
    if(idx===0) return [0,0,0,0];
    const h = hx.replace('#','');
    return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16), 255];
  });
  drawSpriteCanvas(rgba);
}

$('#btnSavePal').onclick = async ()=>{
  try{
    const res = await api(`/api/character/${state.cid}/palettes`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ btn: state.btn, colors: state.liveHex }),
    });
    toast(`Paleta ${state.btn} salva no FIRST.GRC (${res.status})!`);
    state.palettes = await api(`/api/character/${state.cid}/palettes`);
    await selectFrame(state.frameIdx);
    await loadCharacters();
  }catch(ex){ toast(ex.message, true); }
};

$('#btnRevertPal').onclick = async ()=>{
  try{
    const res = await api(`/api/character/${state.cid}/palettes/revert`, {method:'POST'});
    toast(res.status);
    state.palettes = await api(`/api/character/${state.cid}/palettes`);
    renderPaletteEditor();
    await selectFrame(state.frameIdx);
    await loadCharacters();
  }catch(ex){ toast(ex.message, true); }
};

// Arquivos & Hex
async function loadFiles(){
  const q = encodeURIComponent($('#fileSearch').value);
  const om = $('#chkOnlyMod').checked ? 1 : 0;
  const res = await api(`/api/files?q=${q}&only_mod=${om}`);
  const box = $('#fileList');
  box.innerHTML = '';
  for(const f of res.files.slice(0, 200)){
    const b = document.createElement('button');
    b.style.textAlign='left';
    b.textContent = `${f.modified?'[MOD] ':''}${f.clean_path} (${f.size_bytes.toLocaleString()} B, LBA ${f.lba_start})`;
    b.onclick = ()=>{ state.selectedFile = f.clean_path; state.hexPage=0; loadHex(); };
    box.appendChild(b);
  }
}

async function loadHex(){
  if(!state.selectedFile) return;
  const d = await api(`/api/file/hex?path=${encodeURIComponent(state.selectedFile)}&page=${state.hexPage}`);
  $('#hexTitle').textContent = `${d.clean_path} (${d.size_bytes.toLocaleString()} B)`;
  $('#hexPageInfo').textContent = `${d.page+1}/${d.total_pages}`;
  $('#hexView').textContent = d.hexdump;
}

$('#btnHexPrev').onclick = ()=>{ if(state.hexPage>0){ state.hexPage--; loadHex(); } };
$('#btnHexNext').onclick = ()=>{ state.hexPage++; loadHex(); };
$('#btnApplyPatch').onclick = async ()=>{
  if(!state.selectedFile) return toast('Selecione um arquivo primeiro', true);
  try{
    const res = await api('/api/file/patch', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ path: state.selectedFile, offset: $('#patchOffset').value, hex: $('#patchHex').value }),
    });
    toast(res.status);
    await loadHex(); await loadFiles(); await loadCharacters();
  }catch(ex){ toast(ex.message, true); }
};
$('#btnFileRevert').onclick = async ()=>{
  if(!state.selectedFile) return;
  const res = await api('/api/file/revert', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path: state.selectedFile}),
  });
  toast(res.status);
  await loadHex(); await loadFiles(); await loadCharacters();
};

// Controles globais
$('#charSearch').oninput = renderCharGrid;
$('#frameFilter').oninput = ()=>{ state.page=0; renderFramesGrid(); };
$('#btnPrevPage').onclick = ()=>{ if(state.page>0){ state.page--; renderFramesGrid(); } };
$('#btnNextPage').onclick = ()=>{ state.page++; renderFramesGrid(); };
$('#globalBtn').onchange = async ev=>{
  state.btn = ev.target.value;
  anim.imgCache.clear();
  renderCharGrid();
  renderFramesGrid();
  renderPaletteEditor();
  preloadAnimRange();
  drawAnimTick(false);
  await selectFrame(state.frameIdx);
};
$('#seqSelect').onchange = ev=>applySequence(Number(ev.target.value));
$('#animStart').onchange = ()=>{ anim.cursor=0; preloadAnimRange(); drawAnimTick(false); };
$('#animEnd').onchange = ()=>{ anim.cursor=0; preloadAnimRange(); drawAnimTick(false); };
$('#animFps').oninput = ()=>restartAnimLoop();
$('#btnPlayPause').onclick = ()=>{ anim.playing = !anim.playing; restartAnimLoop(); };
$('#btnStepPrev').onclick = ()=>{
  anim.playing = false; restartAnimLoop();
  const list = getAnimRange();
  anim.cursor = (anim.cursor - 1 + list.length) % list.length;
  drawAnimTick(false);
};
$('#btnStepNext').onclick = ()=>{
  anim.playing = false; restartAnimLoop();
  const list = getAnimRange();
  anim.cursor = (anim.cursor + 1) % list.length;
  drawAnimTick(false);
};
$('#btnEditPlaying').onclick = ()=>{
  const list = getAnimRange();
  if(list.length) selectFrame(list[anim.cursor] || list[0]);
};
$('#fileSearch').oninput = loadFiles;
$('#chkOnlyMod').onchange = loadFiles;

$('#btnRevertAll').onclick = async ()=>{
  if(!confirm('Reverter TODAS as modificações pendentes em workspace/files?')) return;
  const r = await api('/api/file/revert', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:'all'})});
  toast(r.status);
  await loadCharacters();
  await selectChar(state.cid);
};

$('#btnBuild').onclick = async ()=>{
  $('#btnBuild').disabled = true;
  $('#statusText').textContent = 'Construindo ISO com EDC/ECC...';
  try{
    const r = await api('/api/build', {method:'POST'});
    toast(`ISO construída e verificada em ${r.elapsed_s}s: ${r.output}`);
    $('#statusText').textContent = `Build pronto (${r.elapsed_s}s)`;
  }catch(ex){
    toast(ex.message, true);
    $('#statusText').textContent = 'Falha no build';
  }finally{
    $('#btnBuild').disabled = false;
  }
};

$('#btnLaunch').onclick = async ()=>{
  try{
    const r = await api('/api/launch', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    toast(`PCSX2 aberto com ${r.disc}`);
  }catch(ex){
    const custom = prompt(ex.message + '\nCole o caminho do pcsx2-qt.exe:');
    if(custom){
      try{
        const r2 = await api('/api/launch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pcsx2_path: custom})});
        toast(`PCSX2 aberto com ${r2.disc}`);
      }catch(ex2){ toast(ex2.message, true); }
    }
  }
};

// ---------------- Character Forge Logic ----------------
state.forgeProjects = [];
state.activeForgeProj = null;
state.activeForgeFrames = [];

function populateForgeBaseSelect(){
  const sel = $('#forgeBaseSelect');
  if(!sel || sel.children.length > 0) return;
  for(const c of state.chars){
    const opt = document.createElement('option');
    opt.value = String(c.cid);
    opt.textContent = `${c.name} (${c.hex_id})`;
    if(c.cid === 0x03) opt.selected = true; // Terry Bogard como padrão do Mode B
    sel.appendChild(opt);
  }
}

async function loadForgeProjects(){
  try{
    const res = await api('/api/forge/projects');
    state.forgeProjects = res.projects || [];
    renderForgeProjectsList();
    if(!state.activeForgeProj && state.forgeProjects.length > 0){
      selectForgeProject(state.forgeProjects[0].slug);
    }
  }catch(ex){ console.warn(ex); }
}

function renderForgeProjectsList(){
  const list = $('#forgeProjectsList');
  if(!list) return;
  list.innerHTML = '';
  if(state.forgeProjects.length === 0){
    list.innerHTML = '<div class="hint" style="padding:10px;text-align:center">Nenhum projeto encontrado. Crie o primeiro acima!</div>';
    return;
  }
  for(const p of state.forgeProjects){
    const d = document.createElement('div');
    const isActive = state.activeForgeProj && state.activeForgeProj.slug === p.slug;
    d.className = 'forge-proj-card' + (isActive ? ' active' : '');
    const valClass = p.valid ? 'ok' : 'warn';
    const valText = p.valid ? 'VALIDADO' : 'PENDENTE';
    d.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between">
        <b style="font-size:12px;color:var(--text)">${p.name}</b>
        <span class="forge-badge ${valClass}">${valText}</span>
      </div>
      <div class="hint">Base: <b>${p.base_slot}</b> [${p.char_id_hex}] • ${p.modified_frames_count || 0} frames mod</div>
      <div class="row" style="margin-top:4px;gap:4px">
        <button class="btn" style="padding:3px 8px;font-size:10px" onclick="event.stopPropagation(); selectForgeProject('${p.slug}')">Abrir</button>
        <button class="btn" style="padding:3px 8px;font-size:10px" onclick="event.stopPropagation(); window.open('/api/forge/project/${p.slug}/export')">📦 Exportar</button>
        <button class="btn danger" style="padding:3px 8px;font-size:10px" onclick="event.stopPropagation(); deleteForgeProject('${p.slug}')">Excluir</button>
      </div>
    `;
    d.onclick = () => selectForgeProject(p.slug);
    list.appendChild(d);
  }
}

async function selectForgeProject(slug){
  try{
    const res = await api(`/api/forge/project/${slug}`);
    state.activeForgeProj = res.project;
    state.activeForgeFrames = res.frames || [];
    renderActiveForgeProject();
    renderForgeProjectsList();
  }catch(ex){ toast(ex.message, true); }
}

function renderActiveForgeProject(){
  const p = state.activeForgeProj;
  if(!p) return;
  $('#activeProjTitle').textContent = `${p.name} (${p.slug})`;
  $('#activeProjMeta').innerHTML = `Slot Base: <b>${p.base_slot.display_name}</b> (${p.base_slot.char_id_hex}) | Autor: <b>${p.author || 'Anônimo'}</b> | Memória Máx: <b>${(p.base_slot.size_bytes/1024).toFixed(1)} KB</b>`;

  const badge = $('#activeProjBadge');
  const isValid = p.validation && p.validation.valid;
  badge.className = 'forge-badge ' + (isValid ? 'ok' : 'warn');
  badge.textContent = isValid ? 'VALIDADO (LBA-SAFE)' : 'PENDENTE DE VALIDAÇÃO';

  renderValidationChecklist(p.validation);
  renderForgeFramesGrid();
}

function renderValidationChecklist(val){
  const valid = val && val.valid;
  const metrics = (val && val.metrics) || {};
  const chkSlot = $('#chkSlot');
  const chkDims = $('#chkDims');
  const chkPal = $('#chkPal');
  const chkMem = $('#chkMem');

  if(valid){
    chkSlot.className = 'check-item pass';
    chkSlot.querySelector('.forge-badge').className = 'forge-badge ok';
    chkSlot.querySelector('.forge-badge').textContent = 'PASS';

    chkDims.className = 'check-item pass';
    chkDims.querySelector('.forge-badge').className = 'forge-badge ok';
    chkDims.querySelector('.forge-badge').textContent = 'PASS';

    chkPal.className = 'check-item pass';
    chkPal.querySelector('.forge-badge').className = 'forge-badge ok';
    chkPal.querySelector('.forge-badge').textContent = 'PASS';

    chkMem.className = 'check-item pass';
    chkMem.querySelector('.forge-badge').className = 'forge-badge ok';
    chkMem.querySelector('.forge-badge').textContent = 'PASS';

    const used = metrics.estimated_bytes || 0;
    const maxB = metrics.max_memory_bytes || 0;
    const headroom = metrics.headroom_bytes || 0;
    $('#valLogDetails').innerHTML = `✅ <b>Validação Concluída com Sucesso!</b><br/>Tamanho: <b>${used.toLocaleString()} B</b> / ${maxB.toLocaleString()} B (Margem livre: <b>${headroom.toLocaleString()} B</b>). Pronto para injeção na ISO!`;
  } else if(val && val.last_validated) {
    const errText = (val.errors || []).join('<br/>') || 'Erros encontrados na validação.';
    $('#valLogDetails').innerHTML = `<span style="color:var(--err)">⚠️ ${errText}</span>`;
  } else {
    $('#valLogDetails').textContent = "Clique em 'Validar Personagem' para executar a inspeção determinística.";
  }
}

function renderForgeFramesGrid(){
  const grid = $('#forgeFramesGrid');
  if(!grid) return;
  grid.innerHTML = '';
  const frames = state.activeForgeFrames || [];
  $('#forgeCustomCount').textContent = `${frames.length} frame(s) personalizado(s)`;

  if(frames.length === 0){
    grid.innerHTML = '<div class="hint" style="grid-column:1/-1;padding:12px;text-align:center">Nenhum sprite customizado ainda. Selecione um frame # acima e envie uma imagem para substituir o sprite!</div>';
    return;
  }

  for(const f of frames){
    const d = document.createElement('div');
    d.className = 'frame-item';
    d.innerHTML = `
      <img src="/api/forge/project/${state.activeForgeProj.slug}/sprite/${f.frame_index}.png?v=${Date.now()%1e5}" />
      <span>#${f.frame_index}</span>
      <span class="hint">${f.width}x${f.height}</span>
    `;
    d.onclick = () => {
      $('#forgeFrameIdx').value = f.frame_index;
    };
    grid.appendChild(d);
  }
}

$('#btnCreateForgeProj').onclick = async () => {
  const name = $('#forgeProjName').value.trim();
  if(!name) return toast('Informe o nome do personagem', true);
  const base_cid = Number($('#forgeBaseSelect').value);
  const author = $('#forgeProjAuthor').value.trim();
  const description = $('#forgeProjDesc').value.trim();
  try{
    const res = await api('/api/forge/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, base_cid, author, description, overwrite: true }),
    });
    toast(`Projeto '${res.project.name}' criado com sucesso!`);
    await loadForgeProjects();
    await selectForgeProject(res.project.slug);
  }catch(ex){ toast(ex.message, true); }
};

$('#btnForgeValidate').onclick = async () => {
  if(!state.activeForgeProj) return toast('Selecione um projeto primeiro', true);
  try{
    const res = await api(`/api/forge/project/${state.activeForgeProj.slug}/validate`, {method: 'POST'});
    toast(res.valid ? 'Personagem validado com sucesso (LBA-Safe)!' : 'Falha na validação', !res.valid);
    await selectForgeProject(state.activeForgeProj.slug);
    await loadForgeProjects();
  }catch(ex){ toast(ex.message, true); }
};

$('#btnForgeInject').onclick = async () => {
  if(!state.activeForgeProj) return toast('Selecione um projeto primeiro', true);
  try{
    const res = await api(`/api/forge/project/${state.activeForgeProj.slug}/inject`, {method: 'POST'});
    toast(res.message || 'Personagem injetado com sucesso no workspace!');
    await loadCharacters();
    await selectForgeProject(state.activeForgeProj.slug);
  }catch(ex){ toast(ex.message, true); }
};

$('#btnForgeExport').onclick = () => {
  if(!state.activeForgeProj) return toast('Selecione um projeto primeiro', true);
  window.open(`/api/forge/project/${state.activeForgeProj.slug}/export`);
};

$('#btnRefreshProjects').onclick = loadForgeProjects;

async function deleteForgeProject(slug){
  if(!confirm(`Tem certeza que deseja excluir o projeto '${slug}'?`)) return;
  try{
    await api(`/api/forge/project/${slug}/delete`, {method: 'POST'});
    toast(`Projeto '${slug}' excluído.`);
    if(state.activeForgeProj && state.activeForgeProj.slug === slug){
      state.activeForgeProj = null;
    }
    await loadForgeProjects();
  }catch(ex){ toast(ex.message, true); }
}

let pendingForgeSpriteBase64 = null;
$('#btnForgePickSprite').onclick = () => $('#forgeSpriteInput').click();
$('#forgeSpriteInput').onchange = ev => {
  const file = ev.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    pendingForgeSpriteBase64 = reader.result;
    toast(`Imagem '${file.name}' carregada e pronta para importar.`);
  };
  reader.readAsDataURL(file);
  ev.target.value = '';
};

$('#btnForgeImportSprite').onclick = async () => {
  if(!state.activeForgeProj) return toast('Selecione um projeto primeiro', true);
  if(!pendingForgeSpriteBase64) return toast('Selecione um arquivo de imagem primeiro', true);
  const frameIdx = Number($('#forgeFrameIdx').value);
  try{
    const res = await api(`/api/forge/project/${state.activeForgeProj.slug}/import_sprite`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        frame_idx: frameIdx,
        image_base64: pendingForgeSpriteBase64,
        btn: 'A',
        remove_magenta: $('#chkForgeMagenta').checked,
        auto_fit: $('#chkForgeAutoFit').checked,
      }),
    });
    toast(`Frame #${frameIdx} importado com sucesso para o projeto!`);
    pendingForgeSpriteBase64 = null;
    await selectForgeProject(state.activeForgeProj.slug);
  }catch(ex){ toast(ex.message, true); }
};

// ---------------- Forge AI Assistant Sidebar ----------------
async function sendForgeAIChat(promptText){
  const txt = promptText || $('#forgeChatInput').value.trim();
  if(!txt) return;
  $('#forgeChatInput').value = '';

  const chatBox = $('#forgeChatBox');
  const uMsg = document.createElement('div');
  uMsg.className = 'chat-msg user';
  uMsg.textContent = txt;
  chatBox.appendChild(uMsg);
  chatBox.scrollTop = chatBox.scrollHeight;

  try{
    const res = await api('/api/forge/ai/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: txt,
        project_name: state.activeForgeProj ? state.activeForgeProj.name : null,
      }),
    });

    const aMsg = document.createElement('div');
    aMsg.className = 'chat-msg assistant';
    let replyHtml = (res.reply || '').replace(/\n/g, '<br/>');
    if(res.skill_executed){
      replyHtml += `<div class="tool-card">🔧 Executou: <b>${res.skill_executed}</b> (${res.status})</div>`;
    }
    aMsg.innerHTML = replyHtml;
    chatBox.appendChild(aMsg);
    chatBox.scrollTop = chatBox.scrollHeight;

    if(res.action_log){
      const aLog = $('#forgeActionLog');
      aLog.innerHTML = '';
      for(const item of res.action_log.slice(-10)){
        const row = document.createElement('div');
        row.style.fontSize = '10px';
        row.textContent = `[${item.type}] ${item.detail}`;
        aLog.appendChild(row);
      }
    }

    await loadForgeProjects();
    if(state.activeForgeProj) await selectForgeProject(state.activeForgeProj.slug);
  }catch(ex){
    const eMsg = document.createElement('div');
    eMsg.className = 'chat-msg assistant';
    eMsg.style.borderColor = 'var(--err)';
    eMsg.textContent = 'Erro ao processar instrução: ' + ex.message;
    chatBox.appendChild(eMsg);
    chatBox.scrollTop = chatBox.scrollHeight;
  }
}

function quickPrompt(p){
  $('#forgeChatInput').value = p;
  sendForgeAIChat(p);
}

$('#btnForgeChatSend').onclick = () => sendForgeAIChat();
$('#forgeChatInput').onkeydown = ev => { if(ev.key === 'Enter') sendForgeAIChat(); };

// Botão [ + CRIAR PERSONAGEM ] no cabeçalho
$('#btnOpenForge').onclick = () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-body').forEach(b => b.classList.remove('active'));
  $('#tabNavForge').classList.add('active');
  $('#tab-forge').classList.add('active');
  populateForgeBaseSelect();
  loadForgeProjects();
  $('#forgeProjName').focus();
};

(async ()=>{
  await loadCharacters();
  populateForgeBaseSelect();
  await loadForgeProjects();
  await selectChar(0x27); // Omega Rugal por padrão
  restartAnimLoop();
})();
</script>
</body>
</html>
"""


def make_handler(backend: StudioBackend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            try:
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                parts = [p for p in u.path.split("/") if p]

                if u.path == "/":
                    self._send(200, HTML_APP.encode("utf-8"), "text/html; charset=utf-8")
                elif u.path == "/api/characters":
                    chars = backend.list_characters()
                    mods = len(backend.proj.modified_entries())
                    self._json(200, {"characters": chars, "modified_files_count": mods})
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "thumb.png":
                    cid = int(parts[2])
                    btn = qs.get("btn", ["A"])[0]
                    self._send(200, backend.get_char_thumb_png(cid, btn), "image/png")
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "palettes":
                    self._json(200, backend.get_palettes(int(parts[2])))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "frames":
                    self._json(200, backend.list_frames(int(parts[2])))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "sequences":
                    self._json(200, backend.list_sequences(int(parts[2])))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "voices":
                    self._json(200, backend.list_voices(int(parts[2])))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "character" and parts[3] == "voice" and parts[4].endswith(".wav"):
                    cid = int(parts[2])
                    cidx = int(parts[4][:-4])
                    self._send(200, backend.get_voice_wav(cid, cidx), "audio/wav")
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "character" and parts[3] == "frame" and parts[4].endswith(".png"):
                    cid = int(parts[2])
                    fidx = int(parts[4][:-4])
                    btn = qs.get("btn", ["A"])[0]
                    mode = qs.get("mode", ["rgba"])[0]
                    self._send(200, backend.get_frame_png(cid, fidx, btn, mode), "image/png")
                elif len(parts) == 6 and parts[0] == "api" and parts[1] == "character" and parts[3] == "frame" and parts[5] == "data":
                    cid = int(parts[2])
                    fidx = int(parts[4])
                    btn = qs.get("btn", ["A"])[0]
                    self._json(200, backend.get_frame_data(cid, fidx, btn))
                elif u.path == "/api/files":
                    q = qs.get("q", [""])[0]
                    om = qs.get("only_mod", ["0"])[0] == "1"
                    self._json(200, {"files": backend.list_files(q, om)})
                elif u.path == "/api/file/hex":
                    p = qs.get("path", [""])[0]
                    pg = int(qs.get("page", ["0"])[0])
                    self._json(200, backend.get_file_hex(p, pg))
                elif u.path == "/api/forge/projects":
                    self._json(200, {"projects": backend.list_forge_projects()})
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project":
                    self._json(200, backend.get_forge_project(parts[3]))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "export":
                    data = backend.export_forge_kofchar(parts[3])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", f'attachment; filename="{parts[3]}.kofchar"')
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                elif len(parts) == 6 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "sprite" and parts[5].endswith(".png"):
                    fidx = int(parts[5][:-4])
                    self._send(200, backend.get_forge_frame_png(parts[3], fidx), "image/png")
                else:
                    self._json(404, {"error": f"Rota não encontrada: {u.path}"})
            except Exception as ex:
                self._json(400, {"error": str(ex)})

        def do_POST(self) -> None:
            try:
                u = urllib.parse.urlparse(self.path)
                parts = [p for p in u.path.split("/") if p]
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "palettes":
                    body = self._read_json()
                    self._json(200, backend.save_palette(int(parts[2]), body["btn"], body["colors"], body.get("pal_idx")))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "character" and parts[3] == "palettes" and parts[4] == "revert":
                    self._json(200, backend.revert_character_palettes(int(parts[2])))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "character" and parts[3] == "frame":
                    body = self._read_json()
                    self._json(200, backend.save_frame(int(parts[2]), int(parts[4]), body))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "character" and parts[3] == "revert":
                    self._json(200, backend.revert_character(int(parts[2])))
                elif u.path == "/api/file/patch":
                    body = self._read_json()
                    self._json(200, backend.patch_file(body["path"], body["offset"], body["hex"]))
                elif u.path == "/api/file/revert":
                    body = self._read_json()
                    self._json(200, backend.revert_file(body["path"]))
                elif u.path == "/api/build":
                    self._json(200, backend.build_iso())
                elif u.path == "/api/launch":
                    body = self._read_json()
                    self._json(200, backend.launch_pcsx2(body.get("pcsx2_path")))
                elif u.path == "/api/forge/create":
                    body = self._read_json()
                    self._json(200, backend.create_forge_project(body))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "import_sprite":
                    body = self._read_json()
                    self._json(200, backend.import_forge_sprite(parts[3], body))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "validate":
                    self._json(200, backend.validate_forge_character(parts[3]))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "inject":
                    self._json(200, backend.inject_forge_character(parts[3]))
                elif u.path == "/api/forge/ai/chat":
                    body = self._read_json()
                    self._json(200, backend.forge_ai_chat(body.get("prompt", ""), body.get("project_name")))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "save":
                    body = self._read_json()
                    self._json(200, backend.save_forge_project(parts[3], body))
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "forge" and parts[2] == "project" and parts[4] == "delete":
                    proj = ForgeProject.load(parts[3])
                    proj.delete()
                    self._json(200, {"status": "ok", "deleted": parts[3]})
                else:
                    self._json(404, {"error": f"Rota não encontrada: {u.path}"})
            except Exception as ex:
                self._json(400, {"error": str(ex)})

    return Handler


def self_test() -> int:
    b = StudioBackend()
    chars = b.list_characters()
    assert len(chars) == 46, f"Esperado 46 personagens, veio {len(chars)}"
    thumb = b.get_char_thumb_png(0x27, "A")
    assert thumb.startswith(b"\x89PNG"), "Thumb P27 inválida"
    fr = b.list_frames(0x27)
    assert fr["total_slots"] == 642, f"P27 frames={fr['total_slots']}"
    f0 = b.get_frame_data(0x27, 0, "A")
    assert not f0["is_stub"] and f0["width_px"] > 0
    pals = b.get_palettes(0x27)
    assert len(pals["buttons"]["A"]["hex"]) == 16

    # Testa roundtrip de paleta e reverte
    orig_hex = pals["buttons"]["A"]["hex"]
    res = b.save_palette(0x27, "A", ["#112233"] * 15)
    assert "pal_idx" in res
    rev = b.revert_character_palettes(0x27)
    assert not b.proj.is_modified(b._first_grc_entry()), f"FIRST.GRC ainda modificado: {rev}"

    # Verifica sequências de golpes especiais e bancos de voz ACX para TODOS os 46 personagens
    for ch in chars:
        cid = ch["cid"]
        sq = b.list_sequences(cid)
        vc = b.list_voices(cid)
        assert len(sq["specials"]) >= 4, f"P{cid:02X} sem especiais suficientes: {sq['specials']}"
        assert vc["total_clips"] > 0, f"P{cid:02X} sem clipes de voz em BGM.AFS"
    wav0 = b.get_voice_wav(0x27, 0)
    assert wav0.startswith(b"RIFF"), "WAV decodificado inválido"
    print(f"SELF-TEST OK: {len(chars)}/46 personagens com especiais + vozes ACX/WAV verificados.")

    # ------------------ Character Forge & AI Assistant Tests ------------------
    print("Executando testes do Character Forge e AI Assistant...")
    test_slug = "terry_legendary_test"
    # 1. Criação do projeto (Mode B - Terry Bogard)
    c_res = b.create_forge_project({
        "name": "Terry Legendary Test",
        "base_slot": 0x03,
        "author": "QA Verifier",
        "description": "Teste automatizado do Character Forge",
        "overwrite": True,
    })
    assert c_res["status"] == "ok"
    proj_info = c_res["project"]
    assert proj_info["slug"] == test_slug

    # 2. Listagem de projetos
    projs = b.list_forge_projects()
    assert any(p["slug"] == test_slug for p in projs), "Projeto não encontrado na listagem"

    # 3. Leitura do projeto
    g_res = b.get_forge_project(test_slug)
    assert g_res["status"] == "ok"
    assert g_res["project"]["base_slot"]["char_id"] == 3

    # 4. Importação de sprite (Gera um PNG 128x128 sintético para o frame 0 do Terry)
    dummy_im = Image.new("RGBA", (128, 128), (220, 40, 40, 255))
    dummy_buf = io.BytesIO()
    dummy_im.save(dummy_buf, format="PNG")
    dummy_b64 = base64.b64encode(dummy_buf.getvalue()).decode("ascii")

    imp_res = b.import_forge_sprite(test_slug, {
        "frame_idx": 0,
        "image_base64": dummy_b64,
        "btn": "A",
        "remove_magenta": False,
        "auto_fit": True,
    })
    assert imp_res["status"] == "ok"
    assert imp_res["frame_idx"] == 0

    # 5. Validação determinística
    val_res = b.validate_forge_character(test_slug)
    assert val_res["status"] == "ok"
    assert val_res["valid"] is True, f"Validação falhou: {val_res}"

    # 6. Exportação do pacote .kofchar
    pkg_bytes = b.export_forge_kofchar(test_slug)
    assert len(pkg_bytes) > 100
    assert pkg_bytes.startswith(b"PK\x03\x04"), "Pacote .kofchar não é um ZIP válido"

    # 7. Injeção no workspace de disco
    inj_res = b.inject_forge_character(test_slug)
    assert inj_res["status"] == "ok"
    assert inj_res["frames_injected"] >= 1
    assert any("P03.BIN" in e["clean_path"] for e in b.proj.modified_entries()), "P03.BIN não foi modificado"

    # 8. Testes do Forge AI Assistant
    ai_create = b.forge_ai_chat("Crie um lutador baseado no Terry")
    assert ai_create["action"] == "execute_tool"
    assert ai_create["skill_executed"] == "skill_create_project"

    ai_val = b.forge_ai_chat("Valide o personagem", test_slug)
    assert ai_val["action"] == "execute_tool"
    assert ai_val["skill_executed"] == "skill_validate_character"

    ai_inject = b.forge_ai_chat("Injete na ISO", test_slug)
    assert ai_inject["action"] == "execute_tool"
    assert ai_inject["skill_executed"] == "skill_build_patch"

    ai_rej = b.forge_ai_chat("Escrever bytes hex 0x90 no offset 0x3E6960")
    assert ai_rej["action"] == "rejected"

    # 9. Limpeza: reverte arquivos modificados e remove projetos de teste
    b.revert_character(0x03)
    assert not any("P03.BIN" in e["clean_path"] for e in b.proj.modified_entries()), "P03.BIN ainda modificado após reverter"

    test_proj = ForgeProject.load(test_slug)
    test_proj.delete()
    default_ws = Path(test_proj.project_dir).parent
    for extra_name in ("terry_bogard_custom", "terry_custom"):
        cand = default_ws / extra_name
        if cand.exists():
            ForgeProject.load(cand).delete()

    print("FORGE INTEGRATION TESTS OK: Projetos, Importação, Validação, Injeção e AI Assistant 100% verificados.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="Abre o navegador automaticamente")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--test", action="store_true", help="Executa testes unitários e de integração")
    args = ap.parse_args()

    if args.self_test or args.test:
        return self_test()

    backend = StudioBackend()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(backend))
    url = f"http://{args.host}:{args.port}/"
    print(f"KOF 2002 Studio rodando em {url}", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

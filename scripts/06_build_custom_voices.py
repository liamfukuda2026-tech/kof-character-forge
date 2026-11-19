#!/usr/bin/env python3
"""
KOF 2002 (PS2) - Custom Character Voice Pack Generator & LBA-Safe ACX Injector
==============================================================================
Generates Brazilian Portuguese (PT-BR) voice clips for Omega Rugal's slot (p27.acx
inside /ADX/BGM.AFS, AFS entry #73, offset 0x9b8c000, size 699,028 bytes), masters
them for arcade PS2 punch (zero leading silence, deep EQ, compression, 24kHz mono),
encodes them into exact KOF 2002 CRI ADX v4 format, packs p27.acx, and patches
ONLY the 342 sectors of p27.acx in-place inside build/kof2002_demo.bin (recalculating
Mode 2 Form 1 EDC + P/Q ECC) and build/kof2002_demo.iso.

Never modifies original disc images, extracted/, or scripts 01-04.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

RAW_SECTOR_SIZE = 2352
LOGICAL_SECTOR_SIZE = 2048
RUGAL_AFS_INDEX = 73
RUGAL_ACX_OFFSET = 0x9B8C000
RUGAL_ACX_SIZE = 699028
RUGAL_ACX_SECTORS = (RUGAL_ACX_SIZE + LOGICAL_SECTOR_SIZE - 1) // LOGICAL_SECTOR_SIZE  # 342

# Default 31 PT-BR fighter lines (clips 00..30). Clips 31..39 preserve original PS2 SFX.
# Each entry: (clip_id, text, pitch, rate, role)
DEFAULT_VOICE_LINES: list[tuple[int, str, str, str, str]] = [
    (0,  "Rá!",                             "-14Hz", "+22%", "Light attack"),
    (1,  "Toma!",                           "-14Hz", "+20%", "Medium attack / combo"),
    (2,  "É hora do show!",                 "-14Hz", "+18%", "Heavy punch / combo signature"),
    (3,  "Agh!",                            "-16Hz", "+15%", "Light damage"),
    (4,  "Argh!",                           "-18Hz", "+10%", "Heavy damage"),
    (5,  "Vem cá!",                         "-15Hz", "+18%", "Grab / throw"),
    (6,  "Não! Ainda não acabou!",          "-16Hz", "+8%",  "KO defeat"),
    (7,  "Haha! Moleza!",                   "-13Hz", "+15%", "Short laugh / win"),
    (8,  "Só isso que você tem?",           "-15Hz", "+12%", "Taunt / victory"),
    (9,  "Hmph!",                           "-16Hz", "+10%", "Guard / effort"),
    (10, "Tá com medo?",                    "-15Hz", "+10%", "Intro / taunt"),
    (11, "Bora! É hora do show!",           "-14Hz", "+14%", "Battle start intro"),
    (12, "Sente a força do braço biônico!", "-15Hz", "+12%", "Intro / super line"),
    (13, "Fraco!",                          "-16Hz", "+18%", "Throw / finisher"),
    (14, "Nem arranhou!",                   "-15Hz", "+16%", "Taunt / counter"),
    (15, "Segura essa sequência!",          "-14Hz", "+18%", "Combo rush"),
    (16, "Carregando!",                     "-16Hz", "+12%", "Special charge"),
    (17, "Corte biônico!",                  "-14Hz", "+18%", "Genocide Cutter special"),
    (18, "Sai!",                            "-15Hz", "+25%", "Quick dash / dodge"),
    (19, "Haha!",                           "-13Hz", "+18%", "Quick chuckle"),
    (20, "É hora do show!",                 "-12Hz", "+20%", "Reppuken / energy punch"),
    (21, "Canhão de plasma!",               "-15Hz", "+15%", "Kaiser Wave special"),
    (22, "Toma essa!",                      "-14Hz", "+20%", "Special strike"),
    (23, "Fácil demais!",                   "-13Hz", "+15%", "Laugh / taunt"),
    (24, "Acabou pra você! É hora do show!","-15Hz", "+14%", "Super / SDM finisher"),
    (25, "Hahaha! Ninguém segura o Elton!", "-13Hz", "+12%", "Victory epic laugh"),
    (26, "Tá tremendo?",                    "-16Hz", "+10%", "Low taunt"),
    (27, "Vitória garantida! Próximo!",     "-14Hz", "+12%", "Victory quote"),
    (28, "Poder máximo!",                   "-16Hz", "+10%", "Max power shout"),
    (29, "Prepara!",                        "-15Hz", "+14%", "Charge shout"),
    (30, "Agora!",                          "-14Hz", "+20%", "Burst shout"),
]


def find_edge_tts() -> str:
    found = shutil.which("edge-tts")
    if found:
        return found
    for cand in [
        r"C:\Users\Elton\AppData\Local\Programs\Python\Python312\Scripts\edge-tts.exe",
        r"C:\Users\Elton\AppData\Local\Programs\Python\Python313\Scripts\edge-tts.exe",
    ]:
        if Path(cand).is_file():
            return cand
    raise FileNotFoundError("edge-tts executable not found on PATH or standard Python Scripts directories.")


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffmpeg_v3_to_ps2_v4_adx(raw_v3: bytes) -> bytes:
    """Convert FFmpeg CRI ADX v3 bytes into exact PS2 KOF 2002 CRI ADX v4 bytes."""
    if len(raw_v3) < 40 or raw_v3[:2] != b"\x80\x00":
        raise ValueError("Invalid ADX header from ffmpeg")
    old_data_off = struct.unpack_from(">H", raw_v3, 2)[0]
    if raw_v3[old_data_off - 2 : old_data_off + 4] != b"(c)CRI":
        raise ValueError(f"Missing (c)CRI signature at 0x{old_data_off - 2:x}")
    payload = raw_v3[old_data_off + 4 :]

    # Exact 40-byte v4 header matching original KOF 2002 p27.acx clips
    v4_hdr = bytearray(40)
    v4_hdr[0:2] = b"\x80\x00"
    struct.pack_into(">H", v4_hdr, 2, 0x0024)
    v4_hdr[4:18] = raw_v3[4:18]  # enc=3, blk=18, bps=4, ch=1, sr=24000, samples, cutoff=500
    v4_hdr[18] = 0x04  # ADX version 4
    v4_hdr[19] = 0x00  # flags
    v4_hdr[34:40] = b"(c)CRI"
    return bytes(v4_hdr) + payload


def generate_one_clip(
    edge_tts_bin: str,
    voice: str,
    work_dir: Path,
    clip_id: int,
    text: str,
    pitch: str,
    rate: str,
    role: str,
) -> tuple[int, Path, float]:
    mp3_p = work_dir / f"clip_{clip_id:02d}.mp3"
    wav_p = work_dir / f"clip_{clip_id:02d}.wav"
    v3_p = work_dir / f"clip_{clip_id:02d}_v3.adx"
    v4_p = work_dir / f"clip_{clip_id:02d}.adx"

    subprocess.run(
        [
            edge_tts_bin,
            "--voice", voice,
            f"--pitch={pitch}",
            f"--rate={rate}",
            "--text", text,
            "--write-media", str(mp3_p),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Arcade PS2 mastering: strip leading/trailing silence (instant frame-1 trigger!),
    # punchy masculine EQ + tight compression + limiter @ 24kHz mono.
    af = (
        "silenceremove=start_periods=1:start_duration=0:start_threshold=-40dB,"
        "areverse,silenceremove=start_periods=1:start_duration=0:start_threshold=-40dB,areverse,"
        "highpass=f=90,lowpass=f=8500,"
        "equalizer=f=160:t=q:w=1.2:g=4.5,equalizer=f=2600:t=q:w=1.2:g=3.5,"
        "acompressor=threshold=-16dB:ratio=4:attack=4:release=50:makeup=3,"
        "alimiter=limit=0.93"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(mp3_p), "-af", af, "-ar", "24000", "-ac", "1", str(wav_p)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav_p), "-ar", "24000", "-ac", "1", "-c:a", "adpcm_adx", str(v3_p)],
        check=True,
    )
    v4_bytes = ffmpeg_v3_to_ps2_v4_adx(v3_p.read_bytes())
    v4_p.write_bytes(v4_bytes)
    v3_p.unlink(missing_ok=True)
    mp3_p.unlink(missing_ok=True)

    with wave.open(str(wav_p), "rb") as w:
        dur = w.getnframes() / float(w.getframerate())
    return clip_id, v4_p, dur


def pack_acx(clips: list[bytes], target_size: int) -> tuple[bytes, int]:
    """Pack 40 ADX clips into a 4-byte aligned CRI ACX container padded to exact target_size."""
    count = len(clips)
    hdr_size = 8 + count * 8
    cur_off = (hdr_size + 3) & ~3

    table = bytearray(hdr_size)
    struct.pack_into(">2I", table, 0, 0, count)
    body = bytearray()
    if cur_off > hdr_size:
        body.extend(b"\x00" * (cur_off - hdr_size))

    for i, data in enumerate(clips):
        pad = (4 - (cur_off % 4)) % 4
        if pad:
            body.extend(b"\x00" * pad)
            cur_off += pad
        struct.pack_into(">2I", table, 8 + i * 8, cur_off, len(data))
        body.extend(data)
        cur_off += len(data)

    used_size = cur_off
    if used_size > target_size:
        raise ValueError(f"Rebuilt p27.acx ({used_size:,} B) exceeds original AFS slot size ({target_size:,} B)!")

    out = bytearray(table) + body
    if len(out) < target_size:
        out.extend(b"\x00" * (target_size - len(out)))
    return bytes(out), used_size


def patch_acx_into_discs(
    acx_bytes: bytes,
    bgm_lba_start: int,
    bin_path: Path | None,
    iso_path: Path | None,
    m03: Any,
) -> None:
    if len(acx_bytes) != RUGAL_ACX_SIZE:
        raise ValueError(f"Expected ACX size {RUGAL_ACX_SIZE}, got {len(acx_bytes)}")
    if RUGAL_ACX_OFFSET % LOGICAL_SECTOR_SIZE != 0:
        raise ValueError("RUGAL_ACX_OFFSET is not 2048-byte sector aligned")

    acx_lba_start = bgm_lba_start + (RUGAL_ACX_OFFSET // LOGICAL_SECTOR_SIZE)
    padded_len = RUGAL_ACX_SECTORS * LOGICAL_SECTOR_SIZE
    padded = acx_bytes + b"\x00" * (padded_len - len(acx_bytes))

    # Preserve any original tail bytes in the last partial sector (bytes 699028..700416)
    if bin_path and bin_path.exists():
        with bin_path.open("r+b") as fp:
            # Read original tail of last sector so untouched slack bytes stay exact
            last_lba = acx_lba_start + RUGAL_ACX_SECTORS - 1
            fp.seek(last_lba * RAW_SECTOR_SIZE)
            last_orig_sec = fp.read(RAW_SECTOR_SIZE)
            rem = RUGAL_ACX_SIZE % LOGICAL_SECTOR_SIZE
            if rem != 0:
                padded_ba = bytearray(padded)
                tail_start = (RUGAL_ACX_SECTORS - 1) * LOGICAL_SECTOR_SIZE + rem
                padded_ba[tail_start:] = last_orig_sec[24 + rem : 2072]
                padded = bytes(padded_ba)

            for i in range(RUGAL_ACX_SECTORS):
                lba = acx_lba_start + i
                user_2048 = padded[i * LOGICAL_SECTOR_SIZE : (i + 1) * LOGICAL_SECTOR_SIZE]
                phys = lba * RAW_SECTOR_SIZE
                fp.seek(phys)
                orig_sec = fp.read(RAW_SECTOR_SIZE)
                new_sec = m03.rebuild_mode2_form1_sector(orig_sec, user_2048)
                fp.seek(phys)
                fp.write(new_sec)

            # Verify every single patched sector passes EDC + P/Q ECC and matches bytes!
            for i in range(RUGAL_ACX_SECTORS):
                lba = acx_lba_start + i
                fp.seek(lba * RAW_SECTOR_SIZE)
                sec = fp.read(RAW_SECTOR_SIZE)
                edc_ok, p_ok, q_ok = m03.verify_mode2_form1_sector(sec)
                if not (edc_ok and p_ok and q_ok):
                    raise RuntimeError(f"EDC/ECC failed at LBA {lba}: edc={edc_ok} p={p_ok} q={q_ok}")
                exp = padded[i * LOGICAL_SECTOR_SIZE : (i + 1) * LOGICAL_SECTOR_SIZE]
                if sec[24:2072] != exp:
                    raise RuntimeError(f"User data mismatch at LBA {lba}")
        print(f"Patched & verified {RUGAL_ACX_SECTORS} Mode2/2352 sectors (LBA {acx_lba_start}..{acx_lba_start + RUGAL_ACX_SECTORS - 1}) in {bin_path}")

    if iso_path and iso_path.exists():
        with iso_path.open("r+b") as fp:
            fp.seek(acx_lba_start * LOGICAL_SECTOR_SIZE)
            fp.write(padded)
            fp.seek(acx_lba_start * LOGICAL_SECTOR_SIZE)
            got = fp.read(RUGAL_ACX_SIZE)
            if got != acx_bytes:
                raise RuntimeError(f"ISO read-back mismatch in {iso_path}")
        print(f"Patched & verified {RUGAL_ACX_SECTORS} ISO2048 sectors (LBA {acx_lba_start}..{acx_lba_start + RUGAL_ACX_SECTORS - 1}) in {iso_path}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Generate custom PT-BR fighter voices and inject p27.acx LBA-safely.")
    parser.add_argument("--voice", default="pt-BR-AntonioNeural", help="edge-tts neural voice name")
    parser.add_argument("--work-dir", type=Path, default=root / "custom_character_demo" / "voices")
    parser.add_argument("--out-acx", type=Path, default=root / "build" / "patched_demo" / "ADX" / "p27.acx")
    parser.add_argument("--bin", type=Path, default=root / "build" / "kof2002_demo.bin")
    parser.add_argument("--iso", type=Path, default=root / "build" / "kof2002_demo.iso")
    parser.add_argument("--preview-wav", type=Path, default=root / "build" / "elton_voices_preview.wav")
    args = parser.parse_args()

    edge_tts_bin = find_edge_tts()
    m03 = load_module(root / "scripts" / "03_build_mod_iso.py", "m03")

    manifest = json.loads((root / "scripts" / "iso_manifest.json").read_text(encoding="utf-8"))
    bgm_entry = next(e for e in manifest["files"] if e["clean_path"].upper() == "/ADX/BGM.AFS")
    bgm_lba_start = int(bgm_entry["lba_start"])

    # 1. Read original p27.acx from read-only extracted/ADX/BGM.AFS to extract original SFX clips 31..39
    orig_afs = root / "extracted" / "ADX" / "BGM.AFS"
    with orig_afs.open("rb") as f:
        f.seek(8 + RUGAL_AFS_INDEX * 8)
        off, sz = struct.unpack("<2I", f.read(8))
        if off != RUGAL_ACX_OFFSET or sz != RUGAL_ACX_SIZE:
            raise RuntimeError(f"Unexpected AFS[73] metadata: off=0x{off:x}, sz={sz}")
        f.seek(off)
        orig_acx = f.read(sz)

    magic, count = struct.unpack_from(">2I", orig_acx, 0)
    assert magic == 0 and count == 40
    orig_clips: list[bytes] = []
    for i in range(count):
        co, cs = struct.unpack_from(">2I", orig_acx, 8 + i * 8)
        orig_clips.append(orig_acx[co : co + cs])

    # 2. Generate all 31 PT-BR voice clips in parallel
    args.work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Generating {len(DEFAULT_VOICE_LINES)} PT-BR voice clips ({args.voice}) into {args.work_dir} ...")
    generated: dict[int, tuple[Path, float]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(generate_one_clip, edge_tts_bin, args.voice, args.work_dir, cid, txt, pitch, rate, role)
            for cid, txt, pitch, rate, role in DEFAULT_VOICE_LINES
        ]
        for fut in futs:
            cid, v4_p, dur = fut.result()
            generated[cid] = (v4_p, dur)

    # 3. Assemble 40-clip list (00..30 custom PT-BR voices, 31..39 preserved original SFX)
    final_clips: list[bytes] = []
    manifest_rows = []
    for i in range(40):
        if i in generated:
            adx_p, dur = generated[i]
            data = adx_p.read_bytes()
            final_clips.append(data)
            txt = next(t for c, t, _, _, _ in DEFAULT_VOICE_LINES if c == i)
            role = next(r for c, _, _, _, r in DEFAULT_VOICE_LINES if c == i)
            manifest_rows.append({"clip": i, "source": "custom_ptbr", "text": txt, "role": role, "dur_s": round(dur, 2), "bytes": len(data)})
        else:
            final_clips.append(orig_clips[i])
            manifest_rows.append({"clip": i, "source": "original_sfx", "bytes": len(orig_clips[i])})

    packed_acx, used_size = pack_acx(final_clips, RUGAL_ACX_SIZE)
    args.out_acx.parent.mkdir(parents=True, exist_ok=True)
    args.out_acx.write_bytes(packed_acx)
    (args.work_dir / "voice_map.json").write_text(json.dumps(manifest_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Packed {args.out_acx}: used={used_size:,}/{RUGAL_ACX_SIZE:,} B (slack={RUGAL_ACX_SIZE - used_size:,} B)")

    # 4. Patch directly into build/kof2002_demo.bin (EDC+ECC) and build/kof2002_demo.iso
    patch_acx_into_discs(packed_acx, bgm_lba_start, args.bin, args.iso, m03)

    # 5. End-to-end roundtrip verify: read p27.acx STRAIGHT BACK OUT of kof2002_demo.iso and decode all 40 clips!
    acx_lba_start = bgm_lba_start + (RUGAL_ACX_OFFSET // LOGICAL_SECTOR_SIZE)
    with args.iso.open("rb") as fiso:
        fiso.seek(acx_lba_start * LOGICAL_SECTOR_SIZE)
        iso_acx = fiso.read(RUGAL_ACX_SIZE)
    assert iso_acx == packed_acx, "ISO ACX readback mismatch!"
    m2, c2 = struct.unpack_from(">2I", iso_acx, 0)
    assert m2 == 0 and c2 == 40

    verify_dir = args.work_dir / "_verify_roundtrip"
    verify_dir.mkdir(exist_ok=True)
    preview_ids = [0, 1, 2, 11, 12, 15, 17, 20, 21, 22, 24, 25]
    preview_frames = []
    gap = b"\x00\x00" * int(24000 * 0.35)
    for i in range(40):
        co, cs = struct.unpack_from(">2I", iso_acx, 8 + i * 8)
        tmp_adx = verify_dir / f"v_{i:02d}.adx"
        tmp_wav = verify_dir / f"v_{i:02d}.wav"
        tmp_adx.write_bytes(iso_acx[co : co + cs])
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp_adx), str(tmp_wav)], check=True)
        if i in preview_ids:
            with wave.open(str(tmp_wav), "rb") as w:
                preview_frames.append(w.readframes(w.getnframes()))
                preview_frames.append(gap)
    shutil.rmtree(verify_dir, ignore_errors=True)

    with wave.open(str(args.preview_wav), "wb") as out_w:
        out_w.setnchannels(1)
        out_w.setsampwidth(2)
        out_w.setframerate(24000)
        out_w.writeframes(b"".join(preview_frames))
    print(f"Roundtrip verified ALL 40/40 ADX v4 clips directly from {args.iso} with 0 errors!")
    print(f"Audition preview written to: {args.preview_wav}")

    h = hashlib.sha256()
    with args.iso.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    print("Updated ISO SHA-256:", h.hexdigest().upper())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

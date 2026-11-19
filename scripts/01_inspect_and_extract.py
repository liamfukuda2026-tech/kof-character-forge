#!/usr/bin/env python3
"""Inspect a KOF 2002 PS2 disc image read-only and extract its main files.

Supports standard 2048-byte/sector ISO images as well as raw 2352-byte/sector
CD-ROM BIN images (Mode 2 Form 1 / Mode 1) via a strictly read-only sector
reader wrapper around pycdlib.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO

import pycdlib

LOGICAL_SECTOR_SIZE = 2048
RAW_SECTOR_SIZE = 2352
PVD_SECTOR = 16
ISO_MAGIC = b"\x01CD001"

CONTAINER_EXTS = {
    ".afs",
    ".bin",
    ".pac",
    ".dat",
    ".pak",
    ".cpk",
    ".img",
    ".arc",
    ".rel",
    ".obj",
    ".rom",
}

ROM_PATTERN = re.compile(
    r"(?:^|[_\-/\\])(?:[cpsmv][_\-]?rom|[cpsmv][0-9]+|crom|srom|prom|mrom|vrom|neo|kof)",
    re.IGNORECASE,
)

PS2_EXEC_PATTERN = re.compile(
    r"(S[CL][PEU][SM]_[0-9]{3}\.[0-9]{2})",
    re.IGNORECASE,
)


class RawBinSectorReader(io.RawIOBase):
    """Read-only binary stream mapping 2048-byte ISO sectors from a 2352-byte BIN."""

    def __init__(self, raw_fp: BinaryIO, sector_size: int, user_offset: int) -> None:
        super().__init__()
        self._fp = raw_fp
        self._sector_size = sector_size
        self._user_offset = user_offset

        self._fp.seek(0, os.SEEK_END)
        raw_size = self._fp.tell()
        self._num_sectors = raw_size // sector_size
        self._logical_size = self._num_sectors * LOGICAL_SECTOR_SIZE
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._logical_size + offset
        else:
            raise ValueError(f"Unsupported whence: {whence}")
        if target < 0:
            raise ValueError("Negative seek position")
        self._pos = min(target, self._logical_size)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, buffer: bytearray | memoryview) -> int:
        mv = memoryview(buffer).cast("B")
        want = len(mv)
        if want == 0 or self._pos >= self._logical_size:
            return 0

        remaining = min(want, self._logical_size - self._pos)
        written = 0
        while written < remaining:
            cur = self._pos + written
            sector_idx = cur // LOGICAL_SECTOR_SIZE
            in_sector_off = cur % LOGICAL_SECTOR_SIZE
            chunk_len = min(remaining - written, LOGICAL_SECTOR_SIZE - in_sector_off)

            phys_off = sector_idx * self._sector_size + self._user_offset + in_sector_off
            self._fp.seek(phys_off, os.SEEK_SET)
            data = self._fp.read(chunk_len)
            if not data:
                break
            mv[written : written + len(data)] = data
            written += len(data)
            if len(data) < chunk_len:
                break

        self._pos += written
        return written


def detect_disc_format(fp: BinaryIO) -> tuple[str, int, int]:
    """Detect whether an image is 2048-byte ISO or 2352-byte raw CD-ROM BIN."""
    fp.seek(0, os.SEEK_END)
    total_size = fp.tell()

    # Standard ISO9660 PVD at sector 16 * 2048
    if total_size >= (PVD_SECTOR + 1) * LOGICAL_SECTOR_SIZE:
        fp.seek(PVD_SECTOR * LOGICAL_SECTOR_SIZE, os.SEEK_SET)
        if fp.read(6) == ISO_MAGIC:
            return ("iso2048", LOGICAL_SECTOR_SIZE, 0)

    # Raw 2352-byte CD-ROM: Mode 2 Form 1 (user data at +24) or Mode 1 (at +16)
    if total_size >= (PVD_SECTOR + 1) * RAW_SECTOR_SIZE:
        for mode_name, user_off in (("bin2352_mode2", 24), ("bin2352_mode1", 16)):
            fp.seek(PVD_SECTOR * RAW_SECTOR_SIZE + user_off, os.SEEK_SET)
            if fp.read(6) == ISO_MAGIC:
                return (mode_name, RAW_SECTOR_SIZE, user_off)

    # UDF-only ISO fallback (BEA01 at sector 16 * 2048 + 1)
    if total_size >= (PVD_SECTOR + 1) * LOGICAL_SECTOR_SIZE:
        fp.seek(PVD_SECTOR * LOGICAL_SECTOR_SIZE + 1, os.SEEK_SET)
        if fp.read(5) == b"BEA01":
            return ("udf2048", LOGICAL_SECTOR_SIZE, 0)

    raise ValueError(
        "Unrecognized disc image format (no ISO9660 CD001 or UDF BEA01 signature found)."
    )


def find_disc_image(workspace: Path) -> Path | None:
    """Auto-detect *.iso (preferred) or *.bin disc image in the workspace root."""
    isos = sorted(workspace.glob("*.iso")) + sorted(workspace.glob("*.ISO"))
    if isos:
        return isos[0]
    bins = sorted(workspace.glob("*.bin")) + sorted(workspace.glob("*.BIN"))
    if bins:
        return bins[0]
    return None


def strip_iso_version(iso_path: str) -> str:
    """Remove trailing ';1' version suffix from an ISO9660 path."""
    return iso_path.split(";", 1)[0]


def classify_entry(clean_path: str, exec_name: str | None) -> str:
    """Classify an ISO file entry by name/extension."""
    upper = clean_path.upper()
    basename = upper.rsplit("/", 1)[-1]
    ext = Path(basename).suffix.lower()

    if basename == "SYSTEM.CNF":
        return "system"
    if exec_name and basename == exec_name.upper():
        return "executable"
    if PS2_EXEC_PATTERN.search(basename):
        return "executable"
    if ext == ".irx":
        return "iop_module"
    if ext in {".afs", ".pac", ".cpk", ".arc"}:
        return "container"
    if ROM_PATTERN.search(clean_path):
        return "rom_or_data"
    if ext in CONTAINER_EXTS:
        return "container_or_data"
    if ext in {".pss", ".sfd", ".adx", ".vag", ".str", ".pss2"}:
        return "media"
    return "other"


def parse_system_cnf(raw_text: str) -> dict[str, Any]:
    """Parse PS2 SYSTEM.CNF key=value pairs and extract the BOOT2 executable."""
    parsed: dict[str, str] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        parsed[k.strip().upper()] = v.strip()

    boot2 = parsed.get("BOOT2", "")
    exec_match = PS2_EXEC_PATTERN.search(boot2)
    ps2_exec = exec_match.group(1).upper() if exec_match else None
    if not ps2_exec and boot2:
        # Fallback: extract filename from cdrom0:\PATH;1
        cleaned = boot2.replace("\\", "/").split(";", 1)[0]
        ps2_exec = cleaned.rsplit("/", 1)[-1] or None

    return {
        "raw": raw_text,
        "entries": parsed,
        "boot2_raw": boot2,
        "ps2_executable": ps2_exec,
    }


def read_iso_file_bytes(iso: pycdlib.PyCdlib, key_type: str, path: str) -> bytes:
    """Read an entire file from the open PyCdlib instance into memory."""
    buf = io.BytesIO()
    iso.get_file_from_iso_fp(buf, **{key_type: path})
    return buf.getvalue()


def pick_walk_mode(iso: pycdlib.PyCdlib) -> tuple[str, str]:
    """Select the primary filesystem key for PyCdlib walk/get_record."""
    # PS2 reads ISO9660 primarily; fall back to UDF or Joliet if ISO9660 is empty.
    try:
        entries = list(iso.walk(iso_path="/"))
        if any(files for _, _, files in entries):
            return ("iso_path", "iso9660")
    except Exception:
        pass

    if iso.has_udf():
        return ("udf_path", "udf")
    if iso.has_joliet():
        return ("joliet_path", "joliet")
    return ("iso_path", "iso9660")


def inspect_and_extract(
    image_path: Path,
    extracted_dir: Path,
    manifest_path: Path,
    extract_files: bool = True,
) -> dict[str, Any]:
    """Inspect disc image read-only, build manifest JSON, and extract files."""
    if not image_path.is_file():
        raise FileNotFoundError(f"Disc image not found: {image_path}")

    raw_size = image_path.stat().st_size
    iso = pycdlib.PyCdlib()

    with image_path.open("rb") as raw_fp:
        fmt, sector_size, user_off = detect_disc_format(raw_fp)
        if sector_size == LOGICAL_SECTOR_SIZE:
            raw_fp.seek(0, os.SEEK_SET)
            stream: BinaryIO = raw_fp
        else:
            stream = io.BufferedReader(
                RawBinSectorReader(raw_fp, sector_size, user_off)
            )  # type: ignore[assignment]

        iso.open_fp(stream)
        try:
            filesystems = {
                "iso9660": True,
                "joliet": bool(iso.has_joliet()),
                "rock_ridge": bool(iso.has_rock_ridge()),
                "udf": bool(iso.has_udf()),
            }

            key_type, fs_used = pick_walk_mode(iso)

            raw_records: list[tuple[str, int, int]] = []
            for dirpath, _dirlist, filelist in iso.walk(**{key_type: "/"}):
                for fname in filelist:
                    full_iso_path = (
                        f"/{fname}" if dirpath == "/" else f"{dirpath}/{fname}"
                    )
                    rec = iso.get_record(**{key_type: full_iso_path})
                    lba_start = int(rec.extent_location())
                    size_bytes = int(
                        getattr(rec, "data_length", None)
                        if hasattr(rec, "data_length")
                        else rec.get_data_length()
                    )
                    raw_records.append((full_iso_path, size_bytes, lba_start))

            # Locate and parse SYSTEM.CNF
            system_cnf_info: dict[str, Any] = {
                "found": False,
                "raw": "",
                "entries": {},
                "boot2_raw": "",
                "ps2_executable": None,
            }
            for full_iso_path, _sz, _lba in raw_records:
                if strip_iso_version(full_iso_path).upper() == "/SYSTEM.CNF":
                    cnf_bytes = read_iso_file_bytes(iso, key_type, full_iso_path)
                    cnf_text = cnf_bytes.decode("ascii", errors="replace")
                    system_cnf_info = {"found": True, **parse_system_cnf(cnf_text)}
                    break

            ps2_exec = system_cnf_info.get("ps2_executable")

            # Fallback executable detection if SYSTEM.CNF absent or unparsed
            if not ps2_exec:
                for full_iso_path, _sz, _lba in raw_records:
                    m = PS2_EXEC_PATTERN.search(strip_iso_version(full_iso_path))
                    if m:
                        ps2_exec = m.group(1).upper()
                        break

            manifest_files: list[dict[str, Any]] = []
            containers: list[dict[str, Any]] = []

            for full_iso_path, size_bytes, lba_start in sorted(
                raw_records, key=lambda item: (item[2], item[0])
            ):
                clean = strip_iso_version(full_iso_path)
                lba_sectors = (size_bytes + LOGICAL_SECTOR_SIZE - 1) // LOGICAL_SECTOR_SIZE
                category = classify_entry(clean, ps2_exec)
                entry = {
                    "iso_path": full_iso_path,
                    "clean_path": clean,
                    "size_bytes": size_bytes,
                    "lba_start": lba_start,
                    "lba_sectors": lba_sectors,
                    "category": category,
                }
                manifest_files.append(entry)
                if category in {"container", "container_or_data", "rom_or_data"}:
                    containers.append(entry)

            # Extract files cleanly into extracted/
            extracted_paths: list[str] = []
            if extract_files:
                extracted_dir.mkdir(parents=True, exist_ok=True)
                for entry in manifest_files:
                    rel_clean = entry["clean_path"].lstrip("/")
                    dest = extracted_dir / Path(rel_clean)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open("wb") as out_fp:
                        iso.get_file_from_iso_fp(
                            out_fp, **{key_type: entry["iso_path"]}
                        )
                    extracted_paths.append(str(dest))

            manifest: dict[str, Any] = {
                "image_path": str(image_path.resolve()),
                "image_size_bytes": raw_size,
                "sector_format": fmt,
                "physical_sector_size": sector_size,
                "logical_sector_size": LOGICAL_SECTOR_SIZE,
                "filesystem_walked": fs_used,
                "filesystems_detected": filesystems,
                "system_cnf": system_cnf_info,
                "ps2_executable": ps2_exec,
                "total_files": len(manifest_files),
                "total_data_bytes": sum(f["size_bytes"] for f in manifest_files),
                "containers_count": len(containers),
                "containers": containers,
                "files": manifest_files,
                "extracted_dir": str(extracted_dir.resolve()) if extract_files else None,
                "extracted_count": len(extracted_paths),
            }

            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return manifest
        finally:
            iso.close()


def print_summary(manifest: dict[str, Any], manifest_path: Path) -> None:
    """Print a concise, readable summary of the disc inspection."""
    print(f"Disc Image     : {manifest['image_path']}")
    print(
        f"Image Size     : {manifest['image_size_bytes']:,} bytes "
        f"({manifest['sector_format']}, FS={manifest['filesystem_walked']})"
    )
    print(f"PS2 Executable : {manifest.get('ps2_executable') or 'Not detected'}")

    cnf = manifest["system_cnf"]
    if cnf.get("found"):
        print("SYSTEM.CNF     :")
        for line in cnf["raw"].strip().splitlines():
            print(f"  {line}")
    else:
        print("SYSTEM.CNF     : Not found")

    print(
        f"\nFiles Found ({manifest['total_files']} total, "
        f"{manifest['total_data_bytes']:,} bytes):"
    )
    print(f"{'LBA Start':>10}  {'Size (bytes)':>14}  {'Category':<18}  {'ISO Path'}")
    print("-" * 76)
    for f in manifest["files"]:
        print(
            f"{f['lba_start']:>10}  {f['size_bytes']:>14,}  "
            f"{f['category']:<18}  {f['iso_path']}"
        )

    if manifest["containers"]:
        print(f"\nData Containers / ROM-style Images ({manifest['containers_count']}):")
        for c in manifest["containers"]:
            print(
                f"  - {c['clean_path']} ({c['size_bytes']:,} bytes, LBA {c['lba_start']})"
            )

    print(f"\nManifest saved : {manifest_path}")
    if manifest.get("extracted_dir"):
        print(
            f"Extracted      : {manifest['extracted_count']} files -> "
            f"{manifest['extracted_dir']}"
        )


def build_arg_parser(default_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a KOF 2002 PS2 ISO/BIN read-only and extract its files."
    )
    parser.add_argument(
        "--iso",
        type=Path,
        default=None,
        help="Path to the .iso (or raw .bin) disc image. Auto-detects in G:\\KOF if omitted.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=default_root / "extracted",
        help="Destination directory for extracted files (default: <workspace>/extracted).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=default_root / "scripts" / "iso_manifest.json",
        help="Output path for the JSON manifest (default: <workspace>/scripts/iso_manifest.json).",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Only inspect and write iso_manifest.json without extracting files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent
    parser = build_arg_parser(default_root)
    args = parser.parse_args(argv)

    image_path = args.iso
    if image_path is None:
        image_path = find_disc_image(default_root)
        if image_path is None:
            print(
                f"ERROR: No .iso or .bin disc image found in {default_root}.\n"
                "Place the KOF 2002 PS2 .iso/.bin in G:\\KOF or pass --iso <path>.",
                file=sys.stderr,
            )
            return 2

    manifest = inspect_and_extract(
        image_path=image_path,
        extracted_dir=args.out_dir,
        manifest_path=args.manifest,
        extract_files=not args.no_extract,
    )
    print_summary(manifest, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

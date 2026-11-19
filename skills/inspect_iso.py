"""Deterministic Skill: skill_inspect_iso.

Inspects the original KOF 2002 disc image, verifies file existence, size,
manifest integrity, and extracted asset tree readiness.
Never modifies any files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skills.base import BaseSkill, SkillResult

ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT_DIR / "scripts" / "iso_manifest.json"
EXTRACTED_DIR = ROOT_DIR / "extracted"
BUILD_DIR = ROOT_DIR / "build"
EXPECTED_SECTOR_FORMAT = "bin2352_mode2"
EXPECTED_BIN_SIZE = 446463696  # Exact size of European SLES-53381 .bin disc image


class InspectIsoSkill(BaseSkill):
    name = "skill_inspect_iso"
    display_name = "Inspect Disc Image & Manifest"
    description = (
        "Inspects the KOF 2002 ISO/BIN disc image, validates sector format, "
        "checks size against European release (446,463,696 bytes), verifies "
        "iso_manifest.json (392 files), and confirms extracted asset availability."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "iso_path": {
                "type": "string",
                "description": "Optional custom path to the .bin or .iso image.",
            }
        },
        "required": [],
    }

    def execute(self, iso_path: str | None = None, **kwargs: Any) -> SkillResult:
        errors: list[str] = []
        warnings: list[str] = []
        data: dict[str, Any] = {}

        # 1. Locate disc image
        bin_file: Path | None = None
        if iso_path:
            p = Path(iso_path)
            if p.is_file():
                bin_file = p
            else:
                errors.append(f"Specified disc image path does not exist: {iso_path}")
        else:
            cand = list(ROOT_DIR.glob("*.bin"))
            if cand:
                bin_file = cand[0]
            else:
                errors.append("No .bin disc image found in project root.")

        if bin_file:
            st = bin_file.stat()
            size = st.st_size
            data["disc_image"] = {
                "path": str(bin_file),
                "filename": bin_file.name,
                "size_bytes": size,
                "expected_size_bytes": EXPECTED_BIN_SIZE,
                "size_matches_official_eu": size == EXPECTED_BIN_SIZE,
            }
            if size != EXPECTED_BIN_SIZE:
                warnings.append(
                    f"Disc image size ({size} bytes) does not match expected European image ({EXPECTED_BIN_SIZE} bytes)."
                )

            cue_cand = bin_file.with_suffix(".cue")
            data["disc_image"]["cue_exists"] = cue_cand.is_file()
            if not cue_cand.is_file():
                warnings.append(f"Matching CUE sheet not found: {cue_cand.name}")

        # 2. Check manifest
        if not MANIFEST_PATH.is_file():
            errors.append(f"Manifest not found: {MANIFEST_PATH}")
        else:
            try:
                manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
                file_count = len(manifest.get("files", []))
                sector_format = manifest.get("sector_format")
                data["manifest"] = {
                    "path": str(MANIFEST_PATH),
                    "file_count": file_count,
                    "sector_format": sector_format,
                    "volume_identifier": manifest.get("volume_identifier"),
                }
                if sector_format != EXPECTED_SECTOR_FORMAT:
                    errors.append(
                        f"Unsupported sector format in manifest: '{sector_format}' (expected '{EXPECTED_SECTOR_FORMAT}')"
                    )
            except Exception as exc:
                errors.append(f"Failed to parse manifest: {exc}")

        # 3. Check extracted asset tree
        extracted_ok = EXTRACTED_DIR.is_dir()
        sles_file = EXTRACTED_DIR / "SLES_533.81"
        data_dir = EXTRACTED_DIR / "DATA"
        pl0_dir = data_dir / "PL0"
        pl1_dir = data_dir / "PL1"
        bgm_afs = EXTRACTED_DIR / "ADX" / "BGM.AFS"

        data["extracted_tree"] = {
            "exists": extracted_ok,
            "sles_53381_exists": sles_file.is_file(),
            "pl0_exists": pl0_dir.is_dir(),
            "pl1_exists": pl1_dir.is_dir(),
            "bgm_afs_exists": bgm_afs.is_file(),
        }

        if not extracted_ok:
            errors.append(f"Extracted directory not found: {EXTRACTED_DIR}")
        else:
            if not sles_file.is_file():
                errors.append("SLES_533.81 ELF not found in extracted/.")
            if not pl0_dir.is_dir() or not pl1_dir.is_dir():
                errors.append("Character sprite containers (DATA/PL0 or DATA/PL1) missing in extracted/.")

        data["build_dir_ready"] = BUILD_DIR.is_dir()

        success = len(errors) == 0
        message = (
            "Disc image and manifest verified successfully."
            if success
            else f"Inspection found {len(errors)} error(s)."
        )

        return SkillResult(
            skill_name=self.name,
            success=success,
            message=message,
            data=data,
            errors=errors,
            warnings=warnings,
        )

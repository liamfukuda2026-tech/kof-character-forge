"""End-to-End Integration Test for Terry Legendary (P03).

Verifies the full pipeline:
  1. Inspect ISO image
  2. Create 'Terry Legendary' based on Terry Bogard (P03)
  3. Import sample sprite (ensuring 16x16 grid, 16-color palette, index 0 transparency)
  4. Run validation on the character project
  5. Build patch and reconstruct Mode 2 Form 1 ISO image
  6. Verify Mode 2 Form 1 EDC/ECC integrity (ECMA-130 CRC-32 and Reed-Solomon P/Q)
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core.characters import resolve_character_slot
from core.skills import execute_skill
from skills.base import SkillResult

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestTerryLegendaryE2E(unittest.TestCase):
    def setUp(self):
        self.test_ws = Path(tempfile.mkdtemp(prefix="test_terry_e2e_ws_"))
        self.test_build = Path(tempfile.mkdtemp(prefix="test_terry_e2e_build_"))

    def tearDown(self):
        shutil.rmtree(self.test_ws, ignore_errors=True)
        shutil.rmtree(self.test_build, ignore_errors=True)

    def test_e2e_terry_legendary_pipeline(self):
        # 1. Inspect ISO
        res_inspect: SkillResult = execute_skill("skill_inspect_iso")
        self.assertTrue(res_inspect.success, f"Inspect failed: {res_inspect.errors}")
        self.assertTrue(res_inspect.data["disc_image"]["size_matches_official_eu"])

        # 2. Create Project
        res_create: SkillResult = execute_skill(
            "skill_create_project",
            project_name="Terry Legendary",
            base_slot="Terry Bogard",
            author="Verification Agent",
            description="E2E Integration character based on Terry Bogard P03.",
            workspace_dir=self.test_ws,
        )
        self.assertTrue(res_create.success, f"Create project failed: {res_create.errors}")
        self.assertEqual(res_create.data["base_slot"]["char_id"], 3)
        self.assertEqual(res_create.data["max_memory_bytes"], 1034776)

        # 3. Import Sample Sprite
        sample_path = ROOT_DIR / "custom_character_demo" / "frame_0000.png"
        self.assertTrue(sample_path.is_file(), f"Sample sprite missing: {sample_path}")

        res_import: SkillResult = execute_skill(
            "skill_import_sprites",
            project_name="Terry Legendary",
            source_path=str(sample_path),
            frame_index=0,
            palette_button="A",
            quantize=True,
            workspace_dir=self.test_ws,
        )
        self.assertTrue(res_import.success, f"Import sprite failed: {res_import.errors}")
        self.assertEqual(res_import.data["imported_count"], 1)

        # 4. Validate Character
        res_val: SkillResult = execute_skill(
            "skill_validate_character",
            project_name="Terry Legendary",
            workspace_dir=self.test_ws,
        )
        self.assertTrue(res_val.success, f"Validation failed: {res_val.errors}")
        self.assertTrue(res_val.data["is_valid"])
        self.assertGreater(res_val.data["metrics"]["headroom_bytes"], 0)

        # 5. Build Patch (Reconstruct ISO with EDC/ECC)
        out_bin = self.test_build / "kof2002_terry_e2e.bin"
        res_build: SkillResult = execute_skill(
            "skill_build_patch",
            project_name="Terry Legendary",
            output_bin=str(out_bin),
            dry_run=False,
            workspace_dir=self.test_ws,
        )
        self.assertTrue(res_build.success, f"Build patch failed: {res_build.errors}")
        self.assertTrue(out_bin.is_file())
        self.assertEqual(out_bin.stat().st_size, 446463696)

        # 6. Verify Mode 2 Form 1 EDC/ECC
        m03_spec = importlib.util.spec_from_file_location("m03_val", str(ROOT_DIR / "scripts" / "03_build_mod_iso.py"))
        m03 = importlib.util.module_from_spec(m03_spec)
        m03_spec.loader.exec_module(m03)

        m07_spec = importlib.util.spec_from_file_location("m07_val", str(ROOT_DIR / "scripts" / "07_disc_editor.py"))
        m07 = importlib.util.module_from_spec(m07_spec)
        m07_spec.loader.exec_module(m07)

        disc_proj = m07.Project()
        verify_ok = disc_proj.verify(out_bin, log=lambda *args: None)
        self.assertTrue(verify_ok, "Disc verification failed")

        # Specific sector check on Terry Bogard (P03, 506 sectors)
        RAW = 2352
        entry = disc_proj.entry("/DATA/PL0/P03.BIN")
        lba_start = entry["lba_start"]
        lba_sectors = entry["lba_sectors"]

        with out_bin.open("rb") as fo:
            for i in range(lba_sectors):
                fo.seek((lba_start + i) * RAW)
                sec = fo.read(RAW)
                sync_ok, edc_ok, ecc_ok = m03.verify_mode2_form1_sector(sec)
                self.assertTrue(sync_ok and edc_ok and ecc_ok, f"Sector {lba_start + i} failed Mode 2 Form 1 EDC/ECC")


if __name__ == "__main__":
    unittest.main()

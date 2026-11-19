"""Unit tests for deterministic skills in skills/ and core.skills."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
import numpy as np
from PIL import Image

from core.forge_project import ForgeProject
from core.skills import execute_skill, registry
from skills.base import SkillResult


class TestDeterministicSkills(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_skills_ws_"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. skill_inspect_iso
    # -------------------------------------------------------------------------
    def test_inspect_iso_skill(self):
        res: SkillResult = execute_skill("skill_inspect_iso")
        self.assertTrue(res.success)
        self.assertIn("disc_image", res.data)
        self.assertIn("manifest", res.data)
        self.assertIn("extracted_tree", res.data)

        # 446MB European disc image
        self.assertTrue(res.data["disc_image"]["size_matches_official_eu"])
        self.assertEqual(res.data["manifest"]["sector_format"], "bin2352_mode2")
        self.assertEqual(res.data["manifest"]["file_count"], 392)

    # -------------------------------------------------------------------------
    # 2. skill_create_project
    # -------------------------------------------------------------------------
    def test_create_project_skill(self):
        res: SkillResult = execute_skill(
            "skill_create_project",
            project_name="Skill Terry",
            base_slot="Terry Bogard",
            author="Tester",
            workspace_dir=self.test_dir,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.data["project_name"], "Skill Terry")
        self.assertEqual(res.data["base_slot"]["char_id"], 3)
        self.assertTrue(Path(res.data["project_dir"]).is_dir())

    def test_create_project_invalid_slot_fails(self):
        res: SkillResult = execute_skill(
            "skill_create_project",
            project_name="Invalid Slot Test",
            base_slot="Nonexistent Fighter",
            workspace_dir=self.test_dir,
        )
        self.assertFalse(res.success)
        self.assertTrue(len(res.errors) > 0)

    # -------------------------------------------------------------------------
    # 3. skill_import_sprites
    # -------------------------------------------------------------------------
    def test_import_sprites_quantization_and_transparency(self):
        # Create project first
        proj = ForgeProject.create("Import Test", "Terry Bogard", workspace_dir=self.test_dir)

        # Create a synthetic RGBA frame with magenta chroma key background (#FF00FF)
        test_frame_path = self.test_dir / "raw_frame.png"
        raw_im = Image.new("RGBA", (64, 64), (255, 0, 255, 255))
        # Draw some character pixels in center
        for y in range(16, 48):
            for x in range(16, 48):
                raw_im.putpixel((x, y), (220, 100, 50, 255))
        raw_im.save(test_frame_path, format="PNG")

        res: SkillResult = execute_skill(
            "skill_import_sprites",
            project_name=str(proj.project_dir),
            source_path=str(test_frame_path),
            frame_index=0,
            palette_button="A",
            quantize=True,
        )

        self.assertTrue(res.success)
        self.assertEqual(res.data["imported_count"], 1)

        # Inspect the saved indexed PNG
        saved_png = proj.frames_dir / "frame_0000.png"
        self.assertTrue(saved_png.is_file())

        with Image.open(saved_png) as saved:
            self.assertEqual(saved.format, "PNG")
            self.assertEqual(saved.mode, "P")
            self.assertEqual(saved.info.get("transparency"), 0)

            arr = np.array(saved, dtype=np.uint8)
            # Magenta border should have mapped to index 0 (transparent)
            self.assertEqual(arr[0, 0], 0)
            # Center pixels should be mapped to opaque indices 1..15
            self.assertGreater(arr[32, 32], 0)
            self.assertLessEqual(int(arr.max()), 15)

    # -------------------------------------------------------------------------
    # 4. skill_validate_character
    # -------------------------------------------------------------------------
    def test_validate_character_success_on_valid_project(self):
        proj = ForgeProject.create("Val Success Test", "terry", workspace_dir=self.test_dir)

        # Add valid 64x64 indexed 16-color frame (multiples of 16)
        valid_im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for y in range(16, 48):
            for x in range(16, 48):
                valid_im.putpixel((x, y), (200, 150, 80, 255))
        proj.add_frame(0, valid_im, quantize_if_needed=True)

        res: SkillResult = execute_skill(
            "skill_validate_character",
            project_name=str(proj.project_dir),
        )

        self.assertTrue(res.success)
        self.assertTrue(res.data["is_valid"])
        metrics = res.data["metrics"]
        self.assertGreater(metrics["headroom_bytes"], 0)
        self.assertEqual(metrics["total_custom_frames"], 1)

    def test_validate_character_detects_tile_alignment_error(self):
        proj = ForgeProject.create("Val Tile Error Test", "terry", workspace_dir=self.test_dir)

        # Create an image that is NOT a multiple of 16 (e.g. 50x50)
        bad_dim_im = Image.new("P", (50, 50))
        bad_dim_im.putpalette([ch for i in range(16) for ch in (i * 16, i * 16, i * 16)])
        bad_path = proj.frames_dir / "frame_0000.png"
        bad_dim_im.save(bad_path, format="PNG", transparency=0)

        res: SkillResult = execute_skill(
            "skill_validate_character",
            project_name=str(proj.project_dir),
        )

        self.assertFalse(res.success)
        self.assertFalse(res.data["is_valid"])
        error_str = " ".join(res.errors)
        self.assertIn("dimensions 50x50 px are not multiples of 16", error_str)

    def test_validate_character_detects_palette_index_overflow(self):
        proj = ForgeProject.create("Val Pal Overflow", "terry", workspace_dir=self.test_dir)

        # Create an image with pixel index 25 > 15
        arr = np.zeros((32, 32), dtype=np.uint8)
        arr[10:20, 10:20] = 25  # Exceeds 15
        bad_pal_im = Image.fromarray(arr, mode="P")
        pal_256 = [ch for i in range(256) for ch in (i, i, i)]
        bad_pal_im.putpalette(pal_256)
        bad_path = proj.frames_dir / "frame_0000.png"
        bad_pal_im.save(bad_path, format="PNG", transparency=0)

        res: SkillResult = execute_skill(
            "skill_validate_character",
            project_name=str(proj.project_dir),
        )

        self.assertFalse(res.success)
        error_str = " ".join(res.errors)
        self.assertIn("uses palette index 25 > 15", error_str)

    # -------------------------------------------------------------------------
    # 5. skill_build_patch
    # -------------------------------------------------------------------------
    def test_build_patch_preflight_blocks_invalid_project(self):
        proj = ForgeProject.create("Build Block Test", "terry", workspace_dir=self.test_dir)

        # Introduce an invalid frame
        bad_dim_im = Image.new("P", (37, 37))
        bad_path = proj.frames_dir / "frame_0000.png"
        bad_dim_im.save(bad_path, format="PNG")

        res: SkillResult = execute_skill(
            "skill_build_patch",
            project_name=str(proj.project_dir),
            dry_run=True,
        )

        self.assertFalse(res.success)
        self.assertIn("Preflight validation failed", res.message)

    def test_build_patch_dry_run_success(self):
        proj = ForgeProject.create("Build Dry Run Test", "Terry Bogard", workspace_dir=self.test_dir)

        # Add valid 64x64 frame
        valid_im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for y in range(16, 48):
            for x in range(16, 48):
                valid_im.putpixel((x, y), (180, 120, 60, 255))
        proj.add_frame(0, valid_im, quantize_if_needed=True)

        res: SkillResult = execute_skill(
            "skill_build_patch",
            project_name=str(proj.project_dir),
            dry_run=True,
        )

        self.assertTrue(res.success)
        self.assertTrue(res.data["dry_run"])
        self.assertIn("DATA/PL0/P03.BIN", res.data["target_file"])


if __name__ == "__main__":
    unittest.main()

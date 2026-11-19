"""Unit tests for core.forge_project (ForgeProject management)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from core.forge_project import ForgeProject


class TestForgeProject(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_forge_proj_"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_project_from_slot(self):
        proj = ForgeProject.create(
            name="Terry Legendary",
            base_slot="Terry Bogard",
            author="Modder",
            description="Legendary Terry mod",
            workspace_dir=self.test_dir,
        )

        self.assertEqual(proj.name, "Terry Legendary")
        self.assertEqual(proj.slug, "terry_legendary")
        self.assertEqual(proj.base_slot.char_id, 3)
        self.assertEqual(proj.base_slot.display_name, "Terry Bogard")

        # Directories
        self.assertTrue(proj.project_dir.is_dir())
        self.assertTrue(proj.frames_dir.is_dir())
        self.assertTrue(proj.audio_dir.is_dir())
        self.assertTrue(proj.palettes_dir.is_dir())
        self.assertTrue(proj.base_dir.is_dir())
        self.assertTrue(proj.json_path.is_file())
        self.assertTrue((proj.base_dir / "base_character.json").is_file())

        # Palettes extracted from FIRST.GRC
        pal_a = proj.get_palette("A")
        self.assertEqual(len(pal_a), 16)
        self.assertEqual(pal_a[0], [255, 0, 255])  # Index 0 transparent

    def test_create_duplicate_raises_unless_overwrite(self):
        ForgeProject.create("Duplicate Test", "terry", workspace_dir=self.test_dir)
        with self.assertRaises(FileExistsError):
            ForgeProject.create("Duplicate Test", "terry", workspace_dir=self.test_dir, overwrite=False)

        # Should succeed with overwrite=True
        recreated = ForgeProject.create("Duplicate Test", "terry", workspace_dir=self.test_dir, overwrite=True)
        self.assertIsNotNone(recreated)

    def test_load_and_save_project(self):
        proj = ForgeProject.create("Save Load Test", "rugal", workspace_dir=self.test_dir)
        proj.data["author"] = "Original Author"
        proj.save()

        loaded = ForgeProject.load("save_load_test", workspace_dir=self.test_dir)
        self.assertEqual(loaded.name, "Save Load Test")
        self.assertEqual(loaded.base_slot.char_id, 0x27)
        self.assertEqual(loaded.data["author"], "Original Author")

    def test_add_and_list_frames(self):
        proj = ForgeProject.create("Frames Test", "terry", workspace_dir=self.test_dir)

        # Create a synthetic 16-color test frame
        test_im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        # Draw some opaque square
        for y in range(16, 48):
            for x in range(16, 48):
                test_im.putpixel((x, y), (200, 50, 50, 255))

        saved_path = proj.add_frame(0, test_im, quantize_if_needed=True)
        self.assertTrue(saved_path.is_file())
        self.assertEqual(saved_path.name, "frame_0000.png")

        frames = proj.list_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["frame_index"], 0)
        self.assertEqual(frames[0]["width"], 64)
        self.assertEqual(frames[0]["height"], 64)
        self.assertEqual(frames[0]["mode"], "P")

        # Get frame path
        self.assertIsNotNone(proj.get_frame_path(0))
        self.assertIsNone(proj.get_frame_path(999))

        # Remove frame
        removed = proj.remove_frame(0)
        self.assertTrue(removed)
        self.assertEqual(len(proj.list_frames()), 0)

    def test_palette_operations(self):
        proj = ForgeProject.create("Palette Test", "terry", workspace_dir=self.test_dir)
        custom_cols = [[255, 0, 255]] + [[i * 10, i * 10, i * 10] for i in range(1, 16)]
        proj.set_palette("B", custom_cols)

        loaded_b = proj.get_palette("B")
        self.assertEqual(len(loaded_b), 16)
        self.assertEqual(loaded_b[0], [255, 0, 255])
        self.assertEqual(loaded_b[1], [10, 10, 10])

    def test_export_and_import_kofchar_package(self):
        proj = ForgeProject.create("Package Test", "terry", author="Elton", workspace_dir=self.test_dir)

        # Add a test frame
        test_im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        for y in range(8, 24):
            for x in range(8, 24):
                test_im.putpixel((x, y), (100, 150, 200, 255))
        proj.add_frame(1, test_im, quantize_if_needed=True)

        zip_out = self.test_dir / "package_test.kofchar"
        exported = proj.export_package(zip_out)
        self.assertTrue(exported.is_file())
        self.assertGreater(exported.stat().st_size, 0)

        # Import into an isolated second workspace
        second_ws = Path(tempfile.mkdtemp(prefix="test_forge_import_ws_"))
        try:
            imported = ForgeProject.import_package(exported, workspace_dir=second_ws)
            self.assertEqual(imported.name, "Package Test")
            self.assertEqual(imported.base_slot.char_id, 3)
            self.assertEqual(imported.data["author"], "Elton")
            frames = imported.list_frames()
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0]["frame_index"], 1)
        finally:
            shutil.rmtree(second_ws, ignore_errors=True)

    def test_list_and_delete_projects(self):
        proj1 = ForgeProject.create("List Test 1", "terry", workspace_dir=self.test_dir)
        proj2 = ForgeProject.create("List Test 2", "rugal", workspace_dir=self.test_dir)

        projects = ForgeProject.list_projects(workspace_dir=self.test_dir)
        self.assertEqual(len(projects), 2)
        names = {p["name"] for p in projects}
        self.assertIn("List Test 1", names)
        self.assertIn("List Test 2", names)

        # Delete one
        proj1.delete()
        remaining = ForgeProject.list_projects(workspace_dir=self.test_dir)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "List Test 2")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for core.characters slot database and lookup."""

from __future__ import annotations

import unittest
from core.characters import CharacterSlot, list_character_slots, resolve_character_slot


class TestCharacterDatabase(unittest.TestCase):
    def test_all_46_slots_present(self):
        slots = list_character_slots()
        self.assertEqual(len(slots), 46)

    def test_resolve_by_slot_id_int(self):
        terry = resolve_character_slot(3)
        self.assertEqual(terry.display_name, "Terry Bogard")
        self.assertEqual(terry.rel_bin_path, "DATA/PL0/P03.BIN")
        self.assertEqual(terry.size_bytes, 1034776)

        rugal = resolve_character_slot(0x27)
        self.assertEqual(rugal.display_name, "Omega Rugal")
        self.assertEqual(rugal.rel_bin_path, "DATA/PL1/P27.BIN")
        self.assertEqual(rugal.size_bytes, 1604064)

    def test_resolve_by_hex_string(self):
        slot = resolve_character_slot("0x03")
        self.assertEqual(slot.display_name, "Terry Bogard")

        slot_p = resolve_character_slot("P27")
        self.assertEqual(slot_p.display_name, "Omega Rugal")

    def test_resolve_by_slug_and_name(self):
        terry_slug = resolve_character_slot("terry")
        self.assertEqual(terry_slug.char_id, 3)

        terry_name = resolve_character_slot("Terry Bogard")
        self.assertEqual(terry_name.char_id, 3)

        rugal_alias = resolve_character_slot("omega rugal")
        self.assertEqual(rugal_alias.char_id, 0x27)

    def test_resolve_invalid_slot_raises(self):
        with self.assertRaises(KeyError):
            resolve_character_slot("nonexistent_character_xyz")
        with self.assertRaises(KeyError):
            resolve_character_slot(999)


if __name__ == "__main__":
    unittest.main()

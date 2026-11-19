"""KOF 2002 (PS2, SLES-53381) - Character Slot Database and Lookup.

Contains verified mappings for all 46 playable/boss character slots on the disc,
including original PXX.BIN relative paths, sizes in bytes, and default A/B/C/D
palette indices from SLES_533.81.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CharacterSlot:
    char_id: int
    char_id_hex: str
    slug: str
    display_name: str
    rel_bin_path: str
    size_bytes: int
    base_palettes: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# All 46 character slots verified against SLES_533.81 at 0x3E6960 and iso_manifest.json
CHARACTER_SLOTS_DATA: list[dict[str, Any]] = [
    {"char_id": 0x00, "slug": "kyo", "display_name": "Kyo Kusanagi", "rel_bin_path": "DATA/PL0/P00.BIN", "size_bytes": 1241024, "base_palettes": {"A": 256, "B": 272, "C": 288, "D": 304}},
    {"char_id": 0x01, "slug": "benimaru", "display_name": "Benimaru Nikaido", "rel_bin_path": "DATA/PL0/P01.BIN", "size_bytes": 835272, "base_palettes": {"A": 320, "B": 336, "C": 352, "D": 368}},
    {"char_id": 0x02, "slug": "daimon", "display_name": "Goro Daimon", "rel_bin_path": "DATA/PL0/P02.BIN", "size_bytes": 867016, "base_palettes": {"A": 384, "B": 400, "C": 416, "D": 432}},
    {"char_id": 0x03, "slug": "terry", "display_name": "Terry Bogard", "rel_bin_path": "DATA/PL0/P03.BIN", "size_bytes": 1034776, "base_palettes": {"A": 448, "B": 464, "C": 480, "D": 496}},
    {"char_id": 0x04, "slug": "andy", "display_name": "Andy Bogard", "rel_bin_path": "DATA/PL0/P04.BIN", "size_bytes": 999680, "base_palettes": {"A": 512, "B": 528, "C": 544, "D": 560}},
    {"char_id": 0x05, "slug": "joe", "display_name": "Joe Higashi", "rel_bin_path": "DATA/PL0/P05.BIN", "size_bytes": 1052016, "base_palettes": {"A": 576, "B": 592, "C": 608, "D": 624}},
    {"char_id": 0x06, "slug": "ryo", "display_name": "Ryo Sakazaki", "rel_bin_path": "DATA/PL0/P06.BIN", "size_bytes": 923768, "base_palettes": {"A": 640, "B": 656, "C": 672, "D": 688}},
    {"char_id": 0x07, "slug": "robert", "display_name": "Robert Garcia", "rel_bin_path": "DATA/PL0/P07.BIN", "size_bytes": 1321840, "base_palettes": {"A": 704, "B": 720, "C": 736, "D": 752}},
    {"char_id": 0x08, "slug": "yuri", "display_name": "Yuri Sakazaki", "rel_bin_path": "DATA/PL0/P08.BIN", "size_bytes": 756152, "base_palettes": {"A": 768, "B": 784, "C": 800, "D": 816}},
    {"char_id": 0x09, "slug": "leona", "display_name": "Leona Heidern", "rel_bin_path": "DATA/PL0/P09.BIN", "size_bytes": 1217576, "base_palettes": {"A": 832, "B": 848, "C": 864, "D": 880}},
    {"char_id": 0x0A, "slug": "ralf", "display_name": "Ralf Jones", "rel_bin_path": "DATA/PL0/P0A.BIN", "size_bytes": 1070376, "base_palettes": {"A": 896, "B": 912, "C": 928, "D": 944}},
    {"char_id": 0x0B, "slug": "clark", "display_name": "Clark Still", "rel_bin_path": "DATA/PL0/P0B.BIN", "size_bytes": 943744, "base_palettes": {"A": 960, "B": 976, "C": 992, "D": 1008}},
    {"char_id": 0x0C, "slug": "athena", "display_name": "Athena Asamiya", "rel_bin_path": "DATA/PL0/P0C.BIN", "size_bytes": 1223544, "base_palettes": {"A": 1024, "B": 1040, "C": 1056, "D": 1072}},
    {"char_id": 0x0D, "slug": "kensou", "display_name": "Sie Kensou", "rel_bin_path": "DATA/PL0/P0D.BIN", "size_bytes": 960384, "base_palettes": {"A": 1088, "B": 1104, "C": 1120, "D": 1136}},
    {"char_id": 0x0E, "slug": "chin", "display_name": "Chin Gentsai", "rel_bin_path": "DATA/PL0/P0E.BIN", "size_bytes": 956776, "base_palettes": {"A": 1152, "B": 1168, "C": 1184, "D": 1200}},
    {"char_id": 0x0F, "slug": "chizuru", "display_name": "Chizuru Kagura", "rel_bin_path": "DATA/PL0/P0F.BIN", "size_bytes": 827080, "base_palettes": {"A": 1216, "B": 1232, "C": 1248, "D": 1264}},
    {"char_id": 0x10, "slug": "mai", "display_name": "Mai Shiranui", "rel_bin_path": "DATA/PL0/P10.BIN", "size_bytes": 853040, "base_palettes": {"A": 1280, "B": 1296, "C": 1312, "D": 1328}},
    {"char_id": 0x11, "slug": "kim", "display_name": "Kim Kaphwan", "rel_bin_path": "DATA/PL0/P11.BIN", "size_bytes": 977720, "base_palettes": {"A": 1344, "B": 1360, "C": 1376, "D": 1392}},
    {"char_id": 0x12, "slug": "chang", "display_name": "Chang Koehan", "rel_bin_path": "DATA/PL0/P12.BIN", "size_bytes": 1279048, "base_palettes": {"A": 1408, "B": 1424, "C": 1440, "D": 1456}},
    {"char_id": 0x13, "slug": "choi", "display_name": "Choi Bounge", "rel_bin_path": "DATA/PL0/P13.BIN", "size_bytes": 891520, "base_palettes": {"A": 1472, "B": 1488, "C": 1504, "D": 1520}},
    {"char_id": 0x14, "slug": "iori", "display_name": "Iori Yagami", "rel_bin_path": "DATA/PL0/P14.BIN", "size_bytes": 1690136, "base_palettes": {"A": 1536, "B": 1552, "C": 1568, "D": 1584}},
    {"char_id": 0x15, "slug": "mature", "display_name": "Mature", "rel_bin_path": "DATA/PL1/P15.BIN", "size_bytes": 1260336, "base_palettes": {"A": 1600, "B": 1616, "C": 1632, "D": 1648}},
    {"char_id": 0x16, "slug": "vice", "display_name": "Vice", "rel_bin_path": "DATA/PL1/P16.BIN", "size_bytes": 998784, "base_palettes": {"A": 1664, "B": 1680, "C": 1696, "D": 1712}},
    {"char_id": 0x17, "slug": "yamazaki", "display_name": "Ryuji Yamazaki", "rel_bin_path": "DATA/PL1/P17.BIN", "size_bytes": 850448, "base_palettes": {"A": 1728, "B": 1744, "C": 1760, "D": 1776}},
    {"char_id": 0x18, "slug": "mary", "display_name": "Blue Mary", "rel_bin_path": "DATA/PL1/P18.BIN", "size_bytes": 1377144, "base_palettes": {"A": 1792, "B": 1808, "C": 1824, "D": 1840}},
    {"char_id": 0x19, "slug": "billy", "display_name": "Billy Kane", "rel_bin_path": "DATA/PL1/P19.BIN", "size_bytes": 1137808, "base_palettes": {"A": 1856, "B": 1872, "C": 1888, "D": 1904}},
    {"char_id": 0x1A, "slug": "yashiro", "display_name": "Yashiro Nanakase", "rel_bin_path": "DATA/PL1/P1A.BIN", "size_bytes": 1541344, "base_palettes": {"A": 1920, "B": 1936, "C": 1952, "D": 1968}},
    {"char_id": 0x1B, "slug": "shermie", "display_name": "Shermie", "rel_bin_path": "DATA/PL1/P1B.BIN", "size_bytes": 1290400, "base_palettes": {"A": 1984, "B": 2000, "C": 2016, "D": 2032}},
    {"char_id": 0x1C, "slug": "chris", "display_name": "Chris", "rel_bin_path": "DATA/PL1/P1C.BIN", "size_bytes": 1627456, "base_palettes": {"A": 2048, "B": 2064, "C": 2080, "D": 2096}},
    {"char_id": 0x1D, "slug": "k_dash", "display_name": "K'", "rel_bin_path": "DATA/PL1/P1D.BIN", "size_bytes": 1461640, "base_palettes": {"A": 2112, "B": 2128, "C": 2144, "D": 2160}},
    {"char_id": 0x1E, "slug": "maxima", "display_name": "Maxima", "rel_bin_path": "DATA/PL1/P1E.BIN", "size_bytes": 1307208, "base_palettes": {"A": 2176, "B": 2192, "C": 2208, "D": 2224}},
    {"char_id": 0x1F, "slug": "whip", "display_name": "Whip", "rel_bin_path": "DATA/PL1/P1F.BIN", "size_bytes": 1680496, "base_palettes": {"A": 2240, "B": 2256, "C": 2272, "D": 2288}},
    {"char_id": 0x20, "slug": "vanessa", "display_name": "Vanessa", "rel_bin_path": "DATA/PL1/P20.BIN", "size_bytes": 1746456, "base_palettes": {"A": 2304, "B": 2320, "C": 2336, "D": 2352}},
    {"char_id": 0x21, "slug": "seth", "display_name": "Seth", "rel_bin_path": "DATA/PL1/P21.BIN", "size_bytes": 1078056, "base_palettes": {"A": 2368, "B": 2384, "C": 2400, "D": 2416}},
    {"char_id": 0x22, "slug": "ramon", "display_name": "Ramon", "rel_bin_path": "DATA/PL1/P22.BIN", "size_bytes": 1266336, "base_palettes": {"A": 2432, "B": 2448, "C": 2464, "D": 2480}},
    {"char_id": 0x23, "slug": "kula", "display_name": "Kula Diamond", "rel_bin_path": "DATA/PL1/P23.BIN", "size_bytes": 729544, "base_palettes": {"A": 2496, "B": 2512, "C": 2528, "D": 2544}},
    {"char_id": 0x24, "slug": "k9999", "display_name": "K9999", "rel_bin_path": "DATA/PL1/P24.BIN", "size_bytes": 1466248, "base_palettes": {"A": 2560, "B": 2576, "C": 2592, "D": 2608}},
    {"char_id": 0x25, "slug": "angel", "display_name": "Angel", "rel_bin_path": "DATA/PL1/P25.BIN", "size_bytes": 1069880, "base_palettes": {"A": 2624, "B": 2640, "C": 2656, "D": 2672}},
    {"char_id": 0x26, "slug": "bao", "display_name": "Bao / Extra", "rel_bin_path": "DATA/PL1/P26.BIN", "size_bytes": 1387360, "base_palettes": {"A": 2688, "B": 2704, "C": 2720, "D": 2736}},
    {"char_id": 0x27, "slug": "rugal", "display_name": "Omega Rugal", "rel_bin_path": "DATA/PL1/P27.BIN", "size_bytes": 1604064, "base_palettes": {"A": 2752, "B": 2768, "C": 2784, "D": 2800}},
    {"char_id": 0x28, "slug": "kusanagi", "display_name": "Kusanagi", "rel_bin_path": "DATA/PL1/P28.BIN", "size_bytes": 998760, "base_palettes": {"A": 2816, "B": 2832, "C": 2848, "D": 2864}},
    {"char_id": 0x2E, "slug": "shingo", "display_name": "Shingo Yabuki", "rel_bin_path": "DATA/PL1/P2E.BIN", "size_bytes": 912672, "base_palettes": {"A": 7616, "B": 7632, "C": 7648, "D": 7664}},
    {"char_id": 0x2F, "slug": "king", "display_name": "King", "rel_bin_path": "DATA/PL1/P2F.BIN", "size_bytes": 611416, "base_palettes": {"A": 7680, "B": 7696, "C": 7712, "D": 7728}},
    {"char_id": 0x30, "slug": "orochi_iori", "display_name": "Orochi Iori", "rel_bin_path": "DATA/PL1/P30.BIN", "size_bytes": 1038952, "base_palettes": {"A": 7776, "B": 7792, "C": 7808, "D": 7824}},
    {"char_id": 0x31, "slug": "geese", "display_name": "Geese Howard", "rel_bin_path": "DATA/PL1/P31.BIN", "size_bytes": 1431784, "base_palettes": {"A": 7856, "B": 7872, "C": 7888, "D": 7904}},
    {"char_id": 0x32, "slug": "goenitz", "display_name": "Goenitz", "rel_bin_path": "DATA/PL1/P32.BIN", "size_bytes": 1154152, "base_palettes": {"A": 7936, "B": 7952, "C": 7968, "D": 7984}},
]

_SLOTS_BY_ID: dict[int, CharacterSlot] = {
    d["char_id"]: CharacterSlot(
        char_id=d["char_id"],
        char_id_hex=f"0x{d['char_id']:02X}",
        slug=d["slug"],
        display_name=d["display_name"],
        rel_bin_path=d["rel_bin_path"],
        size_bytes=d["size_bytes"],
        base_palettes=d["base_palettes"],
    )
    for d in CHARACTER_SLOTS_DATA
}

_SLOTS_BY_SLUG: dict[str, CharacterSlot] = {
    s.slug.lower(): s for s in _SLOTS_BY_ID.values()
}

# Alias dictionary for flexible resolution
_ALIASES: dict[str, int] = {
    "kyo": 0x00, "kyo kusanagi": 0x00,
    "benimaru": 0x01, "benimaru nikaido": 0x01,
    "daimon": 0x02, "goro": 0x02, "goro daimon": 0x02,
    "terry": 0x03, "terry bogard": 0x03, "terrybogard": 0x03,
    "andy": 0x04, "andy bogard": 0x04,
    "joe": 0x05, "joe higashi": 0x05,
    "ryo": 0x06, "ryo sakazaki": 0x06,
    "robert": 0x07, "robert garcia": 0x07,
    "yuri": 0x08, "yuri sakazaki": 0x08,
    "leona": 0x09, "leona heidern": 0x09,
    "ralf": 0x0A, "ralf jones": 0x0A,
    "clark": 0x0B, "clark still": 0x0B,
    "athena": 0x0C, "athena asamiya": 0x0C,
    "kensou": 0x0D, "sie kensou": 0x0D,
    "chin": 0x0E, "chin gentsai": 0x0E,
    "chizuru": 0x0F, "chizuru kagura": 0x0F,
    "mai": 0x10, "mai shiranui": 0x10,
    "kim": 0x11, "kim kaphwan": 0x11,
    "chang": 0x12, "chang koehan": 0x12,
    "choi": 0x13, "choi bounge": 0x13,
    "iori": 0x14, "iori yagami": 0x14,
    "mature": 0x15,
    "vice": 0x16,
    "yamazaki": 0x17, "ryuji yamazaki": 0x17,
    "mary": 0x18, "blue mary": 0x18, "bluemary": 0x18,
    "billy": 0x19, "billy kane": 0x19,
    "yashiro": 0x1A, "yashiro nanakase": 0x1A,
    "shermie": 0x1B,
    "chris": 0x1C,
    "k": 0x1D, "k'": 0x1D, "k_dash": 0x1D, "kdash": 0x1D,
    "maxima": 0x1E,
    "whip": 0x1F,
    "vanessa": 0x20,
    "seth": 0x21,
    "ramon": 0x22,
    "kula": 0x23, "kula diamond": 0x23,
    "k9999": 0x24,
    "angel": 0x25,
    "bao": 0x26,
    "rugal": 0x27, "omega rugal": 0x27, "omegarugal": 0x27, "omega-rugal": 0x27,
    "kusanagi": 0x28,
    "shingo": 0x2E, "shingo yabuki": 0x2E,
    "king": 0x2F,
    "orochi_iori": 0x30, "orochi iori": 0x30, "orochi-iori": 0x30, "wild iori": 0x30,
    "geese": 0x31, "geese howard": 0x31,
    "goenitz": 0x32, "leopold goenitz": 0x32,
}


def list_character_slots() -> list[CharacterSlot]:
    """Return all 46 CharacterSlot instances sorted by slot ID."""
    return [s for _, s in sorted(_SLOTS_BY_ID.items())]


def resolve_character_slot(identifier: int | str) -> CharacterSlot:
    """Resolve an identifier to a CharacterSlot.

    Accepts:
      - Integer ID: 0x03, 3
      - Hex string: "0x03", "0x27", "27"
      - P-prefix code: "P03", "p27"
      - Slug: "terry", "rugal"
      - Full display name: "Terry Bogard", "Omega Rugal"
    """
    if isinstance(identifier, int):
        if identifier in _SLOTS_BY_ID:
            return _SLOTS_BY_ID[identifier]
        raise KeyError(f"Character ID {identifier} (0x{identifier:02X}) is not a valid KOF 2002 slot.")

    raw = str(identifier).strip().lower()

    # Exact alias match
    if raw in _ALIASES:
        return _SLOTS_BY_ID[_ALIASES[raw]]

    # Exact slug match
    if raw in _SLOTS_BY_SLUG:
        return _SLOTS_BY_SLUG[raw]

    # Display name match (case-insensitive)
    for slot in _SLOTS_BY_ID.values():
        if slot.display_name.lower() == raw:
            return slot

    # PXX format (e.g. "p03", "p27")
    p_match = re.fullmatch(r"p([0-9a-f]{2})", raw)
    if p_match:
        cid = int(p_match.group(1), 16)
        if cid in _SLOTS_BY_ID:
            return _SLOTS_BY_ID[cid]

    # Hex format ("0x03")
    if raw.startswith("0x"):
        try:
            cid = int(raw, 16)
            if cid in _SLOTS_BY_ID:
                return _SLOTS_BY_ID[cid]
        except ValueError:
            pass

    # Decimal or 2-char hex string
    if raw.isdigit():
        cid = int(raw, 10)
        if cid in _SLOTS_BY_ID:
            return _SLOTS_BY_ID[cid]

    if re.fullmatch(r"[0-9a-f]{2}", raw):
        try:
            cid = int(raw, 16)
            if cid in _SLOTS_BY_ID:
                return _SLOTS_BY_ID[cid]
        except ValueError:
            pass

    # Substring search in display names
    candidates = [
        s for s in _SLOTS_BY_ID.values()
        if raw in s.display_name.lower() or raw in s.slug.lower()
    ]
    if len(candidates) == 1:
        return candidates[0]

    valid_options = ", ".join(f"{s.display_name} (0x{s.char_id:02X})" for s in list_character_slots()[:6])
    raise KeyError(
        f"Could not resolve character slot '{identifier}'. "
        f"Examples of valid slots: {valid_options}..."
    )

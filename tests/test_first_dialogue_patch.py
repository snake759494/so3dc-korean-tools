#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import patch_first_dialogue as patch  # noqa: E402


ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
FONT = Path(r"C:\Windows\Fonts\malgun.ttf")


@unittest.skipUnless(ISO.is_file() and FONT.is_file(), "original ISO and Malgun Gothic are required")
class FirstDialoguePatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_global = patch.read_exact(
            ISO, patch.GLOBAL_ARCHIVE_OFFSET, patch.GLOBAL_ARCHIVE_SIZE
        )
        cls.original_target = patch.read_exact(
            ISO, patch.TARGET_ARCHIVE_OFFSET, patch.TARGET_ARCHIVE_SIZE
        )
        (
            cls.patched_global,
            cls.global_mclib,
            cls.code_map,
            cls.global_details,
        ) = patch.extend_global_font(cls.original_global, FONT)
        (
            cls.patched_target,
            cls.target_mclib,
            cls.target_details,
        ) = patch.patch_target_archive(cls.original_target, cls.code_map)

    def test_global_extension_stays_in_archive(self) -> None:
        self.assertEqual(len(self.patched_global), patch.GLOBAL_ARCHIVE_SIZE)
        self.assertEqual(self.global_details["new_glyph_count"], 306)
        self.assertEqual(self.global_details["new_decoded"] % 0x80, 0)
        self.assertEqual(
            patch.u32(self.patched_global, patch.GLOBAL_WRAPPER_REL + 4) % 4,
            0,
        )
        self.assertLessEqual(
            self.global_details["new_compressed"],
            self.global_details["payload_allocation"],
        )
        self.assertEqual(
            self.patched_global[patch.GLOBAL_NEW_END_REL : patch.GLOBAL_NEW_END_REL + 4],
            b"DNE\0",
        )

    def test_translation_uses_fourteen_appended_codes(self) -> None:
        self.assertEqual(patch.hangul_characters(), "소피아페이트봐호텔은가없어왜")
        self.assertEqual(set(self.code_map.values()), set(range(293, 307)))
        self.assertEqual(self.target_details["new_local_base"], 307)

    def test_target_local_bitmaps_are_unchanged(self) -> None:
        old_decoded, _ = patch.decode_inner_slz(
            self.original_target, patch.TARGET_MEMBER_REL
        )
        old = patch.Mclib.parse(old_decoded)
        new = patch.Mclib.parse(self.target_mclib)
        self.assertEqual(old.widths, new.widths)
        self.assertEqual(old.bitmaps, new.bitmaps)
        self.assertEqual(new.local_base - old.local_base, 6)
        self.assertEqual(self.target_details["remapped_local_operands"], 108)

    def test_target_growth_uses_only_first_package_padding(self) -> None:
        self.assertEqual(len(self.patched_target), patch.TARGET_ARCHIVE_SIZE)
        self.assertGreaterEqual(self.target_details["remaining_first_package_gap"], 0)
        self.assertEqual(
            self.patched_target[patch.TARGET_SECOND_PACKAGE_REL : patch.TARGET_SECOND_PACKAGE_REL + 4],
            b"\0\0\0\0",
        )
        self.assertEqual(
            self.patched_target[patch.TARGET_SECOND_PACKAGE_REL + 4 : patch.TARGET_SECOND_PACKAGE_REL + 8],
            bytes.fromhex("34000000"),
        )

    def test_message_literalizes_both_names_and_keeps_controls(self) -> None:
        message = bytes.fromhex(str(self.target_details["new_message_hex"]))
        self.assertNotIn(bytes.fromhex("938002"), message)
        self.assertNotIn(bytes.fromhex("938001"), message)
        for control in (
            "8a800000803f",
            "888006",
            "868000000000",
            "8780",
            "8080",
            "888005",
            "888007",
            "84808180",
            "8580cdcc4c3e",
            "8580cdcccc3e",
        ):
            self.assertIn(bytes.fromhex(control), message)
        self.assertLessEqual(
            self.target_details["new_message_bytes"],
            self.target_details["old_message_bytes"],
        )


if __name__ == "__main__":
    unittest.main()

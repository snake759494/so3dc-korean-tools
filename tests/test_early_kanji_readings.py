#!/usr/bin/env python3
from __future__ import annotations

import collections
import os
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import patch_early_kanji_readings as patch  # noqa: E402
import patch_first_dialogue as first  # noqa: E402
import verify_early_kanji_readings as verify  # noqa: E402
from so3_repack import Mclib, encode_glyph_code, render_glyphs  # noqa: E402


ORIGINAL_ISO = Path(
    os.environ.get("SO3_DISC1_ISO", "__missing_original_iso__")
)
FONT = Path(
    os.environ.get("SO3_NANUM_FONT", "__missing_nanum_font__")
)

EXPECTED_ROWS = (
    ("号", "호", 8, 309, 315),
    ("室", "실", 9, 310, 316),
    ("無", "무", 12, 313, 319),
    ("何", "하", 33, 334, 340),
    ("当", "당", 39, 340, 346),
    ("一", "일", 40, 341, 347),
    ("切", "절", 41, 342, 348),
    ("人", "인", 42, 343, 349),
    ("工", "공", 43, 344, 350),
    ("生", "생", 44, 345, 351),
    ("物", "물", 45, 346, 352),
    ("使", "사", 46, 347, 353),
    ("用", "용", 47, 348, 354),
)

EXPECTED_CODE_HITS = {
    315: [(1, 3)],
    316: [(1, 3), (3, 1)],
    319: [(1, 1), (6, 1)],
    340: [(2, 1), (21, 1), (24, 1)],
    346: [(14, 1)],
    347: [(14, 1), (24, 1)],
    348: [(14, 1)],
    349: [(14, 1)],
    350: [(14, 1)],
    351: [(14, 1)],
    352: [(14, 1)],
    353: [(14, 1)],
    354: [(14, 1)],
}


class ReadingTableTests(unittest.TestCase):
    def test_reading_table_is_the_audited_thirteen_slot_mapping(self) -> None:
        patch.validate_reading_rows()
        self.assertEqual(patch.READING_ROWS, EXPECTED_ROWS)
        self.assertEqual(len({row[2] for row in patch.READING_ROWS}), 13)
        self.assertEqual(len({row[3] for row in patch.READING_ROWS}), 13)
        self.assertEqual(len({row[4] for row in patch.READING_ROWS}), 13)
        for kanji, reading, index, original_code, patched_code in patch.READING_ROWS:
            self.assertEqual(len(kanji), 1)
            self.assertEqual(len(reading), 1)
            self.assertEqual(original_code, 301 + index)
            self.assertEqual(patched_code, 307 + index)

    def test_reading_table_validator_rejects_bad_rows(self) -> None:
        bad_rows = (
            (("号室", "호", 8, 309, 315),),
            (("号", "호실", 8, 309, 315),),
            (("号", "호", 72, 373, 379),),
            (("号", "호", 8, 310, 315),),
            (("号", "호", 8, 309, 316),),
            (("号", "호", 8, 309, 315), ("室", "실", 8, 309, 315)),
        )
        for rows in bad_rows:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    patch.validate_reading_rows(rows)

    def test_message_14_preview_selects_each_of_its_nine_slots_once(self) -> None:
        local_codes = [
            code
            for line in patch.PREVIEW_LINES
            for code in line
            if code >= 307
        ]
        self.assertEqual(collections.Counter(local_codes), collections.Counter(range(346, 355)))


class ArtifactPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (ROOT / "__nonexistent_path_test_root__").resolve(strict=False)
        self.input_iso = self.root / "original.iso"
        self.font = self.root / "NanumSquareNeo-cBd.ttf"
        self.output_iso = self.root / "patched.iso"
        self.preview = self.root / "preview.png"
        self.report = self.root / "report.json"

    def assert_patch_collision(
        self,
        *,
        input_iso: Path | None = None,
        font: Path | None = None,
        output_iso: Path | None = None,
        preview: Path | None = None,
        report: Path | None = None,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "path collision"):
            patch.validate_artifact_paths(
                input_iso or self.input_iso,
                font or self.font,
                self.output_iso if output_iso is None else output_iso,
                self.preview if preview is None else preview,
                self.report if report is None else report,
            )

    def test_patcher_rejects_protected_input_aliases(self) -> None:
        self.assert_patch_collision(report=self.input_iso)
        self.assert_patch_collision(preview=self.input_iso)
        self.assert_patch_collision(preview=self.font)

    def test_patcher_rejects_output_aliases(self) -> None:
        self.assert_patch_collision(report=self.output_iso)
        self.assert_patch_collision(report=self.preview)

    def test_verifier_rejects_report_aliases(self) -> None:
        for protected in (self.input_iso, self.output_iso):
            with self.subTest(protected=protected):
                with self.assertRaisesRegex(ValueError, "path collision"):
                    verify.validate_artifact_paths(
                        self.input_iso, self.output_iso, protected
                    )

    def test_distinct_artifact_paths_are_accepted(self) -> None:
        patch.validate_artifact_paths(
            self.input_iso,
            self.font,
            self.output_iso,
            self.preview,
            self.report,
        )
        verify.validate_artifact_paths(
            self.input_iso, self.output_iso, self.report
        )


@unittest.skipUnless(
    ORIGINAL_ISO.is_file() and FONT.is_file(),
    "supported Disc 1 ISO and NanumSquareNeo-cBd.ttf are required",
)
class ArchiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if first.sha256_file(FONT) != patch.FONT_SHA256:
            raise unittest.SkipTest("the local Nanum font does not match the supported SHA-256")

        cls.original_global = first.read_exact(
            ORIGINAL_ISO, first.GLOBAL_ARCHIVE_OFFSET, first.GLOBAL_ARCHIVE_SIZE
        )
        cls.original_target = first.read_exact(
            ORIGINAL_ISO, first.TARGET_ARCHIVE_OFFSET, first.TARGET_ARCHIVE_SIZE
        )
        (
            cls.patched_global,
            cls.global_mclib,
            cls.code_map,
            cls.global_details,
        ) = first.extend_global_font(
            cls.original_global, FONT, gray_levels=patch.FONT_GRAY_LEVELS
        )
        (
            cls.base_target,
            cls.base_target_mclib,
            cls.base_target_details,
        ) = first.patch_target_archive(cls.original_target, cls.code_map)
        (
            cls.patched_target,
            cls.target_mclib,
            cls.target_details,
        ) = patch.repack_target_archive(cls.base_target, FONT)

        cls.base_parsed = Mclib.parse(cls.base_target_mclib)
        cls.target_parsed = Mclib.parse(cls.target_mclib)

    def test_expected_reproducible_artifact_hashes(self) -> None:
        self.assertEqual(
            first.sha256(self.patched_global),
            "0C9BB9000FE13B7A0BD5D08057B76BDED341ED9DB0C276BC90C8A1949327692B",
        )
        self.assertEqual(
            first.sha256(self.global_mclib),
            "843E25AAB01EDECF08FFE267BD9DD85F4816416E67C716B292784E0119F3895D",
        )
        self.assertEqual(
            first.sha256(self.patched_target),
            "58ACAC705237C5FF23C36B6D646BE55C0D71632CDBB60E0443F618D6847659EE",
        )
        self.assertEqual(
            first.sha256(self.target_mclib),
            "37A167F230D9CE019A9846FC2C4468C1488132D365070EFA361945561B7F4718",
        )

    def test_global_font_stays_in_its_archive_and_allocation(self) -> None:
        self.assertEqual(len(self.patched_global), len(self.original_global))
        self.assertEqual(
            self.patched_global[: first.GLOBAL_WRAPPER_REL],
            self.original_global[: first.GLOBAL_WRAPPER_REL],
        )
        self.assertEqual(self.global_details["old_glyph_count"], 292)
        self.assertEqual(self.global_details["new_glyph_count"], 306)
        self.assertEqual(self.global_details["new_compressed"], 25383)
        self.assertEqual(self.global_details["payload_allocation"], 25440)
        self.assertEqual(
            self.global_details["payload_allocation"]
            - self.global_details["new_compressed"],
            57,
        )
        self.assertEqual(sorted(self.code_map.values()), list(range(293, 307)))
        self.assertEqual(set(self.code_map), set(first.hangul_characters()))

        for code in self.code_map.values():
            width, bitmap = first.global_width_and_bitmap(self.global_mclib, code)
            self.assertGreater(width, 0)
            self.assertLessEqual(width, 24)
            self.assertTrue(any(bitmap))
            nibbles = {nibble for byte in bitmap for nibble in (byte & 15, byte >> 4)}
            self.assertLessEqual(nibbles, {0, 7, 15})

    def test_only_the_thirteen_local_bitmaps_change(self) -> None:
        before = self.base_parsed
        after = self.target_parsed
        self.assertEqual(len(self.base_target_mclib), len(self.target_mclib))
        self.assertEqual(before.rows, after.rows)
        self.assertEqual(before.segments, after.segments)
        self.assertEqual(before.widths, after.widths)
        self.assertEqual(
            (after.local_base, after.glyph_count, after.glyph_width, after.glyph_height),
            (307, 72, 24, 24),
        )

        glyph_bytes = after.glyph_stride * after.glyph_height // 2
        changed = {row[2] for row in patch.READING_ROWS}
        independently_rendered = render_glyphs(
            "".join(row[1] for row in patch.READING_ROWS),
            FONT,
            24,
            24,
            24,
            gray_levels=patch.FONT_GRAY_LEVELS,
        )
        rendered_by_index = {
            row[2]: glyph for row, glyph in zip(patch.READING_ROWS, independently_rendered)
        }

        for index in range(after.glyph_count):
            start = index * glyph_bytes
            end = start + glyph_bytes
            if index in changed:
                rendered_width, expected_bitmap = rendered_by_index[index]
                self.assertEqual(rendered_width, 21)
                self.assertEqual(after.widths[index], before.widths[index])
                self.assertEqual(after.widths[index], 24)
                self.assertEqual(after.bitmaps[start:end], expected_bitmap)
                self.assertNotEqual(before.bitmaps[start:end], after.bitmaps[start:end])
            else:
                self.assertEqual(before.widths[index], after.widths[index])
                self.assertEqual(before.bitmaps[start:end], after.bitmaps[start:end])

        allowed: set[int] = set()
        for index in changed:
            start = before.bitmap_start + index * glyph_bytes
            allowed.update(range(start, start + glyph_bytes))
        actual = {
            index
            for index, (old, new) in enumerate(
                zip(self.base_target_mclib, self.target_mclib)
            )
            if old != new
        }
        self.assertTrue(actual)
        self.assertLessEqual(actual, allowed)

    def test_reading_codes_are_live_in_the_scene_messages(self) -> None:
        for _, _, _, _, patched_code in patch.READING_ROWS:
            encoded = encode_glyph_code(patched_code)
            hits = []
            for message_id, offset in self.base_parsed.rows:
                count = self.base_parsed.segments[offset].count(encoded)
                if count:
                    hits.append((message_id, count))
            self.assertEqual(hits, EXPECTED_CODE_HITS[patched_code])

    def test_repacked_pk1_preserves_its_table_and_fixed_record_allocation(self) -> None:
        old_rows = first.parse_pk1_table(self.base_target)
        new_rows = first.parse_pk1_table(self.patched_target)
        self.assertEqual(new_rows, old_rows)
        self.assertEqual(self.target_details["record_growth"], 0)
        self.assertEqual(self.target_details["new_compressed"], 9859)
        self.assertEqual(self.target_details["new_record_size"], 0x28D0)
        self.assertEqual(self.target_details["used_record_size"], 9876)
        self.assertEqual(self.target_details["unused_record_bytes"], 572)
        self.assertEqual(self.target_details["remaining_package_gap"], 360)
        self.assertEqual(len(self.patched_target), len(self.base_target))

        for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
            old_tag, old_id, old_size, old_offset = old_row
            new_tag, new_id, new_size, new_offset = new_row
            self.assertEqual(
                (new_tag, new_id, new_size, new_offset),
                (old_tag, old_id, old_size, old_offset),
            )
            if index != first.TARGET_RECORD_INDEX:
                self.assertEqual(
                    self.patched_target[new_offset : new_offset + new_size],
                    self.base_target[old_offset : old_offset + old_size],
                )

        target_offset = new_rows[first.TARGET_RECORD_INDEX][3]
        used_end = target_offset + self.target_details["used_record_size"]
        allocation_end = target_offset + self.target_details["new_record_size"]
        self.assertFalse(any(self.patched_target[used_end:allocation_end]))

        self.assertEqual(
            self.patched_target[first.TARGET_SECOND_PACKAGE_REL :],
            self.base_target[first.TARGET_SECOND_PACKAGE_REL :],
        )
        first_end = max(offset + size for _, _, size, offset in new_rows)
        self.assertEqual(first_end, first.TARGET_SECOND_PACKAGE_REL - 360)
        self.assertFalse(any(self.patched_target[first_end : first.TARGET_SECOND_PACKAGE_REL]))

    def test_source_signatures_reject_mutated_inputs(self) -> None:
        bad_mclib = bytearray(self.base_target_mclib)
        bad_mclib[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "mclib SHA-256 mismatch"):
            patch.replace_reading_glyphs(bytes(bad_mclib), FONT)

        bad_archive = bytearray(self.base_target)
        bad_archive[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "archive SHA-256 mismatch"):
            patch.repack_target_archive(bytes(bad_archive), FONT)


if __name__ == "__main__":
    unittest.main(verbosity=2)

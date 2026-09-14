from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import patch_hyda_dialogue as patch  # noqa: E402
from so3_repack import Mclib, align, encode_glyph_code  # noqa: E402


def canonical_segment() -> tuple[bytes, dict[str, object]]:
    header = bytes.fromhex("888006868000000000")
    speaker = bytes.fromhex("938001")
    boundary = bytes.fromhex("898087808080")
    body = (
        bytes.fromhex("9e8001000200")
        + encode_glyph_code(272)
        + encode_glyph_code(50)
        + patch.NEWLINE
        + bytes.fromhex("8580cdcccc3d")
        + encode_glyph_code(51)
        + bytes.fromhex("9f808480818000")
    )
    raw = header + speaker + boundary + body + b"\0" * 3
    field_start = len(header)
    field_end = field_start + len(speaker)
    body_start = field_end + len(boundary)
    digest = hashlib.sha256(raw).hexdigest()
    row: dict[str, object] = {
        "archive_id": 1204,
        "stream_id": 5829,
        "message_id": 1,
        "exact_sha256": digest,
        "raw_bytes_hex": raw.hex(),
        "japanese": "「あ\nい",
        "source_line_count": 2,
        "speaker": {"mode": "character_reference", "japanese": "フェイト"},
        "evidence": {
            "speaker_field_start_offset": field_start,
            "speaker_field_end_offset": field_end,
            "body_start_offset": body_start,
        },
    }
    return raw, row


def synthetic_mclib(target: bytes, other: bytes, glyph_count: int = 3) -> bytes:
    table_start = 0x80
    text_start = align(table_start + 16)
    text = target + other
    width_start = align(text_start + len(text))
    bitmap_start = align(width_start + glyph_count)
    glyph_bytes = 24 * 24 // 2
    file_size = align(bitmap_start + glyph_count * glyph_bytes)
    data = bytearray(file_size)
    data[:16] = b"so3mclib 1.75".ljust(16, b"\0")
    words = [
        table_start,
        text_start,
        width_start,
        bitmap_start,
        glyph_count,
        0,
        0,
        24,
        24,
        24,
        301,
        2,
        file_size,
    ]
    struct.pack_into("<13I", data, 0x10, *words)
    struct.pack_into("<II", data, table_start, 1, 0)
    struct.pack_into("<II", data, table_start + 8, 2, len(target))
    data[text_start : text_start + len(text)] = text
    data[width_start : width_start + glyph_count] = bytes([20] * glyph_count)
    for index in range(glyph_count):
        start = bitmap_start + index * glyph_bytes
        data[start : start + glyph_bytes] = bytes([index + 1]) * glyph_bytes
    return bytes(data)


class HydaDialoguePatchTests(unittest.TestCase):
    def test_replace_line_preserves_controls_and_drops_visible_japanese_controls(self) -> None:
        tokens = [
            patch.Token("control", bytes.fromhex("9e8001000200")),
            patch.Token("glyph", encode_glyph_code(272), 272),
            patch.Token("control", bytes.fromhex("868000000000")),
            patch.Token("control", bytes.fromhex("938001")),
            patch.Token("control", bytes.fromhex("9080")),
            patch.Token("control", bytes.fromhex("91800100")),
            patch.Token("glyph", encode_glyph_code(50), 50),
            patch.Token("control", bytes.fromhex("9f80")),
        ]
        mapping = {"가": 400, "나": 401, "다": 402}
        rebuilt = patch.replace_line_tokens(
            tokens, "가나다", lambda char: encode_glyph_code(mapping[char])
        )
        self.assertIn(bytes.fromhex("9e8001000200"), rebuilt)
        self.assertIn(bytes.fromhex("868000000000"), rebuilt)
        self.assertTrue(rebuilt.endswith(bytes.fromhex("9f80")))
        self.assertNotIn(bytes.fromhex("938001"), rebuilt)
        self.assertNotIn(bytes.fromhex("9080"), rebuilt)
        self.assertNotIn(bytes.fromhex("9180"), rebuilt)
        for code in mapping.values():
            self.assertIn(encode_glyph_code(code), rebuilt)

    def test_segment_literalizes_speaker_and_preserves_line_controls(self) -> None:
        original, row = canonical_segment()
        translation = patch.Translation(
            str(row["exact_sha256"]), "「あ\nい", "「한\n글", "페이트"
        )
        chars = "「한글페이트"
        mapping = {char: 400 + index for index, char in enumerate(chars)}
        rebuilt = patch.replace_dialogue_segment(
            original,
            row,
            translation,
            lambda char: encode_glyph_code(mapping[char]),
        )
        header = bytes.fromhex("888006868000000000")
        speaker = b"".join(encode_glyph_code(mapping[ch]) for ch in "페이트")
        self.assertTrue(rebuilt.startswith(header + speaker + bytes.fromhex("898087808080")))
        self.assertNotIn(bytes.fromhex("938001"), rebuilt)
        self.assertEqual(rebuilt.count(patch.NEWLINE), 2)  # delimiter + body newline
        self.assertIn(bytes.fromhex("9e8001000200"), rebuilt)
        self.assertIn(bytes.fromhex("8580cdcccc3d"), rebuilt)
        self.assertTrue(rebuilt.endswith(b"\0"))

    def test_segment_rejects_changed_line_count(self) -> None:
        original, row = canonical_segment()
        translation = patch.Translation(
            str(row["exact_sha256"]), "「あ\nい", "한 줄", "페이트"
        )
        with self.assertRaisesRegex(ValueError, "preserve line count"):
            patch.replace_dialogue_segment(
                original, row, translation, lambda _: encode_glyph_code(400)
            )

    def test_mclib_reuses_only_slots_not_referenced_by_other_messages(self) -> None:
        target, row = canonical_segment()
        # The non-target message protects local code 301 / bitmap index 0.
        other = encode_glyph_code(301) + b"\0"
        original = synthetic_mclib(target, other)
        translation = patch.Translation(
            str(row["exact_sha256"]), "「あ\nい", "「한\n글", "페이트"
        )

        def fake_renderer(text, _font, _width, _height, _stride, **_kwargs):
            return [(19, bytes([0xA0 + index]) * 288) for index, _ in enumerate(text)]

        rebuilt, details = patch.rebuild_mclib(
            original,
            {1: (row, translation)},
            Path("unused.ttf"),
            glyph_renderer=fake_renderer,
        )
        before = Mclib.parse(original)
        after = Mclib.parse(rebuilt)
        before_rows = dict(before.rows)
        after_rows = dict(after.rows)
        self.assertEqual(
            patch.logical_segment(before.segments[before_rows[2]]),
            patch.logical_segment(after.segments[after_rows[2]]),
        )
        self.assertNotEqual(before.segments[before_rows[1]], after.segments[after_rows[1]])
        self.assertEqual(before.widths[0], after.widths[0])
        self.assertEqual(before.bitmaps[:288], after.bitmaps[:288])
        self.assertEqual(details["protected_local_glyphs"], 1)
        self.assertEqual(details["reused_local_glyphs"], 2)
        self.assertGreater(details["appended_local_glyphs"], 0)

    def test_pk1_record_reflow_preserves_other_records(self) -> None:
        count = 3
        header_size = 0x10 + count * 16
        records = [b"A" * 20, b"B" * 32, b"C" * 24]
        offsets = [header_size, header_size + 20, header_size + 52]
        archive = bytearray(256)
        struct.pack_into("<IIII", archive, 0, 0, count, header_size, 0)
        for index, (tag, record, offset) in enumerate(
            zip((b"ONE1", b"DCMS", b"THR3"), records, offsets)
        ):
            struct.pack_into("<4sIII", archive, 0x10 + index * 16, tag, index, len(record), offset)
            archive[offset : offset + len(record)] = record
        rebuilt, details = patch.replace_first_pk1_record(bytes(archive), 1, b"N" * 50)
        rows = patch.parse_pk1_table(rebuilt)
        self.assertEqual(rows[0], (b"ONE1", 0, 20, offsets[0]))
        self.assertEqual(rows[1], (b"DCMS", 1, 52, offsets[1]))
        self.assertEqual(rows[2], (b"THR3", 2, 24, offsets[2] + 20))
        self.assertEqual(rebuilt[rows[0][3] : rows[0][3] + 20], records[0])
        self.assertEqual(rebuilt[rows[2][3] : rows[2][3] + 24], records[2])
        self.assertEqual(details["record_growth"], 20)

    def test_translation_plan_requires_exact_coverage_and_speaker(self) -> None:
        raw, row = canonical_segment()
        catalogue = {"dialogues": [row]}
        translation = {
            "translations": [
                {
                    "exact_sha256": row["exact_sha256"],
                    "japanese": row["japanese"],
                    "korean": "「한\n글",
                    "speaker_korean": "페이트",
                    "occurrences": [
                        {"archive_id": 1204, "stream_id": 5829, "message_id": 1}
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            catalogue_path = base / "catalogue.json"
            translation_path = base / "translation.json"
            catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
            translation_path.write_text(json.dumps(translation), encoding="utf-8")
            source, plan = patch.load_patch_plan(
                catalogue_path,
                translation_path,
                expected_entries=1,
                expected_occurrences=None,
            )
            occurrence = patch.Occurrence(1204, 5829, 1)
            self.assertIn(occurrence, source)
            self.assertEqual(plan[occurrence].speaker_korean, "페이트")

            del translation["translations"][0]["speaker_korean"]
            translation_path.write_text(json.dumps(translation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "speaker_korean is required"):
                patch.load_patch_plan(
                    catalogue_path,
                    translation_path,
                    expected_entries=1,
                    expected_occurrences=None,
                )

    def test_compact_manifest_and_translation_need_no_japanese_or_raw_bytes(self) -> None:
        _raw, row = canonical_segment()
        compact_row = {
            key: row[key]
            for key in (
                "archive_id", "stream_id", "message_id", "exact_sha256",
                "speaker", "evidence", "source_line_count",
            )
        }
        compact = {"schema_version": 1, "occurrences": [compact_row]}
        translation = {
            "translations": [
                {
                    "exact_sha256": row["exact_sha256"],
                    "korean": "「한\n글",
                    "speaker_korean": "페이트",
                    "occurrences": [
                        {"archive_id": 1204, "stream_id": 5829, "message_id": 1}
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manifest_path = base / "manifest.json"
            translation_path = base / "translation.json"
            manifest_path.write_text(json.dumps(compact), encoding="utf-8")
            translation_path.write_text(json.dumps(translation), encoding="utf-8")
            source, plan = patch.load_patch_plan(
                manifest_path,
                translation_path,
                expected_entries=1,
                expected_occurrences=None,
            )
        occurrence = patch.Occurrence(1204, 5829, 1)
        self.assertNotIn("raw_bytes_hex", source[occurrence])
        self.assertIsNone(plan[occurrence].japanese)
        self.assertEqual(plan[occurrence].korean, "「한\n글")


if __name__ == "__main__":
    unittest.main()

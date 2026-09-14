from __future__ import annotations

import random
import struct
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from so3_repack import (  # noqa: E402
    Mclib,
    compress_slz_mode2,
    decompress_slz_payload,
    encode_glyph_code,
)

ALIGNMENT = 0x80
GLYPH_WIDTH = 24
GLYPH_HEIGHT = 24
GLYPH_STRIDE = 24
GLYPH_BYTES = GLYPH_STRIDE * GLYPH_HEIGHT // 2


def align(value: int) -> int:
    return (value + ALIGNMENT - 1) & -ALIGNMENT


def bitmap(seed: int) -> bytes:
    """Return a deterministic packed 4bpp glyph; low pixel is low nibble."""
    pixels = [
        (x * 3 + y * 5 + seed) & 0x0F
        for y in range(GLYPH_HEIGHT)
        for x in range(GLYPH_STRIDE)
    ]
    return bytes(
        pixels[i] | (pixels[i + 1] << 4)
        for i in range(0, len(pixels), 2)
    )


def glyph(seed: int, width: int) -> tuple[int, bytes]:
    return width, bitmap(seed)


def make_mclib_fixture() -> bytes:
    table_start = 0x80
    rows = [(10, 0), (20, 8)]
    text_start = align(table_start + len(rows) * 8)
    width_start = text_start + 0x80
    glyph_count = 4
    bitmap_start = align(width_start + glyph_count)
    file_size = align(bitmap_start + glyph_count * GLYPH_BYTES)

    data = bytearray(file_size)
    data[:9] = b"so3mclib "
    struct.pack_into(
        "<13I",
        data,
        0x10,
        table_start,
        text_start,
        width_start,
        bitmap_start,
        glyph_count,
        0,
        0,
        GLYPH_WIDTH,
        GLYPH_HEIGHT,
        GLYPH_STRIDE,
        124,
        len(rows),
        file_size,
    )
    for index, row in enumerate(rows):
        struct.pack_into("<II", data, table_start + index * 8, *row)

    data[text_start : text_start + 11] = (
        b"\x01\x02\0\0\0\0\0\0\x03\x04\0"
    )

    original_glyphs = [glyph(seed, 8 + seed) for seed in range(1, 5)]
    data[width_start : width_start + glyph_count] = bytes(
        width for width, _ in original_glyphs
    )
    cursor = bitmap_start
    for _, pixels in original_glyphs:
        data[cursor : cursor + GLYPH_BYTES] = pixels
        cursor += GLYPH_BYTES
    return bytes(data)


class SlzTests(unittest.TestCase):
    def test_round_trips_deterministic_samples(self) -> None:
        for length in (0, 1, 2, 3, 7, 8, 9, 18, 19, 274, 275, 1000, 8192):
            rng = random.Random(length)
            samples = (
                bytes(rng.randrange(256) for _ in range(length)),
                b"\0" * length,
                (b"StarOcean3" * ((length + 9) // 10))[:length],
                bytes(rng.randrange(16) for _ in range(length)),
            )
            for sample_index, source in enumerate(samples):
                with self.subTest(length=length, sample=sample_index):
                    packed = compress_slz_mode2(source)
                    self.assertEqual(
                        decompress_slz_payload(packed, 2, len(source)), source
                    )

    def test_glyph_code_boundaries(self) -> None:
        self.assertEqual(encode_glyph_code(1), b"\x01")
        self.assertEqual(encode_glyph_code(127), b"\x7f")
        self.assertEqual(encode_glyph_code(128), b"\x80\x01")
        self.assertEqual(encode_glyph_code(1894), b"\xe6\x0e")
        for invalid in (0, 0x4000):
            with self.subTest(code=invalid):
                with self.assertRaisesRegex(
                    ValueError, "outside tested two-byte range"
                ):
                    encode_glyph_code(invalid)


class SyntheticMclibTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = make_mclib_fixture()
        self.mclib = Mclib.parse(self.raw)

    def test_fixture_parses_without_rom_or_font(self) -> None:
        self.assertEqual(self.mclib.rows, [(10, 0), (20, 8)])
        self.assertEqual(self.mclib.glyph_count, 4)
        self.assertEqual(self.mclib.local_base, 124)
        self.assertEqual(self.mclib.widths, bytes((9, 10, 11, 12)))
        self.assertEqual(len(self.mclib.bitmaps), 4 * GLYPH_BYTES)

    def test_append_rebuilds_sections_and_crosses_two_byte_code_boundary(self) -> None:
        additions = [glyph(9, 13), glyph(10, 15)]
        rebuilt, report = self.mclib.replace_message_and_append_glyphs(
            10,
            additions,
            layout=[0, None, 1],
            space_code=5,
        )
        parsed = Mclib.parse(rebuilt)
        expected_message = b"\x80\x01\x05\x81\x01\0"

        self.assertEqual(parsed.rows, [(10, 0), (20, len(expected_message))])
        self.assertEqual(parsed.segments[0], expected_message)
        moved_other = parsed.segments[len(expected_message)]
        old_other = self.mclib.segments[8]
        self.assertEqual(moved_other[: len(old_other)], old_other)
        self.assertFalse(any(moved_other[len(old_other) :]))
        self.assertEqual(parsed.glyph_count, 6)
        self.assertEqual(parsed.widths, self.mclib.widths + bytes((13, 15)))
        self.assertEqual(
            parsed.bitmaps,
            self.mclib.bitmaps + additions[0][1] + additions[1][1],
        )
        self.assertEqual(parsed.table_start % ALIGNMENT, 0)
        self.assertEqual(parsed.text_start % ALIGNMENT, 0)
        self.assertEqual(parsed.width_start % ALIGNMENT, 0)
        self.assertEqual(parsed.bitmap_start % ALIGNMENT, 0)
        self.assertEqual(len(rebuilt) % ALIGNMENT, 0)
        self.assertEqual(report["strategy"], "append_local_glyphs")
        self.assertEqual(report["new_codes"], [128, 129])
        self.assertEqual(report["new_message_codes"], [128, 5, 129])
        self.assertEqual(report["old_glyph_count"], 4)
        self.assertEqual(report["new_glyph_count"], 6)

        packed = compress_slz_mode2(rebuilt)
        self.assertEqual(
            decompress_slz_payload(packed, 2, len(rebuilt)), rebuilt
        )

    def test_reuse_selected_slots_is_size_neutral_and_localized(self) -> None:
        replacements = [glyph(11, 17), glyph(12, 19)]
        rebuilt, report = self.mclib.replace_message_reusing_glyphs(
            10,
            replacements,
            layout=[0, None, 1],
            space_code=5,
            reuse_codes=[125, 127],
        )
        parsed = Mclib.parse(rebuilt)

        self.assertEqual(len(rebuilt), len(self.raw))
        self.assertEqual(parsed.rows, self.mclib.rows)
        self.assertEqual(
            parsed.segments[0], b"\x7d\x05\x7f\0" + b"\0" * 4
        )
        self.assertEqual(parsed.segments[8], self.mclib.segments[8])

        expected_widths = bytearray(self.mclib.widths)
        expected_widths[1], expected_widths[3] = 17, 19
        self.assertEqual(parsed.widths, bytes(expected_widths))

        expected_bitmaps = bytearray(self.mclib.bitmaps)
        expected_bitmaps[
            1 * GLYPH_BYTES : 2 * GLYPH_BYTES
        ] = replacements[0][1]
        expected_bitmaps[
            3 * GLYPH_BYTES : 4 * GLYPH_BYTES
        ] = replacements[1][1]
        self.assertEqual(parsed.bitmaps, bytes(expected_bitmaps))

        allowed = set(
            range(self.mclib.text_start, self.mclib.text_start + 8)
        )
        allowed.update(
            (self.mclib.width_start + 1, self.mclib.width_start + 3)
        )
        allowed.update(
            range(
                self.mclib.bitmap_start + GLYPH_BYTES,
                self.mclib.bitmap_start + 2 * GLYPH_BYTES,
            )
        )
        allowed.update(
            range(
                self.mclib.bitmap_start + 3 * GLYPH_BYTES,
                self.mclib.bitmap_start + 4 * GLYPH_BYTES,
            )
        )
        changed = {
            index
            for index, pair in enumerate(zip(self.raw, rebuilt))
            if pair[0] != pair[1]
        }
        self.assertTrue(changed)
        self.assertLessEqual(changed, allowed)
        self.assertEqual(report["strategy"], "reuse_selected_local_glyphs")
        self.assertEqual(report["new_codes"], [125, 127])
        self.assertEqual(report["reused_local_indices"], [1, 3])
        self.assertEqual(
            report["old_mclib_size"], report["new_mclib_size"]
        )

    def test_reuse_defaults_to_last_local_slots(self) -> None:
        replacements = [glyph(13, 14), glyph(14, 16)]
        rebuilt, report = self.mclib.replace_message_reusing_glyphs(
            10, replacements
        )
        parsed = Mclib.parse(rebuilt)

        self.assertEqual(
            parsed.segments[0], b"\x7e\x7f\0" + b"\0" * 5
        )
        self.assertEqual(report["strategy"], "reuse_last_local_glyphs")
        self.assertEqual(report["new_codes"], [126, 127])
        self.assertEqual(report["reused_local_indices"], [2, 3])
        self.assertEqual(parsed.widths[:2], self.mclib.widths[:2])
        self.assertEqual(
            parsed.bitmaps[: 2 * GLYPH_BYTES],
            self.mclib.bitmaps[: 2 * GLYPH_BYTES],
        )

    def test_reuse_rejects_message_that_does_not_fit_segment(self) -> None:
        replacements = [glyph(5, 8), glyph(6, 9), glyph(7, 10)]
        with self.assertRaisesRegex(
            ValueError,
            r"replacement bytecode \(10\) does not fit target segment \(8\)",
        ):
            self.mclib.replace_message_reusing_glyphs(
                10,
                replacements,
                layout=[0, 1, 2] * 3,
            )

    def test_reuse_validates_explicit_slot_codes(self) -> None:
        replacements = [glyph(5, 8), glyph(6, 9)]
        cases = (
            ([124, 124], "one unique code"),
            ([123, 125], "outside this local atlas"),
        )
        for codes, message in cases:
            with self.subTest(codes=codes):
                with self.assertRaisesRegex(ValueError, message):
                    self.mclib.replace_message_reusing_glyphs(
                        10,
                        replacements,
                        reuse_codes=codes,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)

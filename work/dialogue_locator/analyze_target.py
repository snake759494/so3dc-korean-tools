#!/usr/bin/env python3
"""Validate and render the issue #1 opening dialogue from its source mclib."""

from __future__ import annotations

import csv
import hashlib
import json
import struct
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
MCLIB = ROOT / "work/full_unpack/disc1/decoded/1220/s006194_d0_o00001188.mclib"
GLOBAL = ROOT / "work/kanji_deep/so3mclib_172.bin"
OUT = ROOT / "work/dialogue_locator"
MESSAGE_ID = 5

EXPECTED = bytes.fromhex(
    "8a800000803f888006868000000000938002878080808a800000803f888007"
    "90026446eb01888005938001888007ed0184808180"
    "a701a70165bb01b001c6014b5f57439c02ed018080"
    "8580cdcc4c3eb202b302b802b502b6027ab9024e7264ed018080"
    "8580cdcccc3e56764b5fce02870161655261f1018480818000"
)


def code(raw: str) -> int:
    b = bytes.fromhex(raw)
    if len(b) == 1:
        return b[0]
    return (b[0] & 0x7F) | (b[1] << 7)


def decode_glyph(raw: bytes, width: int, height: int) -> Image.Image:
    pixels = []
    for value in raw:
        pixels.extend(((value & 15) * 17, (value >> 4) * 17))
    image = Image.new("L", (width, height))
    image.putdata(pixels[: width * height])
    return image


def parse_message(data: bytes, message_id: int) -> tuple[int, int, bytes]:
    table_start, text_start, text_end = struct.unpack_from("<3I", data, 0x10)
    count = struct.unpack_from("<I", data, 0x3C)[0]
    rows = [struct.unpack_from("<II", data, table_start + i * 8) for i in range(count)]
    offsets = sorted(offset for _, offset in rows)
    offset = dict(rows)[message_id]
    index = offsets.index(offset)
    end = offsets[index + 1] if index + 1 < len(offsets) else text_end - text_start
    return offset, text_start + offset, data[text_start + offset : text_start + end]


def main() -> None:
    data = MCLIB.read_bytes()
    global_data = GLOBAL.read_bytes()
    rel, absolute, segment = parse_message(data, MESSAGE_ID)
    assert (rel, absolute, segment) == (0x196, 0x296, EXPECTED)

    words = struct.unpack_from("<13I", data, 0x10)
    width_start, bitmap_start, glyph_count = words[2], words[3], words[4]
    glyph_w, glyph_h, stride = words[7:10]
    local_base = words[10]
    glyph_bytes = stride * glyph_h // 2
    widths = data[width_start : width_start + glyph_count]

    global_width_start, global_bitmap_start = struct.unpack_from("<2I", global_data, 0x18)
    global_count = struct.unpack_from("<I", global_data, 0x20)[0]
    global_widths = global_data[global_width_start : global_width_start + global_count]

    # Literal equivalents of the two 93 80 name-substitution controls are
    # supplied only for this evidence render.  They are not read from the
    # target mclib because the controls resolve them at runtime.
    sofia = [code(x) for x in ("ac01", "b901", "9501", "9e01")]
    fayt = [code(x) for x in ("b901", "9701", "9f01", "b101")]
    lines = [
        sofia,
        [code(x) for x in ("9002", "64", "46", "eb01")] + fayt + [code("ed01")],
        [code(x) for x in ("a701", "a701", "65", "bb01", "b001", "c601", "4b", "5f", "57", "43", "9c02", "ed01")],
        [code(x) for x in ("b202", "b302", "b802", "b502", "b602", "7a", "b902", "4e", "72", "64", "ed01")],
        [code(x) for x in ("56", "76", "4b", "5f", "ce02", "8701", "61", "65", "52", "61", "f101")],
    ]

    def ref(character_code: int):
        if character_code >= local_base:
            return "local", character_code - local_base
        return "global", character_code - 1

    def advance(character_code: int) -> int:
        source, index = ref(character_code)
        return (widths if source == "local" else global_widths)[index]

    line_widths = [sum(advance(c) for c in line) for line in lines]
    image = Image.new("L", (max(line_widths), len(lines) * glyph_h), 0)
    for line_number, line in enumerate(lines):
        x = 0
        for character_code in line:
            source, index = ref(character_code)
            if source == "local":
                begin = bitmap_start + index * glyph_bytes
                glyph = decode_glyph(data[begin : begin + glyph_bytes], glyph_w, glyph_h)
            else:
                begin = global_bitmap_start + index * glyph_bytes
                glyph = decode_glyph(global_data[begin : begin + glyph_bytes], glyph_w, glyph_h)
            image.paste(glyph, (x, line_number * glyph_h), glyph)
            x += advance(character_code)
    image.resize((image.width * 4, image.height * 4), Image.Resampling.NEAREST).save(
        OUT / "message_000005_evidence.png"
    )

    tokens = [
        (0x00, "8A80 0000803F", "scale", "1.0"),
        (0x06, "8880 06", "style", "speaker-name style 6"),
        (0x09, "8680 00000000", "timing/state", "0.0; preserve"),
        (0x0F, "9380 02", "name substitution", "speaker ID 2 = ソフィア"),
        (0x12, "8780", "state terminator", "preserve"),
        (0x14, "8080", "newline", "after speaker name"),
        (0x16, "8A80 0000803F", "scale", "1.0"),
        (0x1C, "8880 07", "style", "normal dialogue style 7"),
        (0x1F, "9002 6446 EB01", "glyphs", "「ねぇ、"),
        (0x25, "8880 05", "style", "highlighted-name style 5"),
        (0x28, "9380 01", "name substitution", "character ID 1 = フェイト"),
        (0x2B, "8880 07", "style", "restore dialogue style 7"),
        (0x2E, "ED01", "glyph", "。"),
        (0x30, "8480", "wait/end-page state", "preserve"),
        (0x32, "8180", "page transition", "preserve"),
        (0x34, "A701 A701 65 BB01 B001 C601 4B 5F 57 43 9C02 ED01", "glyphs", "ココのホテルってさぁ…。"),
        (0x46, "8080", "newline", "preserve"),
        (0x48, "8580 CDCC4C3E", "timing", "float32 0.2; preserve"),
        (0x4E, "B202 B302 B802 B502 B602 7A B902 4E 72 64 ED01", "glyphs", "104号室が無いよね。"),
        (0x61, "8080", "newline", "preserve"),
        (0x63, "8580 CDCCCC3E", "timing", "float32 0.4; preserve"),
        (0x69, "56 76 4B 5F CE02 8701 61 65 52 61 F101", "glyphs", "これって何でなのかな?"),
        (0x78, "8480", "wait/end-page state", "preserve"),
        (0x7A, "8180", "page transition", "preserve"),
        (0x7C - 1, "00", "terminator", "message end"),
    ]
    # Correct the final offsets from the actual token lengths rather than
    # relying on the explanatory table above during future edits.
    with (OUT / "target_tokens.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("relative_offset", "bytes", "kind", "interpretation"))
        for offset, raw, kind, meaning in tokens:
            writer.writerow((f"0x{offset:02X}", raw, kind, meaning))

    result = {
        "archive_id": 1220,
        "archive_lba": 616147,
        "archive_iso_offset": 0x4B369800,
        "stream_id": 6194,
        "stream_source_offset": 0x1188,
        "stream_iso_offset": 0x4B36A988,
        "slz_mode": 2,
        "compressed_size": 10411,
        "unpacked_size": 22528,
        "message_id": MESSAGE_ID,
        "message_relative_offset": rel,
        "message_file_offset": absolute,
        "message_bytes": len(segment),
        "message_hex": segment.hex(),
        "mclib_sha256": hashlib.sha256(data).hexdigest(),
        "geometry": [glyph_w, glyph_h],
        "glyph_bpp": 4,
        "local_glyph_code_base": local_base,
        "local_glyph_count": glyph_count,
        "width_table_offset": width_start,
        "bitmap_offset": bitmap_start,
    }
    (OUT / "target.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

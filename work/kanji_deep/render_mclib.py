#!/usr/bin/env python3
"""Parse and render tri-Ace SO3 `so3mclib` glyph libraries.

The bitmap section is a sequence of fixed-size 4-bpp glyphs.  Header words at
0x1c, 0x20, and 0x2c..0x34 give the bitmap offset, count, and cell geometry.
"""

from __future__ import annotations

import argparse
import csv
import math
import struct
from pathlib import Path

from PIL import Image, ImageDraw


def u32s(data: bytes, offset: int, count: int) -> tuple[int, ...]:
    return struct.unpack_from(f"<{count}I", data, offset)


def decode_glyph(raw: bytes, width: int, height: int, high_first: bool = False) -> Image.Image:
    values: list[int] = []
    for value in raw:
        lo, hi = value & 15, value >> 4
        values.extend((hi, lo) if high_first else (lo, hi))
    image = Image.new("L", (width, height))
    image.putdata([value * 17 for value in values[: width * height]])
    return image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--columns", type=int, default=32)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--high-first", action="store_true")
    args = ap.parse_args()

    data = args.input.read_bytes()
    if not data.startswith(b"so3mclib "):
        raise ValueError("not an so3mclib member")
    words = u32s(data, 0x10, 12)
    table_start, table_end, aux_start, bitmap_start = words[:4]
    glyph_count = words[4]
    cache_w, cache_h = words[5:7]
    glyph_w, glyph_h, glyph_stride = words[7:10]
    local_code_base, mapping_count = words[10:12]
    glyph_bytes = glyph_stride * glyph_h // 2
    expected_end = bitmap_start + glyph_count * glyph_bytes
    trailing = data[expected_end:] if expected_end <= len(data) else b""
    if expected_end > len(data) or len(trailing) >= 0x80 or any(trailing):
        raise ValueError(
            f"bitmap geometry/padding invalid: end 0x{expected_end:X}, file 0x{len(data):X}"
        )

    args.output.mkdir(parents=True, exist_ok=True)
    rows = math.ceil(glyph_count / args.columns)
    atlas = Image.new("L", (args.columns * glyph_w, rows * glyph_h), 0)
    contact = Image.new("RGB", (args.columns * (glyph_w + 12), rows * (glyph_h + 14)), "#202020")
    draw = ImageDraw.Draw(contact)
    for index in range(glyph_count):
        start = bitmap_start + index * glyph_bytes
        glyph = decode_glyph(data[start : start + glyph_bytes], glyph_w, glyph_h, args.high_first)
        x, y = (index % args.columns) * glyph_w, (index // args.columns) * glyph_h
        atlas.paste(glyph, (x, y))
        cx, cy = (index % args.columns) * (glyph_w + 12), (index // args.columns) * (glyph_h + 14)
        contact.paste(glyph.convert("RGB"), (cx, cy))
        draw.text((cx, cy + glyph_h), f"{index:03X}", fill="white")

    if args.scale != 1:
        atlas = atlas.resize((atlas.width * args.scale, atlas.height * args.scale), Image.Resampling.NEAREST)
        contact = contact.resize((contact.width * args.scale, contact.height * args.scale), Image.Resampling.NEAREST)
    atlas.save(args.output / "glyph_atlas.png")
    contact.save(args.output / "glyph_contact_indexed.png")

    metadata = {
        "version": data[:16].split(b"\0", 1)[0].decode("ascii"),
        "table_start": table_start,
        "table_end": table_end,
        "aux_start": aux_start,
        "bitmap_start": bitmap_start,
        "glyph_count": glyph_count,
        "cache_width": cache_w,
        "cache_height": cache_h,
        "glyph_width": glyph_w,
        "glyph_height": glyph_h,
        "glyph_stride": glyph_stride,
        "local_code_base": local_code_base,
        "mapping_count": mapping_count,
        "glyph_bytes": glyph_bytes,
        "trailing_padding": len(trailing),
    }
    with (args.output / "metadata.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=metadata)
        writer.writeheader()
        writer.writerow(metadata)
    print(metadata)


if __name__ == "__main__":
    main()

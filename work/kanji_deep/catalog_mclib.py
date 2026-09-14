#!/usr/bin/env python3
"""Catalog and validate every `so3mclib` glyph member in an SO3 SLZ catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import struct
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kanji_archive"))
from so3_archive_scan import decompress_member  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("iso", type=Path)
    ap.add_argument("catalog", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--union-cell", type=int, default=32)
    args = ap.parse_args()

    with args.catalog.open(encoding="utf-8", newline="") as file:
        source = [r for r in csv.DictReader(file) if r["magic_text"].startswith("so3mclib")]

    rows: list[dict[str, str | int]] = []
    unique: dict[tuple[int, int, bytes], dict[str, int | str | bytes]] = {}
    for number, row in enumerate(source):
        data = decompress_member(args.iso, int(row["offset"]))
        if len(data) < 0x40:
            continue
        words = struct.unpack_from("<12I", data, 0x10)
        table_start, table_end, aux_start, bitmap_start = words[:4]
        glyph_count, cache_w, cache_h = words[4:7]
        glyph_w, glyph_h, glyph_stride, local_code_base, mapping_count = words[7:12]
        glyph_bytes = glyph_stride * glyph_h // 2
        expected_end = bitmap_start + glyph_count * glyph_bytes
        trailing = data[expected_end:] if expected_end <= len(data) else b""
        # Members are SLZ-output-aligned to 0x80.  Their glyph array therefore
        # ends at EOF or is followed by at most 0x7f zero padding bytes.
        valid = (
            glyph_count < 0x10000
            and 0 < glyph_w <= glyph_stride <= 128
            and 0 < glyph_h <= 128
            and expected_end <= len(data)
            and len(trailing) < 0x80
            and not any(trailing)
        )
        if valid:
            for index in range(glyph_count):
                begin = bitmap_start + index * glyph_bytes
                raw = data[begin : begin + glyph_bytes]
                key = (glyph_w, glyph_h, raw)
                if key not in unique:
                    unique[key] = {
                        "width": glyph_w,
                        "height": glyph_h,
                        "raw": raw,
                        "source_offset": int(row["offset"]),
                        "source_lba": int(row["lba"]),
                        "source_index": index,
                    }
        rows.append(
            {
                **row,
                "version": data[:16].split(b"\0", 1)[0].decode("ascii", "replace"),
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
                # Kept as both names for compatibility with the first catalog
                # produced during reverse engineering.
                "flags": local_code_base,
                "local_code_base": local_code_base,
                "mapping_count": mapping_count,
                "glyph_bytes": glyph_bytes,
                "trailing_padding": len(trailing),
                "geometry_valid": int(valid),
                "file_sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if number and number % 500 == 0:
            print(f"processed {number}/{len(source)}, unique glyphs={len(unique)}", flush=True)

    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "mclib_catalog.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    # Export a union contact sheet for the requested cell size.  The raw hash
    # keeps identical glyphs shared by thousands of per-message libraries only once.
    selected = [v for v in unique.values() if v["width"] == args.union_cell and v["height"] == args.union_cell]
    selected.sort(key=lambda v: (int(v["source_offset"]), int(v["source_index"])))
    columns, cell = 32, args.union_cell
    rows_n = math.ceil(len(selected) / columns)
    contact = Image.new("L", (columns * cell, rows_n * cell), 0)
    index_rows = []
    for union_index, item in enumerate(selected):
        raw = bytes(item["raw"])
        values = []
        for value in raw:
            values.extend((value & 15, value >> 4))
        glyph = Image.new("L", (cell, cell))
        glyph.putdata([value * 17 for value in values[: cell * cell]])
        contact.paste(glyph, ((union_index % columns) * cell, (union_index // columns) * cell))
        index_rows.append({k: v for k, v in item.items() if k != "raw"} | {"union_index": union_index, "sha256": hashlib.sha256(raw).hexdigest()})
    contact.resize((contact.width * 2, contact.height * 2), Image.Resampling.NEAREST).save(
        args.output / f"unique_{cell}px_glyphs.png"
    )
    with (args.output / f"unique_{cell}px_glyphs.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(index_rows[0]))
        writer.writeheader()
        writer.writerows(index_rows)

    stats = Counter((int(r["glyph_width"]), int(r["glyph_height"]), int(r["geometry_valid"])) for r in rows)
    print(f"libraries={len(rows)}, unique_glyphs_all_sizes={len(unique)}, union_{cell}px={len(selected)}")
    print("geometry_stats", dict(stats))


if __name__ == "__main__":
    main()

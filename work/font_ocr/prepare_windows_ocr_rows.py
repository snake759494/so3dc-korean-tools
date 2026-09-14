#!/usr/bin/env python3
"""Build deterministic Japanese OCR row images from the SO3 unique glyph atlas."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ATLAS = ROOT / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.png"
DEFAULT_CSV = ROOT / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.csv"
DEFAULT_OUTPUT = ROOT / "work/font_ocr/windows_rows"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--atlas", type=Path, default=DEFAULT_ATLAS)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=int, default=291, help="first decimal union index")
    parser.add_argument("--end", type=int, default=None, help="exclusive decimal union index")
    parser.add_argument("--columns", type=int, default=16)
    args = parser.parse_args()

    with args.csv.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    for expected, row in enumerate(rows):
        if int(row["union_index"]) != expected:
            raise ValueError(f"non-contiguous union index at CSV row {expected}")

    end = len(rows) if args.end is None else args.end
    if not (0 <= args.start < end <= len(rows)):
        raise ValueError(f"invalid union range {args.start}..{end} for {len(rows)} rows")

    geometry = {(int(row["width"]), int(row["height"])) for row in rows}
    if len(geometry) != 1:
        raise ValueError(f"mapping CSV mixes glyph geometries: {sorted(geometry)}")
    glyph_width, glyph_height = next(iter(geometry))
    if glyph_width != glyph_height:
        raise ValueError(f"only square glyph atlases are supported, got {glyph_width}x{glyph_height}")

    source = Image.open(args.atlas).convert("L")
    source_columns = 32
    source_cell = glyph_width * 2  # catalog union atlases are enlarged 2x.
    expected_rows = (len(rows) + source_columns - 1) // source_columns
    if source.size != (source_columns * source_cell, expected_rows * source_cell):
        raise ValueError(
            f"unexpected atlas geometry {source.size}; expected "
            f"{(source_columns * source_cell, expected_rows * source_cell)}"
        )

    variants = [
        {"name": "gray_bicubic_96", "resample": "bicubic", "binary": False},
        {"name": "gray_nearest_96", "resample": "nearest", "binary": False},
        {"name": "binary_nearest_96", "resample": "nearest", "binary": True},
    ]
    output_cell = glyph_width * 4
    output_height = output_cell + 16
    top_padding = 8
    selected = rows[args.start:end]
    row_records: list[dict[str, object]] = []

    for row_number, begin in enumerate(range(0, len(selected), args.columns)):
        cells = selected[begin : begin + args.columns]
        record: dict[str, object] = {
            "row_number": row_number,
            "cells": [
                {
                    "column": column,
                    "union_index": int(item["union_index"]),
                    "source_index": int(item["source_index"]),
                    "bitmap_sha256": item["sha256"],
                }
                for column, item in enumerate(cells)
            ],
            "images": {},
        }
        for variant in variants:
            variant_dir = args.output / str(variant["name"])
            variant_dir.mkdir(parents=True, exist_ok=True)
            canvas = Image.new("L", (len(cells) * output_cell, output_height), 255)
            for column, item in enumerate(cells):
                index = int(item["union_index"])
                x = (index % source_columns) * source_cell
                y = (index // source_columns) * source_cell
                glyph = ImageOps.invert(source.crop((x, y, x + source_cell, y + source_cell)))
                if variant["binary"]:
                    glyph = glyph.point(lambda value: 0 if value < 192 else 255)
                resample = (
                    Image.Resampling.BICUBIC
                    if variant["resample"] == "bicubic"
                    else Image.Resampling.NEAREST
                )
                glyph = glyph.resize((output_cell, output_cell), resample)
                canvas.paste(glyph, (column * output_cell, top_padding))
            filename = f"row_{row_number:04d}_u{int(cells[0]['union_index']):04X}.png"
            path = variant_dir / filename
            canvas.save(path, optimize=True)
            record["images"][str(variant["name"])] = str(path.resolve())
        row_records.append(record)

    manifest = {
        "schema_version": 1,
        "title": f"SO3 {glyph_width}px unique glyph rows for Windows Japanese OCR",
        "atlas": str(args.atlas.resolve()),
        "atlas_sha256": file_sha256(args.atlas),
        "mapping_csv": str(args.csv.resolve()),
        "mapping_csv_sha256": file_sha256(args.csv),
        "union_index_start": args.start,
        "union_index_end_exclusive": end,
        "glyph_count": len(selected),
        "glyph_width": glyph_width,
        "glyph_height": glyph_height,
        "row_columns": args.columns,
        "output_cell_width": output_cell,
        "output_row_height": output_height,
        "variants": [item["name"] for item in variants],
        "rows": row_records,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {manifest_path}: {len(selected)} glyphs, "
        f"{len(row_records)} rows, {len(variants)} variants"
    )


if __name__ == "__main__":
    main()

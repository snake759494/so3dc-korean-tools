#!/usr/bin/env python3
"""Create a labelled page from the 2x-scaled 24px union glyph atlas."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("atlas", type=Path)
    ap.add_argument("mapping", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--start", type=int, default=291)
    ap.add_argument("--count", type=int, default=1024)
    args = ap.parse_args()
    with args.mapping.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))[args.start : args.start + args.count]
    source = Image.open(args.atlas).convert("L")
    source_columns, glyph_px = 32, 48
    columns, cell_w, cell_h = 16, 64, 66
    sheet = Image.new("RGB", (columns * cell_w, math.ceil(len(rows) / columns) * cell_h), "#202020")
    draw = ImageDraw.Draw(sheet)
    for output_index, row in enumerate(rows):
        union_index = int(row["union_index"])
        sx, sy = (union_index % source_columns) * glyph_px, (union_index // source_columns) * glyph_px
        glyph = source.crop((sx, sy, sx + glyph_px, sy + glyph_px)).convert("RGB")
        x, y = (output_index % columns) * cell_w, (output_index // columns) * cell_h
        sheet.paste(glyph, (x + 8, y))
        draw.text((x + 2, y + 49), f"U{union_index:04X} S{int(row['source_index']):03X}", fill="white")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output)


if __name__ == "__main__":
    main()

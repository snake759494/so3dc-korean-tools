#!/usr/bin/env python3
"""Dedicated contact sheets for disc-2-only textures (catalog entries with
d2_only=true, deduped by src sha), rendered at NATIVE resolution over a
checkerboard so text stays legible for visual review. -> sheets\new_*.png"""
from __future__ import annotations
import json
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\work\img_ko_d2")
SHEETS = OUT / "sheets"
PER_SHEET = 6
PAD, LABEL_H = 12, 30
MAX_W = 1600


def checker(size, a=(70, 70, 70), b=(96, 96, 96), s=8):
    bg = Image.new("RGB", size, a)
    dr = ImageDraw.Draw(bg)
    for yy in range(0, size[1], s):
        for xx in range(0, size[0], s):
            if ((xx // s) + (yy // s)) & 1:
                dr.rectangle([xx, yy, xx + s, yy + s], fill=b)
    return bg


def main():
    cat = json.load(open(OUT / "texture_catalog.json", encoding="utf-8"))
    seen, reps = set(), []
    for c in cat:
        if c.get("d2_only") and c["decode_ok"] and c["src_sha256"] not in seen:
            seen.add(c["src_sha256"])
            reps.append(c)
    reps.sort(key=lambda c: (c["archive_id"], c["stream_id"]))
    print(f"disc2-only unique textures: {len(reps)}")
    for page_no in range(0, (len(reps) + PER_SHEET - 1) // PER_SHEET):
        page = reps[page_no * PER_SHEET:(page_no + 1) * PER_SHEET]
        imgs = []
        for c in page:
            im = Image.open(OUT / c["png_path"]).convert("RGBA")
            comp = Image.alpha_composite(
                checker(im.size).convert("RGBA"), im).convert("RGB")
            imgs.append((c, comp))
        # simple vertical stack (few, large images)
        w = min(MAX_W, max(i.width for _, i in imgs) + 2 * PAD)
        h = 40 + sum(i.height + LABEL_H + PAD for _, i in imgs)
        sheet = Image.new("RGB", (w, h), (24, 24, 24))
        dr = ImageDraw.Draw(sheet)
        dr.text((6, 10), f"new_{page_no:03d}: disc-2-only textures "
                         f"(native size)", fill="white")
        y = 40
        for c, im in imgs:
            lbl = (f"{c['archive_id']}:{c['stream_id']} {c['name']} "
                   f"{c['width']}x{c['height']} {c['bpp']}b "
                   f"e={c['edge_score']} cov={c['coverage']}")
            dr.text((PAD, y), lbl, fill="#e6e6a0")
            y += LABEL_H
            sheet.paste(im, (PAD, y))
            y += im.height + PAD
        name = f"new_{page_no:03d}.png"
        sheet.save(SHEETS / name)
        print(f"  {name}: {len(page)} imgs -> {SHEETS/name}")


if __name__ == "__main__":
    main()

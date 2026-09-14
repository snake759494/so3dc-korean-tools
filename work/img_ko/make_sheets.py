#!/usr/bin/env python3
"""Build deduplicated, priority-grouped contact sheets for visual text review,
and annotate the catalog with dedup + sheet assignments."""
from __future__ import annotations
import argparse, json, hashlib, collections
from pathlib import Path
from PIL import Image, ImageDraw

# disc-1 default; disc-2: --out ..\img_ko_d2
OUT = Path(__file__).resolve().parent
SHEETS = OUT / "sheets"

# review priority: lower number = reviewed first (most likely to hold JP text)
GROUP_ORDER = {"strt": 0, "kit": 1, "SHI": 2, "ANKF": 3, "John": 4,
               "yam1": 5, "kami": 6, "asai": 7}
GROUP_LABEL = {
    "strt": "TITLE / LOGO / MENU / SYSTEM screens",
    "kit": "SYSTEM screens (GAME OVER etc.)",
    "SHI": "UI button / help / menu atlases (+ kana)",
    "ANKF": "ASCII font atlas",
    "John": "numeric font + misc art",
    "yam1": "icon / item / element atlases",
    "kami": "character portraits",
    "asai": "field / map tilesets",
}

COLS, ROWS = 5, 5
PER = COLS * ROWS
THUMB = 236           # max thumb dimension
CELL_W, LABEL_H = 250, 34
CELL_H = THUMB + LABEL_H + 6


def checker(size, a=(70, 70, 70), b=(96, 96, 96), s=8):
    bg = Image.new("RGB", size, a)
    dr = ImageDraw.Draw(bg)
    for yy in range(0, size[1], s):
        for xx in range(0, size[0], s):
            if ((xx // s) + (yy // s)) & 1:
                dr.rectangle([xx, yy, xx + s, yy + s], fill=b)
    return bg


def make_thumb(png_path):
    im = Image.open(png_path).convert("RGBA")
    w, h = im.size
    scale = THUMB / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resample = Image.NEAREST if scale >= 1 else Image.LANCZOS
    im = im.resize((nw, nh), resample)
    bg = checker((nw, nh))
    return Image.alpha_composite(bg.convert("RGBA"), im).convert("RGB")


def main():
    global OUT, SHEETS
    ap = argparse.ArgumentParser(description="dedup + contact sheets (default = disc 1)")
    ap.add_argument("--out", type=Path, default=OUT,
                    help="img_ko output root holding png\\ + texture_catalog.json")
    OUT = ap.parse_args().out
    SHEETS = OUT / "sheets"
    SHEETS.mkdir(exist_ok=True)
    cat = json.load(open(OUT / "texture_catalog.json", encoding="utf-8"))
    # dedup by png bytes
    groups = collections.defaultdict(list)
    for c in cat:
        b = (OUT / c["png_path"]).read_bytes()
        c["_hash"] = hashlib.md5(b).hexdigest()
        groups[c["_hash"]].append(c)
    reps = []
    for hh, members in groups.items():
        members.sort(key=lambda c: (c["archive_id"], c["stream_id"]))
        rep = members[0]
        rep["_dup_count"] = len(members)
        reps.append(rep)
    # sort reps for review
    reps.sort(key=lambda c: (GROUP_ORDER.get(c["base_name"], 9),
                             -(c["width"] or 0) * (c["height"] or 0),
                             c["archive_id"], c["stream_id"]))

    # assign to sheets, keeping group boundaries reasonably clean:
    # start a new sheet when the group changes AND current sheet has >= ~half full
    sheet_assign = {}   # hash -> sheet filename
    page = []
    sheet_no = 0
    sheets_meta = []

    def flush():
        nonlocal page, sheet_no
        if not page:
            return
        grp = page[0]["base_name"]
        sheet_name = f"sheet_{sheet_no:03d}_{grp}.png"
        img = Image.new("RGB", (COLS * CELL_W, 40 + ROWS * CELL_H), (24, 24, 24))
        dr = ImageDraw.Draw(img)
        groups_here = sorted({p["base_name"] for p in page},
                             key=lambda g: GROUP_ORDER.get(g, 9))
        title = f"sheet {sheet_no:03d}  |  " + " + ".join(
            f"{g}: {GROUP_LABEL.get(g,'')}" for g in groups_here)
        dr.text((6, 12), title[:150], fill="white")
        for i, c in enumerate(page):
            cx = (i % COLS) * CELL_W
            cy = 40 + (i // COLS) * CELL_H
            th = make_thumb(OUT / c["png_path"])
            ox = cx + (CELL_W - th.width) // 2
            img.paste(th, (ox, cy + 2))
            dups = c["_dup_count"]
            tflag = "  <TEXT>" if c["likely_text"] else ""
            l1 = f"{c['archive_id']}:{c['stream_id']} {c['base_name']} {c['width']}x{c['height']} {c['bpp']}b"
            l2 = f"x{dups}  e={c['edge_score']} cov={c['coverage']}{tflag}"
            ly = cy + THUMB + 6
            dr.text((cx + 4, ly), l1[:40], fill="#e6e6a0")
            dr.text((cx + 4, ly + 14), l2[:40], fill=("#ff9090" if c["likely_text"] else "#a0c0e0"))
            sheet_assign[c["_hash"]] = sheet_name
        img.save(SHEETS / sheet_name)
        sheets_meta.append({"sheet": sheet_name, "count": len(page),
                            "groups": groups_here})
        page = []
        sheet_no += 1

    prev_grp = None
    for c in reps:
        if page and c["base_name"] != prev_grp and len(page) >= PER - COLS:
            flush()
        page.append(c)
        prev_grp = c["base_name"]
        if len(page) >= PER:
            flush()
    flush()

    # annotate every catalog entry with dedup + sheet
    for c in cat:
        hh = c["_hash"]
        c["dup_group"] = hh[:12]
        c["dup_count"] = len(groups[hh])
        c["is_representative"] = (c is groups[hh][0]) or (
            c["archive_id"] == groups[hh][0]["archive_id"] and
            c["stream_id"] == groups[hh][0]["stream_id"])
        c["sheet"] = sheet_assign.get(hh)
        del c["_hash"]
        for k in ("_dup_count",):
            c.pop(k, None)
    json.dump(cat, open(OUT / "texture_catalog.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print(f"unique images: {len(reps)}  sheets: {sheet_no}")
    for m in sheets_meta:
        print(f"  {m['sheet']}: {m['count']} imgs  groups={m['groups']}")


if __name__ == "__main__":
    main()

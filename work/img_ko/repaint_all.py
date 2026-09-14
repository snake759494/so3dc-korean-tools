#!/usr/bin/env python3
"""Drive the Korean repaint of the 4 fixed-text FIS textures and emit
patched member bins, SLZ, before/after previews, and repaint_report.json.

Uses fis_repaint.py (decode + byte-exact re-encode + SLZ fit check) and
image_translations.json (the Korean lines).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

import fis_repaint as F

OUT = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\work\img_ko")
PREV = OUT / "preview"
PATCHED = OUT / "patched"
PREV.mkdir(exist_ok=True)
PATCHED.mkdir(exist_ok=True)
TRANS = json.load(open(OUT / "image_translations.json", encoding="utf-8"))

TARGETS = {
    (40, 25): {"off": 489682104, "next_rel": 31372},
    (42, 36): {"off": 491065344, "next_rel": 28112},
    (44, 41): {"off": 491149312, "next_rel": 54112},
    (2254, 38031): {"off": 2738751360, "next_rel": 49104},
}


def detect_lines(mask, gap=4):
    rows = np.where(mask.any(axis=1))[0]
    if len(rows) == 0:
        return []
    bands = []
    s = p = rows[0]
    for y in rows[1:]:
        if y - p > gap:
            bands.append((s, p))
            s = y
        p = y
    bands.append((s, p))
    out = []
    for y0, y1 in bands:
        cols = np.where(mask[y0:y1 + 1].any(axis=0))[0]
        out.append((int(y0), int(y1), int(cols[0]), int(cols[-1])))
    return out


def repaint_panel(tex, lines_ko, *, ink_index, outline_index, bg_index,
                  text_mask, body_mask, cand, align="right", stroke_w=1,
                  bg_opaque=True, left_margin=6, right_pad=0, vpad=3, hpad=2,
                  detect_mask=None, gap=4, anchor_x=None,
                  aligns=None, anchor_xs=None, stroke_on=True,
                  canvas=None, new_idx=None):
    """Generic white-ink + dark-outline panel repaint (two-pass: erase-all then
    paste-all so adjacent lines never clobber each other).

    detect_mask groups lines (defaults to text_mask); body_mask (white ink) sets
    matched glyph height + vertical centre; text_mask sets the erase span.
    anchor_x: fixed right/left anchor x (else per-line original edge)."""
    pal = tex.pal
    ink_rgba = pal[ink_index]
    outl_rgba = pal[outline_index]
    bg_rgba = pal[bg_index] if bg_opaque else np.array([0, 0, 0, 0], np.uint8)
    lines = detect_lines(detect_mask if detect_mask is not None else text_mask, gap=gap)
    assert len(lines) == len(lines_ko), \
        f"detected {len(lines)} lines but have {len(lines_ko)} ko lines: {lines}"

    if canvas is None:
        canvas = tex.rgba.astype(np.uint8).copy()
    if new_idx is None:
        new_idx = tex.idx.copy()
    H, W = tex.h, tex.w
    sw = stroke_w if stroke_on else 0
    plans = []
    for li, (((y0, y1, x0, x1), ko)) in enumerate(zip(lines, lines_ko)):
        lalign = aligns[li] if aligns is not None else align
        lanchor = (anchor_xs[li] if anchor_xs is not None else anchor_x)
        # matched glyph height + vertical centre from the WHITE INK body
        band_body = body_mask[y0:y1 + 1]
        brows = np.where(band_body.any(axis=1))[0]
        if len(brows):
            body_y0 = y0 + int(brows[0]); body_y1 = y0 + int(brows[-1])
        else:
            body_y0, body_y1 = y0, y1
        cy = (body_y0 + body_y1) / 2.0
        # erase span from text_mask (outline-inclusive) within this band's rows
        by0e = max(0, y0 - vpad); by1e = min(H, y1 + 1 + vpad)
        tm_band = text_mask[by0e:by1e]
        tcols = np.where(tm_band.any(axis=0))[0]
        ox0, ox1 = (int(tcols[0]), int(tcols[-1])) if len(tcols) else (x0, x1)
        if lalign == "right":
            avail_w = (lanchor if lanchor is not None else ox1) - left_margin
        else:
            la = lanchor if lanchor is not None else ox0
            avail_w = (W - left_margin) - la
        target_h = body_y1 - body_y0 + 1
        r = F.render_line_to_height(ko, target_h, ink_rgba, outl_rgba, stroke_w)
        bw = r["body_bbox"][2] - r["body_bbox"][0]
        if bw > avail_w:
            target_h = max(6, int(target_h * avail_w / bw))
            r = F.render_line_to_height(ko, target_h, ink_rgba, outl_rgba, stroke_w)
        bx0, by0, bx1, by1 = r["body_bbox"]
        bcy = (by0 + by1) / 2.0
        dy = int(round(cy - bcy))
        if lalign == "right":
            ax = lanchor if lanchor is not None else ox1
            dx = int(round((ax + 1 + right_pad) - bx1))
        else:
            ax = lanchor if lanchor is not None else ox0
            dx = int(round(ax - bx0))
        sx0, sy0, sx1, sy1 = r["strk_bbox"]
        gx0 = max(0, min(ox0, dx + sx0) - hpad)
        gx1 = min(W, max(ox1 + 1, dx + sx1) + hpad)
        plans.append({"ko": ko, "orig_box": [ox0, y0, ox1, y1],
                      "font_size": r["font_size"], "target_h": target_h,
                      "place": [dx, dy], "tile": r["tile"],
                      "erase": (gx0, by0e, gx1, by1e)})

    # pass 1: erase every line's span
    for p in plans:
        gx0, ey0, gx1, ey1 = p["erase"]
        canvas[ey0:ey1, gx0:gx1] = bg_rgba
    # pass 2: paste every Korean tile
    edited = []
    for p in plans:
        _, wbb = F.paste_tile_over(canvas, p["tile"], *p["place"])
        eb = p["erase"]
        if wbb:
            eb = (min(eb[0], wbb[0]), min(eb[1], wbb[1]),
                  max(eb[2], wbb[2]), max(eb[3], wbb[3]))
        edited.append(eb)
    # quantize each edited bbox back to candidate palette indices
    for (ex0, ey0, ex1, ey1) in edited:
        sub = canvas[ey0:ey1, ex0:ex1]
        q = F.quantize_region(sub, pal, cand, alpha_weight=1.0)
        new_idx[ey0:ey1, ex0:ex1] = q
    info = [{k: p[k] for k in ("ko", "orig_box", "font_size", "target_h", "place")}
            for p in plans]
    return new_idx, edited, info, lines


def repaint_john(tex, blocks, *, ink_index=15, black_index=3, gap=3,
                 line_pad=2):
    """Repaint the 4bpp John disc-message atlas.  `blocks` is a list of dicts:
      {box:(y0,y1,x0,x1), bg:'trans'|'black', lines_ko:[...]}.
    Normal blocks: white-on-transparent, erase per-line span to the block's own
    transparent index.  Highlighted blocks: white-on-black, erase the whole
    black rectangle to `black_index` then repaint.  White AA maps onto the
    16-entry grayscale ramp (no separate outline stroke)."""
    pal = tex.pal
    A = pal[:, 3]
    L = F.lum_of(pal)
    idx = tex.idx
    canvas = tex.rgba.astype(np.uint8).copy()
    new_idx = idx.copy()
    ink_rgba = pal[ink_index]
    cand_all = list(range(16))
    info = []
    # bright glyph mask (excludes both the transparent and near-black bg) for
    # line segmentation; pure-white core for a height reference that is stable
    # across the transparent- and black-bg copies of each message (the AA glow
    # around white-on-black would otherwise inflate the measured height).
    bright = (L[idx] > 90) & (A[idx] > 128)

    def find_lines(box, N):
        """Split a block's bright vertical extent into N equal segments (the N
        lines are evenly pitched but nearly touch) and measure each line."""
        y0, y1, x0, x1 = box
        brows = np.where(bright[y0:y1, x0:x1].any(axis=1))[0]
        assert len(brows), f"john block {box}: no bright text"
        ty0, ty1 = y0 + int(brows[0]), y0 + int(brows[-1])
        span = ty1 - ty0 + 1
        out = []
        for i in range(N):
            s0 = ty0 + round(i * span / N)
            s1 = ty0 + round((i + 1) * span / N)
            seg = bright[s0:s1, x0:x1]
            rr = np.where(seg.any(axis=1))[0]
            cc = np.where(seg.any(axis=0))[0]
            if not len(rr) or not len(cc):
                out.append((s0, s1 - 1, x0, x1 - 1))
            else:
                out.append((s0 + int(rr[0]), s0 + int(rr[-1]),
                            x0 + int(cc[0]), x0 + int(cc[-1])))
        return out

    # pass 1: per-message visual glyph height from the NORMAL (transparent) copy,
    # whose bright extent is not inflated by the white-on-black AA glow.
    msg_th = {}
    for blk in blocks:
        if blk["bg"] == "trans":
            ls = find_lines(blk["box"], len(blk["lines_ko"]))
            hs = [b - a + 1 for (a, b, c, d) in ls]
            msg_th[blk["msg"]] = int(round(float(np.median(hs))))

    for blk in blocks:
        y0, y1, x0, x1 = blk["box"]
        kos = blk["lines_ko"]
        N = len(kos)
        lines = find_lines(blk["box"], N)
        left_x = min(ln[2] for ln in lines)

        if blk["bg"] == "black":
            op = (A[idx[y0:y1, x0:x1]] > 128)
            rows = np.where(op.any(axis=1))[0]
            cols = np.where(op.any(axis=0))[0]
            ry0, ry1 = y0 + int(rows[0]), y0 + int(rows[-1]) + 1
            rx0, rx1 = x0 + int(cols[0]), x0 + int(cols[-1]) + 1
            canvas[ry0:ry1, rx0:rx1] = pal[black_index]
            bg_first = black_index
            avail_w = rx1 - left_x - 4
        else:
            reg = idx[y0:y1, x0:x1]
            tvals = reg[A[reg] < 40]
            bg_first = int(np.bincount(tvals.ravel()).argmax()) if tvals.size else 0
            avail_w = x1 - left_x - 2

        # one uniform glyph height per message (matches the original), taken from
        # the message's normal copy so both copies render at the same size.
        block_th = msg_th[blk["msg"]]
        # shrink uniformly if the widest rendered line would overflow the box
        widest = 1.0
        for ko in kos:
            rr = F.render_line_to_height(ko, block_th, ink_rgba, ink_rgba, 0)
            widest = max(widest, (rr["body_bbox"][2] - rr["body_bbox"][0]) / avail_w)
        if widest > 1.0:
            block_th = max(6, int(block_th / widest))

        plans = []
        for (ly0, ly1, lx0, lx1) in lines:
            cy = (ly0 + ly1) / 2.0
            ko = kos[len(plans)]
            r = F.render_line_to_height(ko, block_th, ink_rgba, ink_rgba, 0)
            bx0, by0, bx1, by1 = r["body_bbox"]
            bcy = (by0 + by1) / 2.0
            dy = int(round(cy - bcy))
            dx = int(round(left_x - bx0))
            sx0, sy0, sx1, sy1 = r["strk_bbox"]
            ex0 = max(0, min(lx0, dx + sx0) - line_pad)
            ex1 = min(tex.w, max(lx1 + 1, dx + sx1) + line_pad)
            ey0 = max(0, ly0 - line_pad); ey1 = min(tex.h, ly1 + 1 + line_pad)
            plans.append({"tile": r["tile"], "place": (dx, dy),
                          "erase": (ex0, ey0, ex1, ey1), "ko": ko,
                          "font_size": r["font_size"], "orig_line": [lx0, ly0, lx1, ly1]})

        if blk["bg"] == "trans":
            for p in plans:
                gx0, gy0, gx1, gy1 = p["erase"]
                canvas[gy0:gy1, gx0:gx1] = (0, 0, 0, 0)
        qboxes = []
        for p in plans:
            _, wbb = F.paste_tile_over(canvas, p["tile"], *p["place"])
            eb = p["erase"]
            if wbb:
                eb = (min(eb[0], wbb[0]), min(eb[1], wbb[1]),
                      max(eb[2], wbb[2]), max(eb[3], wbb[3]))
            qboxes.append(eb)
        cand = [bg_first] + [c for c in cand_all if c != bg_first]
        if blk["bg"] == "black":
            q = F.quantize_region(canvas[ry0:ry1, rx0:rx1], pal, cand)
            new_idx[ry0:ry1, rx0:rx1] = q
        else:
            for (qx0, qy0, qx1, qy1) in qboxes:
                q = F.quantize_region(canvas[qy0:qy1, qx0:qx1], pal, cand)
                new_idx[qy0:qy1, qx0:qx1] = q
        info.append({"box": blk["box"], "bg": blk["bg"], "msg": blk["msg"],
                     "glyph_px": block_th,
                     "lines": [{"ko": p["ko"], "font_size": p["font_size"],
                                "orig_line": p["orig_line"], "place": list(p["place"])}
                               for p in plans]})
    return new_idx, canvas, info


def save_preview(rgba, path):
    """Save a full-size preview.  Transparency is composited over a neutral mid
    grey so white-on-transparent text is visible for verification (the true
    decoded RGBA is preserved in the patched .bin members)."""
    im = Image.fromarray(rgba, "RGBA")
    bg = Image.new("RGBA", im.size, (110, 110, 110, 255))
    Image.alpha_composite(bg, im).convert("RGB").save(path)


def repaint_caption(tex, ko, bbox, *, med_size=7, thr=95, dilate=1,
                    edge=(30, 30, 30), core_thr=180, y_pad=1):
    """Repaint a 24bpp direct-color attack-label caption: mask the JP glyphs,
    inpaint from the surrounding photo, render the Korean centred on the same
    glyph centre at the same height (white ink + thin dark edge).  Returns
    (new_rgb (h,w,3), info)."""
    rgb = tex.rgba[:, :, :3].copy()
    mask = F.caption_mask(rgb, bbox, med_size, thr, dilate)   # dilated: for erase
    tight = F.caption_mask(rgb, bbox, med_size, thr, 0)       # tight: for metrics
    assert mask.any() and tight.any(), f"caption mask empty for bbox {bbox}"
    y0b, y1b, x0b, x1b = bbox
    # glyph BODY height/centre from the mask row profile (background-independent
    # and consistent across the 4 same-size captions): rows whose text-pixel
    # count exceeds 35% of the peak row.
    rowcount = tight.sum(axis=1).astype(np.int32)
    peak = int(rowcount[y0b:y1b].max())
    grows = np.where(rowcount > 0.35 * peak)[0]
    core_y0, core_y1 = int(grows.min()), int(grows.max())
    vcenter = (core_y0 + core_y1) / 2.0
    target_h = core_y1 - core_y0 + 1
    # robust horizontal centre (2..98 pct of the tight-mask x pixels)
    cx = np.where(tight)[1]
    xs = np.sort(cx)
    hx0 = int(xs[int(0.02 * (len(xs) - 1))]); hx1 = int(xs[int(0.98 * (len(xs) - 1))])
    hcenter = (hx0 + hx1) / 2.0

    inp = F.inpaint_harmonic(rgb, mask, iters=250)
    # render Korean white + thin dark edge, on transparent
    r = F.render_line_to_height(ko, target_h, (255, 255, 255, 255),
                                tuple(edge) + (255,), stroke_w=1)
    tile = r["tile"]
    bx0, by0, bx1, by1 = r["body_bbox"]
    dx = int(round(hcenter - (bx0 + bx1) / 2.0))
    dy = int(round(vcenter - (by0 + by1) / 2.0))
    canvas = np.dstack([inp, np.full(inp.shape[:2], 255, np.uint8)])
    canvas, wbb = F.paste_tile_over(canvas, tile, dx, dy)
    new_rgb = canvas[:, :, :3].copy()
    info = {"ko": ko, "caption_bbox": list(bbox),
            "mask_px": int(mask.sum()), "orig_glyph_px": int(target_h),
            "glyph_center": [round(hcenter, 1), round(vcenter, 1)],
            "font_size": r["font_size"], "place": [dx, dy],
            "core_y": [core_y0, core_y1], "core_x": [hx0, hx1]}
    return new_rgb, mask, inp, info


def finalize(tex, new_idx, arch, strm, next_rel, report, member=None, alloc=None):
    """Encode, recompress, check fit; save after preview + patched bins.
    `member` overrides the encode (e.g. 24bpp direct); `alloc` overrides the
    payload allocation (for a last member with next_rel==0)."""
    if member is None:
        member = tex.encode(new_idx)
    payload_space = alloc if alloc is not None else (next_rel - 16)
    fit, comp = F.recompress_and_check(member, payload_space)
    # before preview from the original decode; after from the patched member
    save_preview(tex.rgba, PREV / f"{arch}_{strm}_before.png")
    ptex = F.FISTexture(member)
    save_preview(ptex.rgba, PREV / f"{arch}_{strm}_after.png")
    (PATCHED / f"{arch}_{strm}.bin").write_bytes(member)
    (PATCHED / f"{arch}_{strm}.slz").write_bytes(
        b"SLZ\x02" + int(len(comp)).to_bytes(4, "little")
        + int(len(member)).to_bytes(4, "little")
        + int(next_rel).to_bytes(4, "little") + comp)
    report[f"{arch}:{strm}"]["fit"] = {
        "old_compressed": report[f"{arch}:{strm}"].pop("_old_comp"),
        "new_compressed": fit["new_compressed"],
        "allocation": payload_space,
        "fits": fit["fits"],
        "slack": fit["slack"],
    }
    print(f"{arch}:{strm} new_comp={fit['new_compressed']} space={payload_space} "
          f"fits={fit['fits']} slack={fit['slack']}")
    return fit


# ===========================================================================
if __name__ == "__main__":
    import sys
    only = sys.argv[1] if len(sys.argv) > 1 else None
    report = {}

    def get_lines(label_key):
        for e in TRANS["fixed_text"]:
            if e["label"].startswith(label_key):
                return e
        raise KeyError(label_key)

    # ---- 40:25 : navy panel, 7 white lines, right-aligned ----
    if only in (None, "40"):
        t = TARGETS[(40, 25)]
        m, _ = F.load_member_from_iso(F._ORIG_ISO, t["off"])
        tex = F.FISTexture(m)
        pal = tex.pal
        L = F.lum_of(pal)
        tmask = np.abs(L[tex.idx] - L[1]) > 28
        bmask = L[tex.idx] > 150
        cand = np.unique(tex.idx).tolist()
        ko = get_lines("40:25")["lines_ko"]
        report["40:25"] = {"label": "40:25 strt 512x256 8b", "text_index": 41,
                           "bg_index": 1, "outline_index": 14, "_old_comp": 31353}
        new_idx, edited, info, lines = repaint_panel(
            tex, ko, ink_index=41, outline_index=14, bg_index=1,
            text_mask=tmask, body_mask=bmask, cand=cand, align="right",
            stroke_w=1, bg_opaque=True, left_margin=6)
        report["40:25"]["orig_glyph_px"] = [ln[1] - ln[0] + 1 for ln in lines]
        report["40:25"]["lines"] = info
        finalize(tex, new_idx, 40, 25, t["next_rel"], report)

    # ---- helper: region-restricted masks + candidate set ----
    def region_masks(tex, region, ink_lum=150, alpha_thr=40):
        pal = tex.pal; L = F.lum_of(pal); A = pal[:, 3]
        y0, y1, x0, x1 = region
        amask = A[tex.idx] > alpha_thr
        lmask = L[tex.idx] > ink_lum
        reg = np.zeros(tex.idx.shape, bool)
        reg[y0:y1, x0:x1] = True
        text_mask = amask & reg
        body_mask = amask & lmask & reg
        cand = np.unique(tex.idx[y0:y1, x0:x1]).tolist()
        return text_mask, body_mask, cand

    # ---- 42:36 : Install screen, 2 mixed EN+KO lines, transparent, right-aligned ----
    if only in (None, "42"):
        t = TARGETS[(42, 36)]
        m, _ = F.load_member_from_iso(F._ORIG_ISO, t["off"])
        tex = F.FISTexture(m)
        region = (400, 445, 150, 470)
        tmask, bmask, cand = region_masks(tex, region)
        cand = [57] + [c for c in cand if c != 57]   # bg (transparent) wins ties
        ko = get_lines("42:36")["lines_ko"]
        report["42:36"] = {"label": "42:36 strt 512x512 8b", "text_index": 255,
                           "bg_index": 57, "outline_index": 177, "_old_comp": 28093}
        new_idx, edited, info, lines = repaint_panel(
            tex, ko, ink_index=255, outline_index=177, bg_index=57,
            text_mask=tmask, body_mask=bmask, detect_mask=bmask, cand=cand,
            align="right", stroke_w=1, bg_opaque=False, gap=4,
            anchor_x=456, vpad=4)
        report["42:36"]["orig_glyph_px"] = [ln[1] - ln[0] + 1 for ln in lines]
        report["42:36"]["lines"] = info
        finalize(tex, new_idx, 42, 36, t["next_rel"], report)

    # ---- 44:41 : Music mode, 2 mixed EN+KO lines, transparent, right-aligned ----
    if only in (None, "44"):
        t = TARGETS[(44, 41)]
        m, _ = F.load_member_from_iso(F._ORIG_ISO, t["off"])
        tex = F.FISTexture(m)
        region = (428, 470, 18, 392)
        tmask, bmask, cand = region_masks(tex, region)
        cand = [1] + [c for c in cand if c != 1]
        ko = get_lines("44:41")["lines_ko"]
        report["44:41"] = {"label": "44:41 strt 512x512 8b", "text_index": 7,
                           "bg_index": 1, "outline_index": 6, "_old_comp": 54093}
        new_idx, edited, info, lines = repaint_panel(
            tex, ko, ink_index=7, outline_index=6, bg_index=1,
            text_mask=tmask, body_mask=bmask, detect_mask=bmask, cand=cand,
            stroke_w=1, bg_opaque=False, gap=2,
            aligns=["left", "right"], anchor_xs=[24, 380], vpad=3)
        report["44:41"]["orig_glyph_px"] = [ln[1] - ln[0] + 1 for ln in lines]
        report["44:41"]["lines"] = info
        finalize(tex, new_idx, 44, 41, t["next_rel"], report)

    # ---- 2254:38031 : John disc-message atlas, 4bpp, 6 messages x2 copies ----
    if only in (None, "john", "2254"):
        t = TARGETS[(2254, 38031)]
        m, _ = F.load_member_from_iso(F._ORIG_ISO, t["off"])
        tex = F.FISTexture(m)
        msgs = get_lines("2254:38031")["messages"]
        ko = {i: msgs[i]["ko"] for i in range(6)}
        # box, bg-type, message index (0..5) -- coords read from decoded atlas
        blocks = [
            {"msg": 0, "box": (0, 52, 0, 240), "bg": "trans", "lines_ko": ko[0]},    # A
            {"msg": 0, "box": (54, 114, 0, 240), "bg": "black", "lines_ko": ko[0]},  # B
            {"msg": 1, "box": (116, 172, 0, 240), "bg": "trans", "lines_ko": ko[1]}, # C
            {"msg": 1, "box": (174, 236, 0, 240), "bg": "black", "lines_ko": ko[1]}, # D
            {"msg": 2, "box": (237, 323, 0, 240), "bg": "trans", "lines_ko": ko[2]}, # E
            {"msg": 3, "box": (324, 412, 0, 240), "bg": "trans", "lines_ko": ko[3]}, # F
            {"msg": 3, "box": (413, 511, 0, 240), "bg": "black", "lines_ko": ko[3]}, # G
            {"msg": 2, "box": (0, 98, 254, 495), "bg": "black", "lines_ko": ko[2]},  # H
            {"msg": 4, "box": (110, 168, 254, 495), "bg": "trans", "lines_ko": ko[4]},  # I
            {"msg": 4, "box": (174, 242, 254, 495), "bg": "black", "lines_ko": ko[4]},  # J
            {"msg": 5, "box": (254, 372, 254, 495), "bg": "trans", "lines_ko": ko[5]},  # K
            {"msg": 5, "box": (382, 511, 254, 495), "bg": "black", "lines_ko": ko[5]},  # L
        ]
        report["2254:38031"] = {"label": "2254:38031 John 512x512 4b",
                                "text_index": 15, "bg_index": "0/1/2(trans)",
                                "black_index": 3, "_old_comp": 49088}
        new_idx, canvas, jinfo = repaint_john(tex, blocks)
        report["2254:38031"]["blocks"] = jinfo
        # per-message glyph height (normal & highlighted copies share one size)
        report["2254:38031"]["orig_glyph_px"] = sorted(
            {b["msg"]: b["glyph_px"] for b in jinfo}.items())
        finalize(tex, new_idx, 2254, 38031, t["next_rel"], report)

    # ---- 47:49/52/55/56 : 24bpp battle-demo attack-label captions ----
    ATTACK = {
        (47, 49): {"off": 491298816, "next_rel": 166368, "comp": 166352,
                   "bbox": (12, 26, 88, 208)},
        (47, 52): {"off": 491771636, "next_rel": 153676, "comp": 153657,
                   "bbox": (11, 26, 108, 182)},
        (47, 55): {"off": 492216812, "next_rel": 158468, "comp": 158449,
                   "bbox": (12, 26, 90, 206)},
        (47, 56): {"off": 492375280, "next_rel": 0, "comp": 169476,
                   "bbox": (12, 26, 100, 184)},
    }
    attack_ko = {(int(e["label"].split()[0].split(":")[0]),
                  int(e["label"].split()[0].split(":")[1])): e
                 for e in TRANS["attack_labels"]}
    for (arch, strm), cfg in ATTACK.items():
        if only not in (None, "attack", str(arch), f"{arch}:{strm}"):
            continue
        m, _ = F.load_member_from_iso(F._ORIG_ISO, cfg["off"])
        tex = F.FISTexture(m)
        ent = attack_ko[(arch, strm)]
        new_rgb, mask, inp, cinfo = repaint_caption(tex, ent["ko"], cfg["bbox"])
        member = tex.encode(new_direct=new_rgb)
        alloc = (cfg["next_rel"] - 16) if cfg["next_rel"] else cfg["comp"]
        report[f"{arch}:{strm}"] = {
            "label": ent["label"], "jp": ent["jp"], "ko": ent["ko"],
            "class": "direct24", "text_color": "white+dark-edge",
            "orig_glyph_px": cinfo["orig_glyph_px"], "caption": cinfo,
            "_old_comp": cfg["comp"]}
        finalize(tex, None, arch, strm, cfg["next_rel"], report,
                 member=member, alloc=alloc)

    # write the full report only on a complete run (all 4 present)
    if only is None:
        json.dump(report, open(OUT / "repaint_report.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("wrote repaint_report.json")
    for k, v in report.items():
        f = v.get("fit", {})
        print(f"  {k:<12} fit={f.get('fits')} new={f.get('new_compressed')} "
              f"alloc={f.get('allocation')} slack={f.get('slack')}")

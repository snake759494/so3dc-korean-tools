#!/usr/bin/env python3
"""
Decode ALL FIS texture resources on Star Ocean 3 DC Disc 1 to viewable RGBA PNGs.

Format summary (reverse-engineered & validated, see decode_notes.md):
  FIS = tri-Ace GS/GIF DMA texture packet.
  - u32 @ 0x20 = CLUT byte size (0 => no CLUT, direct-colour image).
  - CLUT (when present) is 256 or 32 RGBA8888 entries at file offset 0x100,
    stored in PS2 CSM1 order (swap index bits 3<->4) and PS2 alpha (0x80 == opaque).
  - The header is a chain of GIF A+D register writes. We parse BITBLTBUF (0x50 ->
    DPSM/pixel format), TRXREG (0x52 -> transfer RRW x RRH) and TRXDIR (0x53).
  - Pixel data starts at (clut_size + 0x200) for CLUT images, else (last TRXDIR + 0x28).

Five texture classes (DPSM of the image transfer, CLUT present?):
  0x14 PSMT4  + CLUT -> 4bpp indexed, native linear, dims = TRXREG
  0x13 PSMT8  + CLUT -> 8bpp indexed, native linear, dims = TRXREG
  0x00 PSMCT32+ CLUT -> 8bpp indexed DISGUISED as 32bpp -> real dims = 2*TRXREG,
                         pixel bytes are PS2-swizzled (unswizzle8 required)
  0x00 PSMCT32, no CLUT -> 32bpp direct RGBA
  0x01 PSMCT24, no CLUT -> 24bpp direct RGB
  0x02/0x0A PSMCT16(S), no CLUT -> 16bpp direct RGBA5551 (rare)

Pixel data of indexed native + direct images is linear raster order.
"""
from __future__ import annotations
import argparse, struct, csv, json, os, re
from pathlib import Path
import numpy as np
from PIL import Image

WS = Path(os.environ.get("SO3_WS", r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2")) / "work"
# Disc-1 defaults; disc-2: --disc-root work\full_unpack\disc2 --out work\img_ko_d2
DISC = WS / "full_unpack" / "disc1"
MANIFEST = DISC / "manifests" / "stream_manifest.csv"
OUT = WS / "img_ko"

# ---- DPSM constants ----
PSMCT32, PSMCT24, PSMCT16, PSMT8, PSMCT16S, PSMT4 = 0x00, 0x01, 0x02, 0x13, 0x0A, 0x14


def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def ps2_alpha_arr(a):
    return np.minimum(a.astype(np.uint16) * 2, 255).astype(np.uint8)


def csm1(i):
    return (i & ~0x18) | ((i & 0x08) << 1) | ((i & 0x10) >> 1)


_UNSWZ8 = {}


def unswizzle8_map(W, H):
    key = (W, H)
    m = _UNSWZ8.get(key)
    if m is None:
        y = np.arange(H, dtype=np.int64)[:, None]
        x = np.arange(W, dtype=np.int64)[None, :]
        block_loc = (y & ~0xF) * W + (x & ~0xF) * 2
        swap_sel = (((y + 2) >> 2) & 0x1) * 4
        ypos = (((y & ~0x3) >> 1) + (y & 1)) & 0x7
        column_loc = ypos * W * 2 + ((x + swap_sel) & 0x7) * 4
        byte_sel = ((y >> 1) & 1) + ((x >> 2) & 2)
        m = (block_loc + column_loc + byte_sel).ravel()
        _UNSWZ8[key] = m
    return m


def parse_registers(d, limit):
    """Return lists of (offset, data8) for BITBLTBUF/TRXREG/TRXDIR A+D writes."""
    bb, trxreg, trxdir = [], [], []
    i = 0x20
    end = min(limit, len(d) - 8)
    while i < end:
        reg = d[i]
        if reg in (0x50, 0x52, 0x53) and d[i + 1:i + 8] == b"\x00" * 7:
            data = d[i - 8:i]
            if reg == 0x50:
                bb.append((i, data))
            elif reg == 0x52:
                trxreg.append((i, data))
            else:
                trxdir.append(i)
        i += 8
    return bb, trxreg, trxdir


def read_clut(d, n_entries):
    """Read n_entries RGBA8888 from 0x100, apply PS2 alpha; return palette in
    LOGICAL order (CSM1-corrected when >=32 entries)."""
    raw = np.frombuffer(d[0x100:0x100 + n_entries * 4], dtype=np.uint8)
    raw = raw.reshape(-1, 4).copy()
    raw[:, 3] = ps2_alpha_arr(raw[:, 3])
    pal = np.zeros((n_entries, 4), dtype=np.uint8)
    if n_entries >= 32:
        for phys in range(n_entries):
            # phys slot holds the value for logical index L where csm1(L)==phys
            pal[csm1(phys) if csm1(phys) < n_entries else phys] = raw[phys]
        # rebuild properly: logical L -> raw[csm1(L)]
        pal = np.zeros((n_entries, 4), dtype=np.uint8)
        for L in range(n_entries):
            pal[L] = raw[csm1(L)]
    else:
        pal = raw
    return pal


def decode(path):
    d = path.read_bytes()
    info = {"decode_ok": False, "error": ""}
    clut_size = u32(d, 0x20) if len(d) >= 0x24 else 0
    has_clut = clut_size > 0
    scan_limit = (clut_size + 0x200) if has_clut else 0x120
    bb, trxreg, trxdir = parse_registers(d, scan_limit if scan_limit > 0x40 else 0x120)
    if not trxreg:
        # fall back: scan a bit wider
        bb, trxreg, trxdir = parse_registers(d, 0x600)
    if not trxreg:
        info["error"] = "no TRXREG"
        return None, info
    # image transfer = the largest-area TRXREG
    img_off, img_data = max(trxreg, key=lambda t: (u32(t[1], 0) & 0xFFF) * (u32(t[1], 4) & 0xFFF))
    rrw = u32(img_data, 0) & 0xFFF
    rrh = u32(img_data, 4) & 0xFFF
    # DPSM of image = the BITBLTBUF whose offset is just before the image TRXREG
    dpsm = None
    for off, data in bb:
        if off < img_off:
            dpsm = data[7] & 0x3F
    if dpsm is None and bb:
        dpsm = bb[-1][1][7] & 0x3F
    # pixel start
    if has_clut:
        pixel_start = clut_size + 0x200
    else:
        pixel_start = (max(trxdir) + 0x28) if trxdir else 0x100
    body = d[pixel_start:]

    swizzle = "none"
    clut_info = "none"
    try:
        if has_clut and dpsm in (PSMT4,):
            w, h, bpp = rrw, rrh, 4
            need = (w * h + 1) // 2
            if len(body) < need:
                raise ValueError(f"short 4bpp {len(body)}<{need}")
            b = np.frombuffer(body[:need], dtype=np.uint8)
            idx = np.empty(w * h, dtype=np.uint8)
            idx[0::2] = b & 0x0F
            idx[1::2] = (b >> 4)
            idx = idx[:w * h].reshape(h, w)
            # 4bpp uses logical entries 0..15; CSM1 needs up to phys 23, so read
            # at least 24 entries when the stored CLUT is CSM1 (>=32 entries).
            n_read = clut_size // 4
            pal = read_clut(d, n_read if n_read >= 32 else min(16, n_read))
            rgba = pal[:16][idx]
            clut_info = f"{clut_size//4}e/16used CSM1"
        elif has_clut and dpsm in (PSMT8,):
            w, h, bpp = rrw, rrh, 8
            need = w * h
            if len(body) < need:
                raise ValueError(f"short 8bpp {len(body)}<{need}")
            idx = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w)
            pal = read_clut(d, 256)
            rgba = pal[idx]
            clut_info = "256e CSM1"
        elif has_clut and dpsm in (PSMCT32,):
            # 8bpp disguised as 32bpp: real dims 2x, swizzled
            w, h, bpp = rrw * 2, rrh * 2, 8
            need = w * h
            if len(body) < need:
                raise ValueError(f"short swz8 {len(body)}<{need}")
            src = np.frombuffer(body[:need], dtype=np.uint8)
            idx = src[unswizzle8_map(w, h)].reshape(h, w)
            pal = read_clut(d, 256)
            rgba = pal[idx]
            swizzle = "unswizzle8(2x)"
            clut_info = "256e CSM1"
        elif (not has_clut) and dpsm == PSMCT32:
            w, h, bpp = rrw, rrh, 32
            need = w * h * 4
            if len(body) < need:
                raise ValueError(f"short 32 {len(body)}<{need}")
            a = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w, 4).copy()
            a[:, :, 3] = ps2_alpha_arr(a[:, :, 3])
            rgba = a
        elif (not has_clut) and dpsm == PSMCT24:
            w, h, bpp = rrw, rrh, 24
            need = w * h * 3
            if len(body) < need:
                raise ValueError(f"short 24 {len(body)}<{need}")
            rgb = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w, 3)
            rgba = np.dstack([rgb, np.full((h, w), 255, np.uint8)])
        elif (not has_clut) and dpsm in (PSMCT16, PSMCT16S):
            w, h, bpp = rrw, rrh, 16
            need = w * h * 2
            if len(body) < need:
                raise ValueError(f"short 16 {len(body)}<{need}")
            v = np.frombuffer(body[:need], dtype="<u2").reshape(h, w).astype(np.uint32)
            r = ((v & 0x1F) << 3).astype(np.uint8)
            g = (((v >> 5) & 0x1F) << 3).astype(np.uint8)
            bl = (((v >> 10) & 0x1F) << 3).astype(np.uint8)
            al = np.where(((v >> 15) & 1) | (v > 0), 255, 0).astype(np.uint8)
            rgba = np.dstack([r, g, bl, al])
        else:
            raise ValueError(f"unhandled dpsm=0x{dpsm:02x} clut={has_clut}")
    except Exception as e:
        info.update({"error": str(e), "dpsm": dpsm, "rrw": rrw, "rrh": rrh,
                     "has_clut": has_clut, "pixel_start": pixel_start})
        return None, info

    img = Image.fromarray(rgba, "RGBA")
    info.update({"decode_ok": True, "width": int(w), "height": int(h), "bpp": bpp,
                 "dpsm": dpsm, "clut_info": clut_info, "swizzle": swizzle,
                 "pixel_start": pixel_start, "clut_size": clut_size})
    return img, info


def text_heuristic(rgba, bpp, base_name):
    """Return (likely_text, score, coverage)."""
    h, w = rgba.shape[:2]
    a = rgba[:, :, 3].astype(np.float32) / 255.0
    coverage = float(a.mean())
    lum = (0.299 * rgba[:, :, 0] + 0.587 * rgba[:, :, 1] + 0.114 * rgba[:, :, 2])
    lum = lum * a  # premultiply so transparent bg is 0
    dx = np.abs(np.diff(lum, axis=1))
    dy = np.abs(np.diff(lum, axis=0))
    edge = ((dx > 45).sum() + (dy > 45).sum()) / float(w * h)
    # Name-flag only groups that are predominantly text (fonts / title / system).
    # SHI/yam1/kami/asai are mixed (renders + atlases) so rely on the pixel edge
    # heuristic there -- the real SHI UI atlases score edge>0.3 and are caught.
    name_text = base_name in ("John", "ANKF", "strt", "kit")
    px_text = (bpp in (4, 8)) and (edge > 0.05) and (0.008 < coverage < 0.75)
    return bool(name_text or px_text), round(float(edge), 4), round(coverage, 4)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9]", "", s)[:12] or "tex"


def main():
    ap = argparse.ArgumentParser(
        description="Decode all FIS textures of one disc to RGBA PNGs (defaults = disc 1)")
    ap.add_argument("--disc-root", type=Path, default=DISC,
                    help="unpack root containing manifests\\stream_manifest.csv "
                         "and decoded\\ (disc-2: work\\full_unpack\\disc2)")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="stream_manifest.csv (default: <disc-root>\\manifests\\stream_manifest.csv)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output dir for png\\ + texture_catalog.json "
                         "(default: work\\img_ko; disc-2: work\\img_ko_d2)")
    args = ap.parse_args()
    disc = args.disc_root
    manifest = args.manifest or (disc / "manifests" / "stream_manifest.csv")
    out = args.out or OUT
    png_dir = out / "png"
    png_dir.mkdir(parents=True, exist_ok=True)

    rows = [r for r in csv.DictReader(open(manifest, encoding="utf-8"))
            if r["magic_text"] == "FIS"]
    print(f"FIS streams: {len(rows)}")
    catalog = []
    ok = fail = 0
    fail_log = []
    for n, r in enumerate(rows):
        path = disc / r["path"]
        arch, strm = int(r["archive_id"]), int(r["stream_id"])
        base = safe_name(r["fis_name"][:4])
        img, info = decode(path)
        entry = {
            "archive_id": arch, "stream_id": strm, "name": r["fis_name"][:8],
            "base_name": base,
            "iso_offset": int(r["iso_offset"]), "source_offset": int(r["source_offset"]),
            "mode": int(r["mode"]), "compressed": int(r["compressed"]),
            "unpacked": int(r["unpacked"]), "next_rel": int(r["next_rel"] or 0),
            "path": r["path"],
        }
        # repack headroom: bytes between this member start and next member,
        # minus 16-byte SLZ header and current compressed payload
        nr = int(r["next_rel"] or 0)
        entry["allocation_hint"] = (nr - 16 - int(r["compressed"])) if nr else None
        if img is None:
            entry.update({"width": info.get("rrw"), "height": info.get("rrh"),
                          "bpp": None, "clut_info": None, "swizzle": None,
                          "png_path": None, "sheet": None, "decode_ok": False,
                          "likely_text": False, "edge_score": None, "coverage": None,
                          "error": info.get("error")})
            fail += 1
            fail_log.append(f"{arch}:{strm} {r['fis_name'][:8]!r} {info.get('error')}")
        else:
            fname = f"a{arch:04d}_s{strm:05d}_{base}.png"
            img.save(png_dir / fname)
            rgba = np.asarray(img)
            lt, score, cov = text_heuristic(rgba, info["bpp"], base)
            entry.update({"width": info["width"], "height": info["height"],
                          "bpp": info["bpp"], "clut_info": info["clut_info"],
                          "swizzle": info["swizzle"], "png_path": f"png/{fname}",
                          "sheet": None, "decode_ok": True, "likely_text": lt,
                          "edge_score": score, "coverage": cov, "error": ""})
            ok += 1
        catalog.append(entry)
        if (n + 1) % 200 == 0:
            print(f"  {n+1}/{len(rows)}  ok={ok} fail={fail}")
    json.dump(catalog, open(out / "texture_catalog.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print(f"DONE decode: ok={ok} fail={fail} -> {out/'texture_catalog.json'}")
    if fail_log:
        (out / "decode_failures.txt").write_text("\n".join(fail_log), encoding="utf-8")
        print("failures:", *fail_log[:20], sep="\n  ")


if __name__ == "__main__":
    main()

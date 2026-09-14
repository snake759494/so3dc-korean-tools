#!/usr/bin/env python3
"""FIS texture repaint pipeline for the SO3 DC Disc 1 Korean IMAGE patch.

This is a LIBRARY + CLI to:
  1. Decode a FIS member (from the ISO SLZ stream / decoded file) into
     (indices, CLUT, header) with the exact pixel class + swizzle, and
     RE-ENCODE it byte-identically (the round-trip foundation).
  2. Repaint rendered Japanese text with Korean (NanumSquare Neo), matching
     the original glyph size / baseline / position, mapping AA coverage onto
     the texture's own palette ramp, editing ONLY the repainted region.
  3. Re-encode the FIS member, SLZ-recompress (optimal, mandatory decompress
     round-trip), and confirm it FITS the member's allocation.

Format reference: work/img_ko/decode_notes.md.  Only the pixel body is ever
rewritten; the FIS header + CLUT bytes are preserved verbatim.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(r"D:\ps2\NanumSquareNeo-cBd.ttf")

# ---- wiring to the shipped SLZ tools ----
_ROOT = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2")
sys.path.insert(0, str(_ROOT / "publish" / "so3dc-korean-tools"))
sys.path.insert(0, str(_ROOT / "work" / "full_ko"))
from so3_repack import read_slz_member, decompress_slz_payload  # noqa: E402
from slz_optimal import compress_slz_mode2_optimal  # noqa: E402

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
    """m such that unswizzled_linear[k] = raw_body[m[k]] (a permutation)."""
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


_SWZ8_INV = {}


def swizzle8_inv(W, H):
    """inv such that raw_body[inv] = linear.ravel();  raw_body = scatter."""
    key = (W, H)
    inv = _SWZ8_INV.get(key)
    if inv is None:
        m = unswizzle8_map(W, H)
        inv = np.empty_like(m)
        inv[m] = np.arange(m.size, dtype=m.dtype)
        _SWZ8_INV[key] = inv
    return inv


def parse_registers(d, limit):
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


def read_clut_logical(d, n_entries):
    """RGBA8888 from 0x100, PS2 alpha, CSM1-corrected (>=32 entries)."""
    raw = np.frombuffer(d[0x100:0x100 + n_entries * 4], dtype=np.uint8)
    raw = raw.reshape(-1, 4).copy()
    raw[:, 3] = ps2_alpha_arr(raw[:, 3])
    if n_entries >= 32:
        pal = np.zeros((n_entries, 4), dtype=np.uint8)
        for L in range(n_entries):
            pal[L] = raw[csm1(L)]
    else:
        pal = raw
    return pal


class FISTexture:
    """Decoded FIS member with a byte-exact re-encode path."""

    def __init__(self, raw: bytes):
        self.raw = raw
        d = raw
        self.clut_size = u32(d, 0x20) if len(d) >= 0x24 else 0
        self.has_clut = self.clut_size > 0
        scan_limit = (self.clut_size + 0x200) if self.has_clut else 0x120
        bb, trxreg, trxdir = parse_registers(d, scan_limit if scan_limit > 0x40 else 0x120)
        if not trxreg:
            bb, trxreg, trxdir = parse_registers(d, 0x600)
        if not trxreg:
            raise ValueError("no TRXREG")
        img_off, img_data = max(
            trxreg, key=lambda t: (u32(t[1], 0) & 0xFFF) * (u32(t[1], 4) & 0xFFF))
        self.rrw = u32(img_data, 0) & 0xFFF
        self.rrh = u32(img_data, 4) & 0xFFF
        dpsm = None
        for off, data in bb:
            if off < img_off:
                dpsm = data[7] & 0x3F
        if dpsm is None and bb:
            dpsm = bb[-1][1][7] & 0x3F
        self.dpsm = dpsm
        if self.has_clut:
            self.pixel_start = self.clut_size + 0x200
        else:
            self.pixel_start = (max(trxdir) + 0x28) if trxdir else 0x100
        self.prefix = d[:self.pixel_start]
        self.body = d[self.pixel_start:]

        self.idx = None       # (h,w) uint8 index array (indexed classes)
        self.pal = None       # (n,4) logical RGBA
        self.rgba = None      # (h,w,4) decoded preview
        self.pixel_class = None
        self.trailing = b""   # body bytes after the pixel payload (usually empty)
        self._decode()

    def _decode(self):
        d = self.raw
        body = self.body
        if self.has_clut and self.dpsm == PSMT4:
            w, h, bpp = self.rrw, self.rrh, 4
            need = (w * h + 1) // 2
            b = np.frombuffer(body[:need], dtype=np.uint8)
            idx = np.empty(w * h, dtype=np.uint8)
            idx[0::2] = b & 0x0F
            idx[1::2] = (b >> 4)
            self.idx = idx[:w * h].reshape(h, w)
            n_read = self.clut_size // 4
            self.pal = read_clut_logical(d, n_read if n_read >= 32 else min(16, n_read))
            self.rgba = self.pal[:16][self.idx]
            self.pixel_class = "pt4"
            self.trailing = body[need:]
        elif self.has_clut and self.dpsm == PSMT8:
            w, h, bpp = self.rrw, self.rrh, 8
            need = w * h
            self.idx = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w).copy()
            self.pal = read_clut_logical(d, 256)
            self.rgba = self.pal[self.idx]
            self.pixel_class = "pt8"
            self.trailing = body[need:]
        elif self.has_clut and self.dpsm == PSMCT32:
            w, h, bpp = self.rrw * 2, self.rrh * 2, 8
            need = w * h
            src = np.frombuffer(body[:need], dtype=np.uint8)
            self.idx = src[unswizzle8_map(w, h)].reshape(h, w).copy()
            self.pal = read_clut_logical(d, 256)
            self.rgba = self.pal[self.idx]
            self.pixel_class = "swz8"
            self.trailing = body[need:]
        elif (not self.has_clut) and self.dpsm == PSMCT32:
            w, h, bpp = self.rrw, self.rrh, 32
            need = w * h * 4
            a = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w, 4).copy()
            self._direct_raw = a.copy()
            a[:, :, 3] = ps2_alpha_arr(a[:, :, 3])
            self.rgba = a
            self.pixel_class = "direct32"
            self.trailing = body[need:]
        elif (not self.has_clut) and self.dpsm == PSMCT24:
            w, h, bpp = self.rrw, self.rrh, 24
            need = w * h * 3
            rgb = np.frombuffer(body[:need], dtype=np.uint8).reshape(h, w, 3).copy()
            self._direct_raw = rgb.copy()
            self.rgba = np.dstack([rgb, np.full((h, w), 255, np.uint8)])
            self.pixel_class = "direct24"
            self.trailing = body[need:]
        else:
            raise ValueError(f"unhandled dpsm=0x{self.dpsm:02x} clut={self.has_clut}")
        self.w, self.h, self.bpp = int(w), int(h), int(bpp)

    # ---- re-encode ----
    def encode(self, new_idx=None, new_direct=None) -> bytes:
        """Rebuild the FIS member bytes. new_idx (h,w) for indexed classes,
        new_direct (h,w,ch) raw bytes for direct classes.  None => unchanged."""
        pc = self.pixel_class
        if pc in ("pt4", "pt8", "swz8"):
            idx = self.idx if new_idx is None else new_idx
            idx = np.ascontiguousarray(idx, dtype=np.uint8)
            if idx.shape != (self.h, self.w):
                raise ValueError(f"index shape {idx.shape} != {(self.h, self.w)}")
            if pc == "pt8":
                body = idx.tobytes()
            elif pc == "pt4":
                flat = idx.ravel()
                packed = np.empty((flat.size + 1) // 2, dtype=np.uint8)
                packed[:] = (flat[0::2] & 0x0F) | (flat[1::2] << 4)
                body = packed.tobytes()
            else:  # swz8: decode is idx_flat[k]=src[m[k]] -> invert by scatter
                m = unswizzle8_map(self.w, self.h)
                raw_body = np.empty(self.w * self.h, dtype=np.uint8)
                raw_body[m] = idx.ravel()
                body = raw_body.tobytes()
        elif pc in ("direct32", "direct24"):
            arr = self._direct_raw if new_direct is None else new_direct
            body = np.ascontiguousarray(arr, dtype=np.uint8).tobytes()
        else:
            raise ValueError(f"encode unsupported for class {pc}")
        return self.prefix + body + self.trailing

    def roundtrip_ok(self) -> bool:
        return self.encode() == self.raw


# ---------------------------------------------------------------------------
# member loading
# ---------------------------------------------------------------------------

def load_member_from_iso(iso: Path, iso_offset: int):
    decoded, slz = read_slz_member(iso, iso_offset)
    return decoded, slz


def recompress_and_check(member_bytes: bytes, payload_space: int):
    """Compress optimal, verify decompress round-trip, report fit."""
    comp = compress_slz_mode2_optimal(member_bytes)
    if decompress_slz_payload(comp, 2, len(member_bytes)) != member_bytes:
        raise AssertionError("SLZ optimal round-trip FAILED")
    return {
        "new_compressed": len(comp),
        "payload_space": payload_space,
        "fits": len(comp) <= payload_space,
        "slack": payload_space - len(comp),
    }, comp


# ---------------------------------------------------------------------------
# text rendering + palette quantization (repaint core)
# ---------------------------------------------------------------------------

def lum_of(pal):
    return 0.299 * pal[:, 0] + 0.587 * pal[:, 1] + 0.114 * pal[:, 2]


def _premul(a):
    a = a.astype(np.int32)
    af = a[..., 3:4]
    rgb = a[..., :3] * af // 255
    return np.concatenate([rgb, af], axis=-1)


def quantize_region(tile_rgba, pal, cand_indices, alpha_weight=1.0):
    """Map each pixel of tile_rgba (H,W,4) to the nearest candidate palette
    index by premultiplied-RGBA distance.  Returns (H,W) uint8 index array."""
    cand = np.asarray(cand_indices, dtype=np.int64)
    tp = _premul(tile_rgba).reshape(-1, 4).astype(np.int32)
    pp = _premul(pal[cand]).astype(np.int32)
    # weight alpha channel so transparency is respected
    w = np.array([1, 1, 1, alpha_weight], dtype=np.float32)
    diff = (tp[:, None, :].astype(np.float32) - pp[None, :, :].astype(np.float32)) * w
    d = (diff * diff).sum(-1)
    nn = d.argmin(1)
    return cand[nn].reshape(tile_rgba.shape[:2]).astype(np.uint8)


def _ink_bbox(gray, thr=8):
    ys, xs = np.where(gray > thr)
    if len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def render_line_to_height(text, target_h, ink_rgba, outline_rgba,
                          stroke_w=1, font_path=FONT_PATH, pad=6,
                          size_axis="body"):
    """Render `text` (NanumSquare Neo) as an RGBA tile whose ink (glyph body)
    height matches target_h.  White fill + dark outline, anti-aliased.

    Returns dict: tile (H,W,4 uint8), ink_bbox (x0,y0,x1,y1) within tile of the
    *stroked* glyph extent, and body_bbox of the fill-only ink (for baseline).
    """
    fp = str(font_path)
    # iteratively solve font pixel size so the fill ink height == target_h
    size = float(target_h) + 4.0
    for _ in range(6):
        font = ImageFont.truetype(fp, max(6, int(round(size))))
        img = Image.new("L", (max(16, len(text) * target_h * 2 + 40),
                              target_h * 4 + 40), 0)
        dr = ImageDraw.Draw(img)
        dr.text((20, 20), text, font=font, fill=255)
        bb = _ink_bbox(np.asarray(img))
        if bb is None:
            size += 2
            continue
        h = bb[3] - bb[1]
        if h == 0:
            break
        if abs(h - target_h) <= 0:
            break
        size *= target_h / h
    fs = max(6, int(round(size)))
    font = ImageFont.truetype(fp, fs)
    # render at scale to get clean AA, on transparent, fill white + dark stroke
    W = max(16, int(len(text) * (fs) * 1.4) + 40)
    H = fs * 3 + 40
    rgba = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(rgba)
    ink = tuple(int(v) for v in ink_rgba)
    outl = tuple(int(v) for v in outline_rgba)
    if stroke_w > 0:
        dr.text((20, 20), text, font=font, fill=ink,
                stroke_width=stroke_w, stroke_fill=outl)
    else:
        dr.text((20, 20), text, font=font, fill=ink)
    arr = np.asarray(rgba).copy()
    # body bbox = white-fill ink only (for baseline/height ref)
    body_img = Image.new("L", (W, H), 0)
    dbody = ImageDraw.Draw(body_img)
    dbody.text((20, 20), text, font=font, fill=255)
    body_bb = _ink_bbox(np.asarray(body_img))
    # stroked bbox = anything with alpha
    alpha = arr[:, :, 3]
    strk_bb = _ink_bbox(alpha, thr=8)
    if strk_bb is None or body_bb is None:
        raise ValueError(f"empty render for {text!r}")
    x0 = min(strk_bb[0], body_bb[0])
    y0 = min(strk_bb[1], body_bb[1])
    x1 = max(strk_bb[2], body_bb[2])
    y1 = max(strk_bb[3], body_bb[3])
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(W, x1 + pad); y1 = min(H, y1 + pad)
    tile = arr[y0:y1, x0:x1]
    return {
        "tile": tile,
        "font_size": fs,
        # body bbox relative to tile origin
        "body_bbox": (body_bb[0] - x0, body_bb[1] - y0,
                      body_bb[2] - x0, body_bb[3] - y0),
        "strk_bbox": (strk_bb[0] - x0, strk_bb[1] - y0,
                      strk_bb[2] - x0, strk_bb[3] - y0),
    }


def paste_tile_over(dst_rgba, tile, dx, dy):
    """Alpha-composite `tile` (h,w,4) onto dst_rgba at (dx,dy). Returns new array
    plus the destination bbox actually written."""
    H, W = dst_rgba.shape[:2]
    th, tw = tile.shape[:2]
    x0 = max(0, dx); y0 = max(0, dy)
    x1 = min(W, dx + tw); y1 = min(H, dy + th)
    if x1 <= x0 or y1 <= y0:
        return dst_rgba, None
    sub = tile[y0 - dy:y1 - dy, x0 - dx:x1 - dx].astype(np.float32)
    a = sub[:, :, 3:4] / 255.0
    base = dst_rgba[y0:y1, x0:x1].astype(np.float32)
    ba = base[:, :, 3:4] / 255.0
    out_a = a + ba * (1 - a)                       # straight-alpha "over"
    num = sub[:, :, :3] * a + base[:, :, :3] * ba * (1 - a)
    out_rgb = np.divide(num, out_a, out=np.zeros_like(num), where=out_a > 0)
    dst_rgba[y0:y1, x0:x1, :3] = np.clip(out_rgb + 0.5, 0, 255).astype(np.uint8)
    dst_rgba[y0:y1, x0:x1, 3] = np.clip(out_a[:, :, 0] * 255 + 0.5, 0, 255).astype(np.uint8)
    return dst_rgba, (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# 24bpp direct-color caption repaint (inpaint + centred Korean)
# ---------------------------------------------------------------------------
from PIL import ImageFilter  # noqa: E402


def caption_mask(rgb, bbox, med_size=7, thr=95, dilate=1):
    """Mask the caption glyphs (white text + dark edge) inside `bbox` by their
    difference from a local-median background estimate.  Robust against the
    photographic scene (smooth sky/terrain has small median-diff)."""
    im = Image.fromarray(rgb, "RGB")
    med = np.asarray(im.filter(ImageFilter.MedianFilter(med_size))).astype(np.int32)
    diff = np.abs(rgb.astype(np.int32) - med).sum(axis=2)
    y0, y1, x0, x1 = bbox
    region = np.zeros(rgb.shape[:2], bool)
    region[y0:y1, x0:x1] = True
    mask = (diff > thr) & region
    if dilate:
        mim = Image.fromarray((mask * 255).astype(np.uint8)).filter(
            ImageFilter.MaxFilter(2 * dilate + 1))
        mask = (np.asarray(mim) > 0) & region
    return mask


def inpaint_harmonic(rgb, mask, iters=250):
    """Fill masked pixels by harmonic (Laplace) diffusion from surrounding
    unmasked pixels -- a smooth plausible background for a thin caption band."""
    out = rgb.astype(np.float32).copy()
    m = mask
    for _ in range(iters):
        acc = np.zeros_like(out)
        div = np.zeros(out.shape[:2], np.float32)
        acc[1:] += out[:-1]; div[1:] += 1
        acc[:-1] += out[1:]; div[:-1] += 1
        acc[:, 1:] += out[:, :-1]; div[:, 1:] += 1
        acc[:, :-1] += out[:, 1:]; div[:, :-1] += 1
        avg = acc / np.maximum(div, 1)[..., None]
        out[m] = avg[m]
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# self-test: prove the FIS round-trip on the 4 targets
# ---------------------------------------------------------------------------

_TARGETS = [
    (40, 25, 489682104, 31372),
    (42, 36, 491065344, 28112),
    (44, 41, 491149312, 54112),
    (2254, 38031, 2738751360, 49104),
    (2254, 38032, 2738800464, None),   # neighbor (English copy) - inspect only
]

_ORIG_ISO = Path("D:/ps2/Star Ocean Till the End of Time Director's Cut (Disc 1).iso")


def _selftest():
    for arch, strm, iso_off, next_rel in _TARGETS:
        member, slz = load_member_from_iso(_ORIG_ISO, iso_off)
        tex = FISTexture(member)
        rt = tex.roundtrip_ok()
        line = (f"{arch}:{strm} class={tex.pixel_class} {tex.w}x{tex.h} {tex.bpp}bpp "
                f"pixel_start=0x{tex.pixel_start:X} clut={tex.clut_size} "
                f"FIS_roundtrip={rt}")
        if next_rel:
            info, _ = recompress_and_check(member, next_rel - 16)
            line += f"  optimal={info['new_compressed']} space={info['payload_space']} slack={info['slack']}"
        print(line)
        assert rt, f"FIS round-trip FAILED for {arch}:{strm}"
    print("ALL FIS ROUND-TRIPS OK")


if __name__ == "__main__":
    _selftest()

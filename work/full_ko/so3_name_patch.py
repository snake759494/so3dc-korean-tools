#!/usr/bin/env python3
"""SO3 DC hardcoded character-name table Korean patch (task D11-2).

Makes DYNAMIC name references (93 80 <id> resolver, A1 80 party-slot, and the
status/menu screens drawn by engine UI code) render Korean instead of
half-width katakana.

Primary plan (kana-slot repurpose, no code patch):
  * The engine resolves ids 1..10 to half-width-katakana NUL-terminated
    strings stored in three table copies, then converts each byte with the
    runtime converter at VA 0x465550 into a GLOBAL 1.72 atlas glyph code
    (kana byte c -> code c - 0x13) and draws from the global atlas.
  * We re-spell every name with 21 distinct plain kana bytes (one byte per
    Hangul syllable) and replace those 21 global-atlas kana slots with
    Nanum-rendered Hangul syllable bitmaps.  The converter itself is not
    modified.  The chosen codes collide with nothing the Korean re-encoding
    uses (GLOBAL_CODE_MAP: 1..65 and listed punctuation; 157 'FW dash' also
    excluded), and none of the chosen bytes participates in dakuten
    composition because 0xDE/0xDF never follow them.

Patched extents (all fail-closed, allocation-checked, byte-verified):
  * archive 8 global font member (so3mclib 1.72, ISO 3,788,176): 21 glyph
    bitmaps + their advance bytes; recompressed SLZ mode-2 optimal.
  * archive 2 / 0002.sle member 1 (font/text renderer module): the 80-byte
    reverse-order name table at module offset 0x11BC28 (VA 0x5038A8) and the
    two record-initializer strcpy literals at 0x11FA98/0x11FAA0
    (VA 0x507718 Sophia / 0x507720 Fayt; both are copy-only sources into
    record+0x20, confirmed by disassembly).  Rebuilt as SLZ16 mode 3 with a
    token-minimal encoder, re-encrypted with the SLE stream cipher.
  * archive 68 stream 61 (MWo3- input module): forward table at 0x9080.
  * archive 1069 stream 1679 (MWo3 field/scenario module): reverse pool at
    0x153718; the id9 dev-leftover (izaaku) is replaced with the proper
    Adray spelling.

Fallback plan (--fallback-latin): Latin names through the existing A-Z/a-z
codes 14..65 - table rewrite only, the global font is left untouched.

Designed to run AFTER the main message patch on the intermediate ISO: it
touches only the four extents above and verifies the pre-patch state of each
byte-exactly before writing anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
_PUBLISH = WS / "publish" / "so3dc-korean-tools"
if not (_PUBLISH / "so3_repack.py").exists():
    _PUBLISH = WS  # repo checkout: the clone root IS the workspace
for _p in (str(_PUBLISH), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from so3_repack import (  # noqa: E402
    ENTRY_COUNT,
    SECTOR,
    decode_index,
    decompress_slz_payload,
)
from slz_optimal import compress_slz_mode2_optimal  # noqa: E402

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is present in the build env
    _np = None


# ---------------------------------------------------------------------------
# constants: names, kana byte assignment, converter facts
# ---------------------------------------------------------------------------

DEFAULT_FONT = Path(os.environ.get("SO3_FONT", r"D:\ps2\NanumSquareNeo-cBd.ttf"))
RENDER_PX = 22
GRAY_LEVELS = 2
GLYPH_CELL = 24
GLYPH_BYTES = 288  # 24x24 4bpp

HANGUL_NAMES = {
    1: "페이트", 2: "소피아", 3: "마리아", 4: "클리프", 5: "넬",
    6: "알벨", 7: "로저", 8: "스프레", 9: "아드레이", 10: "미라쥬",
}

LATIN_NAMES = {
    1: "Fayt", 2: "Sophia", 3: "Maria", 4: "Cliff", 5: "Nel",
    6: "Albel", 7: "Roger", 8: "Peppita", 9: "Adray", 10: "Mirage",
}

# syllable -> half-width kana byte (JIS X 0201).  code = byte - 0x13,
# atlas slot = code - 1.  Chosen loosely phonetically; all plain kana,
# no 0xB0, no 0xDE/0xDF, all distinct.
SYLLABLE_KANA = {
    "페": 0xCD, "이": 0xB2, "트": 0xC4, "소": 0xBF, "피": 0xCB,
    "아": 0xB1, "마": 0xCF, "리": 0xD8, "클": 0xB8, "넬": 0xC8,
    "알": 0xA7, "벨": 0xD2, "로": 0xDB, "저": 0xBC, "스": 0xBD,
    "프": 0xCC, "레": 0xDA, "드": 0xC2, "미": 0xD0, "라": 0xD7,
    "쥬": 0xAD,
}

# global codes the Korean re-encoding already uses (patch_hyda_dialogue
# GLOBAL_CODE_MAP: digits/'-','.',"'" = 1..13, A-Z 14..39, a-z 40..65 and
# the audited punctuation slots) plus 157 (the long-vowel dash).
FORBIDDEN_GLOBAL_CODES = frozenset(range(1, 66)) | {
    232, 233, 235, 237, 239, 241, 243, 258, 259, 263, 264,
    272, 273, 278, 283, 284, 285, 157,
}

GLOBAL_GLYPH_COUNT = 292

SLE_KEY = bytes.fromhex("66665442B379F0C7E7D51E4B7BA41C7D")

# ---------------------------------------------------------------------------
# constants: patched extents (Disc 1 facts, all runtime-cross-checked)
# ---------------------------------------------------------------------------

SHA_FONT = "8F91FE6C630BF7890E2934D3B302911188C52DDE5732AA70E3ED40EEA325A3BC"
SHA_0068 = "DFFD07CBC420CD2FCD5593082E9632C930DD6EB9183D6A100C3DE3DE6FC4DF68"
SHA_1069 = "70D4FA07ABF8C7CEAD5F6CEF829AC86DC0D894D12A6CB5214C79B900B1A6A671"
SHA_SLE = "C0BE5D30CD5A28C7E31B399C1C867CB463E157BE9A06270871E5707857CBFD00"
SHA_SLE_M0 = "BA6F68051F89C5B918624C1CD1E743A0E6698148427C4A9EDAA9F45321555451"
SHA_SLE_M1 = "E10283983CB167AE0E430320360401A5971CACEE7154BD512A9A967206114271"

REF_FONT = WS / r"work\full_unpack\disc1\decoded\0008\s000014_d0_o00008590.bin"
REF_0068 = WS / r"work\full_unpack\disc1\decoded\0068\s000061_d0_o00000000.bin"
REF_1069 = WS / r"work\full_unpack\disc1\decoded\1069\s001679_d0_o00000000.bin"
REF_SLE = WS / r"work\full_unpack\disc1\raw\0002.sle"

FONT_MEMBER = {
    "name": "font_global_1_72", "archive_id": 8, "iso_offset": 3788176,
    "mode": 2, "compressed": 24496, "unpacked": 84608,
    # next level-0 SLZ stream of archive 8 starts at 0x3A2D90
    "boundary": 3812752, "sha": SHA_FONT, "ref": REF_FONT,
}
S0068_MEMBER = {
    "name": "s000061_archive_68", "archive_id": 68, "iso_offset": 493129728,
    "mode": 1, "compressed": 17490, "unpacked": 37248,
    "boundary": 493148160, "sha": SHA_0068, "ref": REF_0068,  # archive end
}
S1069_MEMBER = {
    "name": "s001679_archive_1069", "archive_id": 1069, "iso_offset": 1059936256,
    "mode": 3, "compressed": 667686, "unpacked": 1449344,
    "boundary": 1060605952, "sha": SHA_1069, "ref": REF_1069,  # archive end
}
SLE_EXTENT = {
    "name": "0002_sle", "archive_id": 2, "iso_offset": 2355200, "size": 645120,
    "sha": SHA_SLE, "ref": REF_SLE,
    "member1_offset": 73156, "member1_mode": 3,
    "member1_compressed": 571646, "member1_unpacked": 1186048,
}

# module-image offsets of the tables
SLE_TABLE_OFFSET = 0x11BC28      # VA 0x5038A8, reverse order (id10 first)
SLE_LITERALS = {2: 0x11FA98, 1: 0x11FAA0}  # VA 0x507718 / 0x507720, 8B slots
T0068_OFFSET = 0x9080            # forward order (id1 first)
T1069_OFFSET = 0x153718          # VA 0x336218, reverse order (id10 first)

SLOT_BYTES = 8  # 7 name bytes + NUL

# per-disc extent override (disc-2 port).  Only archive POSITIONS may move;
# every content fact (mode/sizes/SHAs/table offsets/key/allocations) is
# asserted unchanged against the config, fail-closed.  Activated via
# --config or the SO3_NAME_PATCH_CONFIG env var (default: disc-1 constants).
_DISC_CONFIG: str | None = None


def apply_disc_config(config_path: Path) -> dict:
    global _DISC_CONFIG, REF_FONT, REF_0068, REF_1069, REF_SLE
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))

    sle = cfg["sle"]
    if (int(sle["iso_offset"]), int(sle["size"])) != (
            SLE_EXTENT["iso_offset"], SLE_EXTENT["size"]):
        raise NamePatchError("config: 0002.sle extent moved; port assumptions broken")
    if sle["sha256"].upper() != SHA_SLE:
        raise NamePatchError("config: 0002.sle SHA differs from the disc-1 constant")
    if sle.get("sle_key_hex", SLE_KEY.hex()).lower() != SLE_KEY.hex().lower():
        raise NamePatchError("config: SLE key differs")

    font = cfg["global_font"]
    if (int(font["iso_offset"]), int(font["boundary"])) != (
            FONT_MEMBER["iso_offset"], FONT_MEMBER["boundary"]):
        raise NamePatchError("config: global font extent moved; port assumptions broken")
    if font["decoded_sha256"].upper() != SHA_FONT:
        raise NamePatchError("config: global font SHA differs from the disc-1 constant")

    copies = {int(c["archive"]): c for c in cfg["secondary_copies"]}
    overrides = {}
    for member, aid, sha_const in ((S0068_MEMBER, 68, SHA_0068),
                                   (S1069_MEMBER, 1069, SHA_1069)):
        c = copies[aid]
        if c["decoded_sha256"].upper() != sha_const:
            raise NamePatchError(f"config: archive {aid} decoded SHA differs")
        if (int(c["mode"]), int(c["compressed"]), int(c["unpacked"])) != (
                member["mode"], member["compressed"], member["unpacked"]):
            raise NamePatchError(f"config: archive {aid} member header facts differ")
        member["iso_offset"] = int(c["iso_offset"])
        member["boundary"] = int(c["boundary"])
        overrides[aid] = (member["iso_offset"], member["boundary"])

    # reference paths: switch to the config disc's unpack tree (SHA-gated by
    # _load_reference, so a wrong path can only fail, never mis-verify).
    ref_updates = {}
    if font.get("decoded_path"):
        REF_FONT = WS / font["decoded_path"]
        FONT_MEMBER["ref"] = REF_FONT
        ref_updates["font"] = str(REF_FONT)
        parts = Path(font["decoded_path"]).parts
        if "decoded" in parts:
            disc_root = WS.joinpath(*parts[: parts.index("decoded")])
            sle_raw = disc_root / "raw" / "0002.sle"
            if sle_raw.exists():
                REF_SLE = sle_raw
                SLE_EXTENT["ref"] = REF_SLE
                ref_updates["sle"] = str(REF_SLE)
    if copies[68].get("decoded_path"):
        REF_0068 = WS / copies[68]["decoded_path"]
        S0068_MEMBER["ref"] = REF_0068
        ref_updates["0068"] = str(REF_0068)
    if copies[1069].get("decoded_path"):
        REF_1069 = WS / copies[1069]["decoded_path"]
        S1069_MEMBER["ref"] = REF_1069
        ref_updates["1069"] = str(REF_1069)

    _DISC_CONFIG = str(config_path)
    return {"config": _DISC_CONFIG, "position_overrides": overrides,
            "reference_paths": ref_updates}

# original half-width katakana rows, id1..id10 (0002.sle / 0068 copies)
_ORIG_ROWS = {
    1: bytes.fromhex("ccaab2c4"),      # fueito
    2: bytes.fromhex("bfcca8b1"),      # sofia
    3: bytes.fromhex("cfd8b1"),        # maria
    4: bytes.fromhex("b8d8cc"),        # kurifu
    5: bytes.fromhex("c8d9"),          # neru
    6: bytes.fromhex("b1d9cdded9"),    # aruberu
    7: bytes.fromhex("dbbcdeacb0"),    # rojaa
    8: bytes.fromhex("bdccda"),        # sufure
    9: bytes.fromhex("b1c4dedab0"),    # adoree
    10: bytes.fromhex("d0d7b0bcdead"),  # miraaju
}
# archive 1069 pool carries a dev leftover in the id9 slot
_ORIG_ROWS_1069 = dict(_ORIG_ROWS)
_ORIG_ROWS_1069[9] = bytes.fromhex("b2bbdeb0b8")  # izaaku


class NamePatchError(RuntimeError):
    pass


# honored at import so that delegating tools (verify_full_iso --name-patch)
# see the per-disc extents without their own plumbing; unset = disc-1 defaults.
_ENV_CONFIG = os.environ.get("SO3_NAME_PATCH_CONFIG")
if _ENV_CONFIG:
    apply_disc_config(Path(_ENV_CONFIG))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


# ---------------------------------------------------------------------------
# converter simulation (VA 0x465550 rules, name_resource_findings.md section 3)
# ---------------------------------------------------------------------------

def simulate_converter(name_bytes: bytes) -> list[int]:
    """Half-width byte string -> global glyph codes, mirroring VA 0x465550."""
    codes: list[int] = []
    hiragana = False
    i = 0
    while i < len(name_bytes):
        c = name_bytes[i]
        if c == 0:
            break
        if 0x30 <= c <= 0x39:
            codes.append(c - 0x30 + 1)
        elif 0x41 <= c <= 0x5A:
            codes.append(c - 0x41 + 14)
        elif 0x61 <= c <= 0x7A:
            codes.append(c - 0x61 + 40)
        elif c == 0xB0:
            codes.append(157)
        elif c == 0x7E:
            hiragana = not hiragana
        elif 0xA6 <= c <= 0xDD:
            peek = name_bytes[i + 1] if i + 1 < len(name_bytes) else 0
            if peek == 0xDE and (0xB6 <= c <= 0xC4 or 0xCA <= c <= 0xCE or c == 0xB3):
                if 0xB6 <= c <= 0xC4:
                    code = c + 0x16
                elif 0xCA <= c <= 0xCE:
                    code = c + 0x11
                else:
                    code = 203  # u + dakuten -> vu
                codes.append(code - 0x64 if hiragana else code)
                i += 2
                continue
            if peek == 0xDF and 0xCA <= c <= 0xCE:
                codes.append((c + 0x16) - (0x64 if hiragana else 0))
                i += 2
                continue
            base = c - 0x13
            codes.append(base - 0x64 if hiragana else base)
        else:
            codes.append(0xF1)
        i += 1
    return codes


def replaced_slot_map() -> dict[int, str]:
    """glyph code -> Hangul syllable for the 21 repurposed kana slots."""
    return {kana - 0x13: syl for syl, kana in SYLLABLE_KANA.items()}


def code_to_char(code: int, replaced: dict[int, str]) -> str:
    if code in replaced:
        return replaced[code]
    if 1 <= code <= 10:
        return chr(ord("0") + code - 1)
    if 14 <= code <= 39:
        return chr(ord("A") + code - 14)
    if 40 <= code <= 65:
        return chr(ord("a") + code - 40)
    if code == 157:
        return "ー"
    if 147 <= code <= 202:
        return bytes((code + 0x13,)).decode("cp932")  # half-width kana
    return f"⟦{code}⟧"


def validate_code_constraints() -> dict[str, object]:
    """Prove the 21 chosen kana bytes/codes obey every constraint."""
    bad: list[str] = []
    bytes_used = list(SYLLABLE_KANA.values())
    if len(set(bytes_used)) != len(bytes_used):
        bad.append("kana bytes are not distinct")
    codes = [b - 0x13 for b in bytes_used]
    if len(set(codes)) != len(codes):
        bad.append("codes are not distinct")
    for syl, b in SYLLABLE_KANA.items():
        code = b - 0x13
        if not 0xA6 <= b <= 0xDD or b == 0xB0:
            bad.append(f"{syl}: byte 0x{b:02X} is not a plain kana byte")
        if b in (0xDE, 0xDF):
            bad.append(f"{syl}: byte 0x{b:02X} is a voicing mark")
        if code in FORBIDDEN_GLOBAL_CODES:
            bad.append(f"{syl}: code {code} collides with a reserved global code")
        if not 0 <= code - 1 < GLOBAL_GLYPH_COUNT:
            bad.append(f"{syl}: slot {code - 1} outside the global atlas")
        # single byte -> single code through the real converter rules
        if simulate_converter(bytes((b,))) != [code]:
            bad.append(f"{syl}: converter does not map 0x{b:02X} to {code}")
    # composition can never trigger: no chosen byte is 0xDE/0xDF, so no
    # in-name byte pair forms a dakuten/handakuten sequence.
    if bad:
        raise NamePatchError("kana assignment constraint violation: " + "; ".join(bad))
    return {"codes": sorted(codes), "slots": sorted(c - 1 for c in codes)}


def kana_spelling(name: str) -> bytes:
    return bytes(SYLLABLE_KANA[ch] for ch in name)


def latin_spelling(name: str) -> bytes:
    return name.encode("ascii")


def name_rows(fallback_latin: bool) -> dict[int, bytes]:
    names = LATIN_NAMES if fallback_latin else HANGUL_NAMES
    spell = latin_spelling if fallback_latin else kana_spelling
    rows: dict[int, bytes] = {}
    for cid, name in names.items():
        raw = spell(name)
        if len(raw) > SLOT_BYTES - 1:
            raise NamePatchError(f"name id {cid} spelling exceeds 7 bytes: {name}")
        rows[cid] = raw
    return rows


# ---------------------------------------------------------------------------
# SLZ mode-3 (LZSS16) token-minimal encoder
# ---------------------------------------------------------------------------
# Grammar (from the ground-truth decoder): 2 flag bytes precede each group of
# 16 tokens (LSB-first, byte0 then byte1; 1 = literal).  Every token carries
# exactly 2 payload bytes: a literal copies one word; a match copies
# L in 2..17 words from word distance 1..0xFFF (overlap legal), encoded as
# (dist & 0xFF, ((L-2) << 4) | (dist >> 8)).  Since literals and matches cost
# the same 2 payload bytes + 1 flag bit, minimizing the token count minimizes
# the output size exactly; the DP below is token-minimal over the full token
# set (up to a bounded exception: interiors of >=17-word runs are pinned to
# the always-valid distance-1/length-17 match to keep the matcher fast).

_M3_WINDOW = 0xFFF
_M3_MAXLEN = 17


def _run_lengths16(w, n: int):
    if n == 1:
        return _np.ones(1, dtype=_np.int64)
    ends = _np.flatnonzero(w[1:] != w[:-1])
    ends = _np.append(ends, n - 1)
    idx = _np.arange(n, dtype=_np.int64)
    return ends[_np.searchsorted(ends, idx)] - idx + 1


def _find_matches16(w, n: int):
    """For each word position: longest previous match (words) and a distance."""
    maxlen = _np.zeros(n, dtype=_np.int32)
    mdist = _np.zeros(n, dtype=_np.int32)
    if n < 2:
        return maxlen, mdist
    run = _run_lengths16(w, n)
    interior = _np.empty(n, dtype=bool)
    interior[0] = False
    _np.equal(w[1:], w[:-1], out=interior[1:])
    deep = interior & (run >= _M3_MAXLEN)
    maxlen[deep] = _M3_MAXLEN
    mdist[deep] = 1
    cand = _np.flatnonzero(~deep)

    a = w.astype(_np.uint64)
    ids = [None] * 5
    ids[1] = a
    c64k = _np.uint64(65536)
    for k in range(2, 5):
        ids[k] = ids[k - 1][: n - k + 1] * c64k + a[k - 1:]

    for L in range(2, _M3_MAXLEN + 1):
        m = n - L + 1
        cand = cand[cand < m]
        if cand.size < 2:
            break
        keys = []
        off = 0
        rem = L
        while rem > 0:
            k = min(4, rem)
            keys.append(ids[k][cand + off])
            off += k
            rem -= k
        if len(keys) == 1:
            order = _np.argsort(keys[0], kind="stable")
        else:
            order = _np.lexsort(tuple(reversed(keys)))  # stable, primary first
        sorted_keys = [k[order] for k in keys]
        same = _np.ones(cand.size - 1, dtype=bool)
        for k in sorted_keys:
            same &= k[1:] == k[:-1]
        p = cand[order]
        d = p[1:] - p[:-1]  # positive within equal-key groups (stable sort)
        ok = same & (d <= _M3_WINDOW)
        tgt = p[1:][ok]
        maxlen[tgt] = L
        mdist[tgt] = d[ok]
        keep = _np.zeros(p.size, dtype=bool)
        keep[1:] = ok
        keep[:-1] |= ok
        cand = _np.sort(p[keep])
    return maxlen, mdist


def compress_slz_mode3_optimal(data: bytes) -> bytes:
    """Token-minimal SLZ16 mode-3 encoder.

    Round-trips through decompress_slz_payload(payload, 3, len(data)).
    """
    if _np is None:
        raise NamePatchError("numpy is required for the mode-3 encoder")
    data = bytes(data)
    n = len(data)
    if n % 2:
        raise ValueError("mode-3 image length must be even")
    nw = n // 2
    if nw == 0:
        return b""
    w = _np.frombuffer(data, dtype="<u2")
    maxlen_a, mdist_a = _find_matches16(w, nw)
    maxlen = maxlen_a.tolist()
    mdist = mdist_a.tolist()

    cost = [0] * (nw + 1)
    tlen = [1] * nw
    for i in range(nw - 1, -1, -1):
        best = cost[i + 1]
        length = 1
        m = maxlen[i]
        if m:
            seg = cost[i + 2 : i + m + 1]
            c = min(seg)
            if c < best:
                best = c
                length = seg.index(c) + 2
        cost[i] = best + 1
        tlen[i] = length

    out = bytearray()
    i = 0
    bit = 16
    flag_pos = 0
    while i < nw:
        if bit == 16:
            flag_pos = len(out)
            out.extend(b"\x00\x00")
            bit = 0
        length = tlen[i]
        if length == 1:
            if bit < 8:
                out[flag_pos] |= 1 << bit
            else:
                out[flag_pos + 1] |= 1 << (bit - 8)
            out.extend(data[2 * i : 2 * i + 2])
        else:
            d = mdist[i]
            out.append(d & 0xFF)
            out.append(((length - 2) << 4) | (d >> 8))
        bit += 1
        i += length
    return bytes(out)


# ---------------------------------------------------------------------------
# SLE stream cipher
# ---------------------------------------------------------------------------

def decrypt_sle_payload(payload: bytes, key: bytes = SLE_KEY) -> bytes:
    if _np is not None:
        c = _np.frombuffer(payload, dtype=_np.uint8).astype(_np.int32)
        idx = _np.arange(len(payload), dtype=_np.int64)
        running = (3 + 3 * idx) & 0xFF
        keys = _np.frombuffer((key * (len(payload) // 16 + 1))[: len(payload)], dtype=_np.uint8)
        return (((c - running) & 0xFF) ^ keys).astype(_np.uint8).tobytes()
    out = bytearray(len(payload))
    running = 3
    for i, v in enumerate(payload):
        out[i] = ((v - running) & 0xFF) ^ key[i & 15]
        running = (running + 3) & 0xFF
    return bytes(out)


def encrypt_sle_payload(plain: bytes, key: bytes = SLE_KEY) -> bytes:
    if _np is not None:
        p = _np.frombuffer(plain, dtype=_np.uint8).astype(_np.int32)
        idx = _np.arange(len(plain), dtype=_np.int64)
        running = (3 + 3 * idx) & 0xFF
        keys = _np.frombuffer((key * (len(plain) // 16 + 1))[: len(plain)], dtype=_np.uint8)
        return (((p ^ keys) + running) & 0xFF).astype(_np.uint8).tobytes()
    out = bytearray(len(plain))
    running = 3
    for i, v in enumerate(plain):
        out[i] = ((v ^ key[i & 15]) + running) & 0xFF
        running = (running + 3) & 0xFF
    return bytes(out)


def parse_sle(data: bytes) -> list[dict[str, int]]:
    members = []
    offset = 0
    while True:
        header = data[offset : offset + 16]
        if header[:3] != b"SLE":
            raise NamePatchError(f"SLE signature missing at 0x{offset:X}")
        mode = header[3]
        compressed, unpacked, next_rel = struct.unpack_from("<III", header, 4)
        members.append({
            "offset": offset, "mode": mode, "compressed": compressed,
            "unpacked": unpacked, "next_rel": next_rel,
        })
        if next_rel == 0:
            break
        offset += next_rel
    return members


# ---------------------------------------------------------------------------
# Hangul glyph rendering (matches the main patch: Nanum 22px in a 24x24 cell)
# ---------------------------------------------------------------------------

def render_hangul_glyphs(font_path: Path) -> dict[int, tuple[str, int, bytes]]:
    """slot index -> (syllable, advance, 288-byte 4bpp bitmap), 2 gray levels."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), RENDER_PX)
    result: dict[int, tuple[str, int, bytes]] = {}
    for syllable, kana in sorted(SYLLABLE_KANA.items(), key=lambda kv: kv[1]):
        slot = (kana - 0x13) - 1
        bbox = font.getbbox(syllable)
        if bbox is None:
            raise NamePatchError(f"font has no glyph for {syllable!r}")
        advance = max(1, min(GLYPH_CELL, round(font.getlength(syllable))))
        image = Image.new("L", (GLYPH_CELL, GLYPH_CELL), 0)
        draw = ImageDraw.Draw(image)
        ink_w, ink_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (GLYPH_CELL - ink_w) // 2 - bbox[0])
        y = max(0, (GLYPH_CELL - ink_h) // 2 - bbox[1])
        draw.text((x, y), syllable, font=font, fill=255)
        pixels = [
            round(v / 255 * (GRAY_LEVELS - 1)) * 15 // (GRAY_LEVELS - 1)
            for v in image.getdata()
        ]
        bitmap = bytes(
            pixels[i] | (pixels[i + 1] << 4) for i in range(0, len(pixels), 2)
        )
        if not any(bitmap):
            raise NamePatchError(f"rendered empty bitmap for {syllable!r}")
        result[slot] = (syllable, advance, bitmap)
    return result


# ---------------------------------------------------------------------------
# image builders (pure functions original image -> patched image)
# ---------------------------------------------------------------------------

def _check_font_header(image: bytes) -> None:
    if not image.startswith(b"so3mclib 1.72"):
        raise NamePatchError("global font member is not so3mclib 1.72")
    words = struct.unpack_from("<13I", image, 0x10)
    if (words[2], words[3], words[4]) != (0x80, 0x200, GLOBAL_GLYPH_COUNT):
        raise NamePatchError("unexpected global font section layout")
    if (words[7], words[8], words[9]) != (24, 24, 24):
        raise NamePatchError("unexpected global font glyph geometry")
    if words[12] != len(image):
        raise NamePatchError("global font size field mismatch")


def build_font_image(original: bytes, glyphs: dict[int, tuple[str, int, bytes]]) -> bytes:
    _check_font_header(original)
    image = bytearray(original)
    for slot, (_syl, advance, bitmap) in glyphs.items():
        if not 0 <= slot < GLOBAL_GLYPH_COUNT:
            raise NamePatchError(f"slot {slot} out of range")
        if len(bitmap) != GLYPH_BYTES:
            raise NamePatchError(f"slot {slot}: bitmap is {len(bitmap)} bytes")
        image[0x80 + slot] = advance
        start = 0x200 + slot * GLYPH_BYTES
        image[start : start + GLYPH_BYTES] = bitmap
    return bytes(image)


def _table_rows_bytes(rows: dict[int, bytes], order: str) -> bytes:
    ids = range(1, 11) if order == "forward" else range(10, 0, -1)
    return b"".join(rows[cid].ljust(SLOT_BYTES, b"\x00") for cid in ids)


def build_table_image(
    original: bytes, table_offset: int, order: str, rows: dict[int, bytes],
    expected_rows: dict[int, bytes], literals: dict[int, int] | None = None,
) -> bytes:
    old = _table_rows_bytes(expected_rows, order)
    have = original[table_offset : table_offset + len(old)]
    if have != old:
        raise NamePatchError(
            f"original name table mismatch at 0x{table_offset:X}: "
            f"{have.hex()} != {old.hex()}"
        )
    image = bytearray(original)
    new = _table_rows_bytes(rows, order)
    image[table_offset : table_offset + len(new)] = new
    if literals:
        for cid, offset in literals.items():
            expect = expected_rows[cid].ljust(SLOT_BYTES, b"\x00")
            if original[offset : offset + SLOT_BYTES] != expect:
                raise NamePatchError(f"literal for id {cid} mismatch at 0x{offset:X}")
            image[offset : offset + SLOT_BYTES] = rows[cid].ljust(SLOT_BYTES, b"\x00")
    return bytes(image)


def decode_table(image: bytes, table_offset: int, order: str,
                 replaced: dict[int, str]) -> dict[int, str]:
    """Read a 10-slot table and decode each entry through the converter sim."""
    ids = list(range(1, 11)) if order == "forward" else list(range(10, 0, -1))
    decoded: dict[int, str] = {}
    for i, cid in enumerate(ids):
        slot = image[table_offset + i * SLOT_BYTES : table_offset + (i + 1) * SLOT_BYTES]
        raw = slot.split(b"\x00", 1)[0]
        codes = simulate_converter(slot)
        decoded[cid] = "".join(code_to_char(c, replaced) for c in codes)
        if len(raw) > SLOT_BYTES - 1:
            raise NamePatchError(f"id {cid}: entry not NUL-terminated")
    return decoded


# ---------------------------------------------------------------------------
# build: everything that must be written, computed before any write
# ---------------------------------------------------------------------------

@dataclass
class MemberPlan:
    name: str
    iso_offset: int
    write_bytes: bytes          # full bytes to write at iso_offset
    image: bytes                # decompressed module image (for verification)
    mode_in: int
    mode_out: int
    orig_compressed: int
    new_compressed: int
    allocation: int
    extra: dict = field(default_factory=dict)

    def fit_report(self) -> dict:
        return {
            "member": self.name, "iso_offset": self.iso_offset,
            "mode_in": self.mode_in, "mode_out": self.mode_out,
            "orig_compressed": self.orig_compressed,
            "new_compressed": self.new_compressed,
            "allocation": self.allocation,
            "margin": self.allocation - self.new_compressed,
            "fits": self.new_compressed <= self.allocation,
            **self.extra,
        }


def _slz_header(magic: bytes, mode: int, compressed: int, unpacked: int,
                next_rel: int) -> bytes:
    return magic + bytes((mode,)) + struct.pack("<III", compressed, unpacked, next_rel)


def _writable_allocation(iso_bytes_after_payload: bytes, structural: int,
                         orig_compressed: int) -> int:
    """Original payload size plus verified contiguous zero padding only."""
    zeros = 0
    for value in iso_bytes_after_payload[: structural - orig_compressed]:
        if value:
            break
        zeros += 1
    return orig_compressed + zeros


def _read_extent(handle, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise NamePatchError(f"short read at ISO offset 0x{offset:X}")
    return data


def _extract_member(handle, member: dict) -> tuple[bytes, bytes, int]:
    """Returns (decompressed image, raw header, next_rel)."""
    header = _read_extent(handle, member["iso_offset"], 16)
    if header[:3] != b"SLZ":
        raise NamePatchError(f"{member['name']}: SLZ header missing")
    mode = header[3]
    compressed, unpacked, next_rel = struct.unpack_from("<III", header, 4)
    if mode != member["mode"] or unpacked != member["unpacked"]:
        raise NamePatchError(
            f"{member['name']}: unexpected SLZ header "
            f"(mode {mode}, unpacked {unpacked})"
        )
    payload = _read_extent(handle, member["iso_offset"] + 16, compressed)
    image = decompress_slz_payload(payload, mode, unpacked)
    return image, header, next_rel


def _member_plan(handle, member: dict, image: bytes, mode_out: int,
                 payload: bytes, next_rel: int, extra: dict) -> MemberPlan:
    structural = member["boundary"] - member["iso_offset"] - 16
    tail = _read_extent(
        handle, member["iso_offset"] + 16 + member["compressed"],
        structural - member["compressed"],
    )
    allocation = _writable_allocation(tail, structural, member["compressed"])
    if len(payload) > allocation:
        raise NamePatchError(
            f"{member['name']}: recompressed payload {len(payload)} exceeds "
            f"allocation {allocation}"
        )
    if decompress_slz_payload(payload, mode_out, len(image)) != image:
        raise NamePatchError(f"{member['name']}: SLZ round-trip failed")
    header = _slz_header(b"SLZ", mode_out, len(payload), len(image), next_rel)
    return MemberPlan(
        member["name"], member["iso_offset"], header + payload, image,
        member["mode"], mode_out, member["compressed"], len(payload),
        allocation, extra,
    )


def build_plans(handle, fallback_latin: bool, font_path: Path) -> list[MemberPlan]:
    validate_code_constraints()
    rows = name_rows(fallback_latin)
    plans: list[MemberPlan] = []

    # cross-check the hidden index: every extent must live inside its archive
    handle.seek(0x200000)
    table = decode_index(handle.read(ENTRY_COUNT * 3 * 4))
    for member in (FONT_MEMBER, S0068_MEMBER, S1069_MEMBER):
        aid = member["archive_id"]
        start = table[aid] * SECTOR
        end = start + table[ENTRY_COUNT + aid] * SECTOR
        if not (start <= member["iso_offset"] and member["boundary"] <= end):
            raise NamePatchError(
                f"{member['name']}: extent not inside archive {aid} "
                f"(0x{start:X}..0x{end:X})"
            )
    sle_start = table[SLE_EXTENT["archive_id"]] * SECTOR
    sle_size = table[ENTRY_COUNT + SLE_EXTENT["archive_id"]] * SECTOR
    if (sle_start, sle_size) != (SLE_EXTENT["iso_offset"], SLE_EXTENT["size"]):
        raise NamePatchError("0002.sle extent mismatch against the hidden index")

    # ---- global font member (primary plan only) ----
    if not fallback_latin:
        image, _, next_rel = _extract_member(handle, FONT_MEMBER)
        if sha256(image) != FONT_MEMBER["sha"]:
            raise NamePatchError(
                "global font member does not match the original "
                "(already patched, or an incompatible intermediate ISO)"
            )
        glyphs = render_hangul_glyphs(font_path)
        patched = build_font_image(image, glyphs)
        payload = compress_slz_mode2_optimal(patched)
        plans.append(_member_plan(
            handle, FONT_MEMBER, patched, 2, payload, next_rel,
            {"replaced_slots": sorted(glyphs)},
        ))

    # ---- archive 68 forward table ----
    image, _, next_rel = _extract_member(handle, S0068_MEMBER)
    if sha256(image) != S0068_MEMBER["sha"]:
        raise NamePatchError("archive 68 stream 61 does not match the original")
    patched = build_table_image(image, T0068_OFFSET, "forward", rows, _ORIG_ROWS)
    payload = compress_slz_mode2_optimal(patched)
    plans.append(_member_plan(handle, S0068_MEMBER, patched, 2, payload, next_rel,
                              {"table_offset": T0068_OFFSET}))

    # ---- archive 1069 reverse pool (fixes the id9 dev leftover) ----
    image, _, next_rel = _extract_member(handle, S1069_MEMBER)
    if sha256(image) != S1069_MEMBER["sha"]:
        raise NamePatchError("archive 1069 stream 1679 does not match the original")
    patched = build_table_image(image, T1069_OFFSET, "reverse", rows, _ORIG_ROWS_1069)
    payload = compress_slz_mode3_optimal(patched)
    mode_out = 3
    if len(payload) > S1069_MEMBER["boundary"] - S1069_MEMBER["iso_offset"] - 16:
        payload = compress_slz_mode2_optimal(patched)  # decoder dispatches on mode
        mode_out = 2
    plans.append(_member_plan(handle, S1069_MEMBER, patched, mode_out, payload,
                              next_rel, {"table_offset": T1069_OFFSET}))

    # ---- 0002.sle member 1 ----
    sle = _read_extent(handle, SLE_EXTENT["iso_offset"], SLE_EXTENT["size"])
    if sha256(sle) != SLE_EXTENT["sha"]:
        raise NamePatchError("0002.sle does not match the original")
    members = parse_sle(sle)
    if len(members) != 2 or members[1]["offset"] != SLE_EXTENT["member1_offset"]:
        raise NamePatchError("unexpected 0002.sle chain layout")
    m1 = members[1]
    if (m1["mode"], m1["compressed"], m1["unpacked"]) != (
        SLE_EXTENT["member1_mode"], SLE_EXTENT["member1_compressed"],
        SLE_EXTENT["member1_unpacked"],
    ):
        raise NamePatchError("unexpected 0002.sle member 1 header")
    payload_start = m1["offset"] + 16
    plain = decrypt_sle_payload(sle[payload_start : payload_start + m1["compressed"]])
    image = decompress_slz_payload(plain, m1["mode"], m1["unpacked"])
    if sha256(image) != SHA_SLE_M1:
        raise NamePatchError("0002.sle member 1 image does not match the original")
    patched = build_table_image(image, SLE_TABLE_OFFSET, "reverse", rows,
                                _ORIG_ROWS, literals=SLE_LITERALS)
    # keep mode 3: the boot-ELF SLE loader is only proven on mode-3 members
    new_plain = compress_slz_mode3_optimal(patched)
    structural = SLE_EXTENT["size"] - payload_start
    allocation = _writable_allocation(
        sle[payload_start + m1["compressed"]:], structural, m1["compressed"]
    )
    if len(new_plain) > allocation:
        raise NamePatchError(
            f"0002.sle member 1: mode-3 payload {len(new_plain)} exceeds "
            f"allocation {allocation}; use --fallback-latin only if this also "
            "fails there"
        )
    if decompress_slz_payload(new_plain, 3, len(patched)) != patched:
        raise NamePatchError("0002.sle member 1: SLZ round-trip failed")
    encrypted = encrypt_sle_payload(new_plain)
    if decrypt_sle_payload(encrypted) != new_plain:
        raise NamePatchError("0002.sle member 1: cipher round-trip failed")
    new_sle = bytearray(sle)
    new_sle[m1["offset"] : payload_start] = _slz_header(
        b"SLE", 3, len(new_plain), len(patched), 0
    )
    new_sle[payload_start : payload_start + len(encrypted)] = encrypted
    # bytes between the new payload end and the original end stay as-is; the
    # loader reads exactly compressed_size bytes.
    if len(new_sle) != SLE_EXTENT["size"]:
        raise NamePatchError("0002.sle rebuild changed the file size")
    if bytes(new_sle[: m1["offset"]]) != sle[: m1["offset"]]:
        raise NamePatchError("0002.sle member 0 region changed")
    plans.append(MemberPlan(
        SLE_EXTENT["name"], SLE_EXTENT["iso_offset"], bytes(new_sle), patched,
        3, 3, m1["compressed"], len(new_plain), allocation,
        {"table_offset": SLE_TABLE_OFFSET, "literals": dict(SLE_LITERALS)},
    ))
    return plans


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def _load_reference(path: Path, expected_sha: str, what: str) -> bytes:
    if not path.exists():
        raise NamePatchError(f"reference for {what} missing: {path}")
    data = path.read_bytes()
    if sha256(data) != expected_sha:
        raise NamePatchError(f"reference for {what} does not match its SHA-256")
    return data


def _diff_outside(a: bytes, b: bytes, spans: list[tuple[int, int]]) -> int:
    if len(a) != len(b):
        return abs(len(a) - len(b)) or 1
    mask = bytearray(a)
    other = bytearray(b)
    for start, end in spans:
        mask[start:end] = other[start:end]
    return sum(1 for x, y in zip(mask, other) if x != y)


def verify_name_patch(iso_path: Path, *, fallback_latin: bool = False,
                      font_path: Path = DEFAULT_FONT) -> dict:
    """Re-extract all three tables + the global font from a patched ISO and
    prove that names decode to the exact target strings and that untouched
    bytes are identical to the originals.  Raises NamePatchError on failure.
    """
    names = LATIN_NAMES if fallback_latin else HANGUL_NAMES
    rows = name_rows(fallback_latin)
    replaced = {} if fallback_latin else replaced_slot_map()
    report: dict[str, object] = {"iso": str(iso_path), "mode": (
        "fallback_latin" if fallback_latin else "kana_slot_repurpose")}
    problems: list[str] = []

    ref_font = _load_reference(REF_FONT, SHA_FONT, "global font")
    ref_0068 = _load_reference(REF_0068, SHA_0068, "archive 68 stream 61")
    ref_1069 = _load_reference(REF_1069, SHA_1069, "archive 1069 stream 1679")
    ref_sle = _load_reference(REF_SLE, SHA_SLE, "0002.sle")
    ref_m1 = decompress_slz_payload(
        decrypt_sle_payload(ref_sle[73172 : 73172 + 571646]), 3, 1186048
    )
    if sha256(ref_m1) != SHA_SLE_M1:
        raise NamePatchError("reference 0002.sle member 1 decode mismatch")

    glyphs = None if fallback_latin else render_hangul_glyphs(font_path)

    with Path(iso_path).open("rb") as handle:
        # ---- global font ----
        image, _, _ = _extract_member(handle, {**FONT_MEMBER, "mode": 2})
        if fallback_latin:
            if image != ref_font:
                problems.append("fallback mode but the global font was modified")
        else:
            expected = build_font_image(ref_font, glyphs)
            if image != expected:
                problems.append("global font member differs from the expected build")
            spans = []
            for slot in glyphs:
                spans.append((0x80 + slot, 0x80 + slot + 1))
                spans.append((0x200 + slot * GLYPH_BYTES,
                              0x200 + (slot + 1) * GLYPH_BYTES))
            untouched_diff = _diff_outside(image, ref_font, spans)
            if untouched_diff:
                problems.append(
                    f"global font: {untouched_diff} bytes changed outside the "
                    f"21 replaced slots"
                )
            report["font"] = {
                "replaced_slots": sorted(glyphs),
                "advances": {syl: adv for _s, (syl, adv, _b) in sorted(glyphs.items())},
                "untouched_bytes_diff": untouched_diff,
            }

        # ---- the three name tables ----
        tables = {}
        img_0068, _, _ = _extract_member(handle, {**S0068_MEMBER, "mode": 2})
        tables["0068_forward"] = (img_0068, ref_0068, T0068_OFFSET, "forward")
        hdr = _read_extent(handle, S1069_MEMBER["iso_offset"], 16)
        img_1069, _, _ = _extract_member(handle, {**S1069_MEMBER, "mode": hdr[3]})
        tables["1069_reverse"] = (img_1069, ref_1069, T1069_OFFSET, "reverse")

        sle = _read_extent(handle, SLE_EXTENT["iso_offset"], SLE_EXTENT["size"])
        members = parse_sle(sle)
        m1 = members[1]
        plain = decrypt_sle_payload(
            sle[m1["offset"] + 16 : m1["offset"] + 16 + m1["compressed"]]
        )
        img_m1 = decompress_slz_payload(plain, m1["mode"], m1["unpacked"])
        tables["sle_member1_reverse"] = (img_m1, ref_m1, SLE_TABLE_OFFSET, "reverse")
        if sle[:73156] != ref_sle[:73156]:
            problems.append("0002.sle member 0 region changed")

        table_report = {}
        for label, (image, ref, offset, order) in tables.items():
            decoded = decode_table(image, offset, order, replaced)
            bad = {cid: got for cid, got in decoded.items() if got != names[cid]}
            if bad:
                problems.append(f"{label}: wrong decode {bad}")
            spans = [(offset, offset + 10 * SLOT_BYTES)]
            if label == "sle_member1_reverse":
                spans += [(off, off + SLOT_BYTES) for off in SLE_LITERALS.values()]
                for cid, off in SLE_LITERALS.items():
                    lit = image[off : off + SLOT_BYTES]
                    got = "".join(
                        code_to_char(c, replaced) for c in simulate_converter(lit)
                    )
                    if got != names[cid]:
                        problems.append(f"sle literal id {cid} decodes to {got!r}")
            diff = _diff_outside(image, ref, spans)
            if diff:
                problems.append(f"{label}: {diff} bytes changed outside the table")
            table_report[label] = {"decoded": decoded, "outside_diff": diff}
        report["tables"] = table_report

        # raw byte double-check: the tables must literally be our spellings
        for label, (image, _ref, offset, order) in tables.items():
            if image[offset : offset + 80] != _table_rows_bytes(rows, order):
                problems.append(f"{label}: raw table bytes differ from the plan")

    if problems:
        raise NamePatchError("verification failed: " + " | ".join(problems))
    report["ok"] = True
    return report


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply_name_patch(iso_in: Path, iso_out: Path | None = None,
                     report_path: Path | None = None, *,
                     fallback_latin: bool = False,
                     font_path: Path = DEFAULT_FONT) -> dict:
    """Patch the hardcoded name tables (and, in the primary plan, the global
    font) of a SO3 DC Disc 1 ISO.  iso_out may equal iso_in (in-place)."""
    iso_in = Path(iso_in)
    iso_out = Path(iso_out) if iso_out is not None else iso_in
    in_place = iso_in.resolve() == iso_out.resolve()

    with iso_in.open("rb") as handle:
        plans = build_plans(handle, fallback_latin, font_path)

    if not in_place:
        iso_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(iso_in, iso_out)

    with iso_out.open("r+b") as handle:
        for plan in plans:
            handle.seek(plan.iso_offset)
            handle.write(plan.write_bytes)

    # byte-level post-write verification of every member
    with iso_out.open("rb") as handle:
        for plan in plans:
            if plan.name == SLE_EXTENT["name"]:
                sle = _read_extent(handle, plan.iso_offset, SLE_EXTENT["size"])
                m1 = parse_sle(sle)[1]
                plain = decrypt_sle_payload(
                    sle[m1["offset"] + 16 : m1["offset"] + 16 + m1["compressed"]]
                )
                image = decompress_slz_payload(plain, m1["mode"], m1["unpacked"])
            else:
                header = _read_extent(handle, plan.iso_offset, 16)
                compressed, unpacked, _ = struct.unpack_from("<III", header, 4)
                payload = _read_extent(handle, plan.iso_offset + 16, compressed)
                image = decompress_slz_payload(payload, header[3], unpacked)
            if image != plan.image:
                raise NamePatchError(f"{plan.name}: post-write image mismatch")

    verification = verify_name_patch(
        iso_out, fallback_latin=fallback_latin, font_path=font_path
    )

    report = {
        "task": "D11-2 hardcoded character-name tables",
        "plan": "fallback_latin" if fallback_latin else "kana_slot_repurpose",
        "iso_in": str(iso_in),
        "iso_out": str(iso_out),
        "in_place": in_place,
        "names": LATIN_NAMES if fallback_latin else HANGUL_NAMES,
        "kana_assignment": None if fallback_latin else {
            syl: {"byte": f"0x{b:02X}", "code": b - 0x13, "slot": b - 0x14}
            for syl, b in SYLLABLE_KANA.items()
        },
        "members": [plan.fit_report() for plan in plans],
        "verification": verification,
    }
    if report_path is not None:
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso_in", type=Path)
    parser.add_argument("iso_out", type=Path, nargs="?",
                        help="omit to patch in place")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fallback-latin", action="store_true",
                        help="Latin names via existing A-Z codes; no font change")
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--config", type=Path, default=None,
                        help="per-disc extent override JSON (disc-2 port); "
                             "default = disc-1 constants")
    args = parser.parse_args()
    if args.config is not None:
        summary = apply_disc_config(args.config)
        print(json.dumps({"disc_config": summary}, ensure_ascii=False, indent=2))
    if args.verify_only:
        result = verify_name_patch(
            args.iso_in, fallback_latin=args.fallback_latin, font_path=args.font
        )
    else:
        result = apply_name_patch(
            args.iso_in, args.iso_out, args.report,
            fallback_latin=args.fallback_latin, font_path=args.font,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

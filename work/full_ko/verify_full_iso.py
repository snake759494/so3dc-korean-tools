# -*- coding: utf-8 -*-
"""Independent full-ISO verifier for the SO3 DC full-disc Korean patch.

This is the full-disc analogue of publish/so3dc-korean-tools/tools/
verify_hyda_dialogue_iso.py.  It deliberately does NOT import any encoding or
rebuild logic from so3_full_patch.py (the module under test).  The only
imports from the project are the in-game-verified primitives of so3_repack
(Mclib.parse, decompress_slz_payload, read_index); everything else — the
control tokenizer, PK1/PACK/SLZ-chain archive layout parser, glyph decoding
via bitmap signatures, Nanum re-rendering and the pixel-width engine — is
re-implemented here from the file-format knowledge, mirroring how the Hyda
verifier re-implements its own PK1/boundary/tokenizer.

Ground truth = the PLAN (patch_plan_full.json).  The plan is the authoritative
statement of what was patched, AFTER build_full_plan.py -> plan_fixups.py
(reshapes variant-marker and dynamic-token entries) -> fit_repair.py (drops
capacity-over messages, leaving them Japanese).  The verifier still decodes the
ISO bytes independently (own tokenizer, layout parser, glyph decode, font
re-render) and compares against the plan's {korean, speaker_korean} per
(container, msgid); the plan's `unique` container-sha entries are expanded to
every on-disc occurrence via container_catalog.  A message NOT in the plan must
be byte-identical to the original (that is the correct check for the fit_repair
drops); a planned message that did not change, or a non-planned message that
did, is a real bug.  tr_out is only an optional advisory cross-check
(--tr-out-crosscheck); it legitimately diverges from the plan after the
post-processors and never affects the exit code.

Checks
  1. sizes + original SHA-256 pin (+ optional patched pin), Nanum font pin
  2. hidden index: raw 0x200000 block byte-identical AND decoded 6,144 entries
  3. diff scope: streamed compare; every differing byte inside a planned
     archive extent; every planned archive differs; total diff bytes reported
  4. per planned archive: package structure identity (PK1 row count/tags/ids,
     PACK table shape/aux), non-target records byte-identical (possibly
     shifted), gaps zero-filled (or byte-preserved in place), record
     allocations respected, SLZ headers sane (mode preserved, next_rel chains
     consistent), every target SLZ decompresses with round-trip size match
  5. per target container: geometry/local_base/message-id table unchanged,
     non-target messages logically identical, every translated message
     decodes EXACTLY to its expected Korean text (independent tokenizer +
     independent glyph decode), control signature preserved (drop =
     9080/9180/9380), speaker field decoded == speaker_korean (incl. the
     variant 8880+9380+8980 body-marker pattern)
  6. font proof: re-render the expected Korean charset (Nanum 22px, 2 gray
     levels) and match (width,bitmap) signatures; preserved ⟦G⟧ bitmaps
     byte-identical to the original atlas
  7. width gate: every translated line's px from the PATCHED atlas advance
     widths vs the unit's delivery budget (batch budget_px)
  8. coverage: every translated unit applied at EVERY inventory occurrence;
     JP containers left unpatched reported (error unless allowed)
  9. xdelta: encode original->patched, re-apply, byte-identical

--name-patch: when the ISO additionally carries so3_name_patch (which runs
AFTER the message patch), its four extents (archive 8 global font member,
archive 2 = 0002.sle, the archive 68 and 1069 members) are whitelisted in the
diff scope; their content checks are delegated to
so3_name_patch.verify_name_patch(iso), plus an independent audit proves the
global font's changed slots are exactly the 21 documented in
name_patch_progress.md (all other 271 slots and the header byte-identical to
the original member).

Exit code is non-zero when any error was recorded.  A JSON report with
per-check details is written with --report.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
PUBLISH = WS / "publish" / "so3dc-korean-tools"
if not (PUBLISH / "so3_repack.py").exists():
    PUBLISH = WS  # repo checkout: the clone root IS the workspace
if str(PUBLISH) not in sys.path:
    sys.path.insert(0, str(PUBLISH))

# In-game-verified primitives only (see module docstring).
from so3_repack import Mclib, decompress_slz_payload, read_index  # noqa: E402

try:
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# ---------------------------------------------------------------------------
# pinned constants
# ---------------------------------------------------------------------------

ORIGINAL_ISO_SIZE = 4_689_854_464
ORIGINAL_ISO_SHA256 = "95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826"
NANUM_FONT_SHA256 = "4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767"
SECTOR = 0x800
INDEX_OFFSET = 0x200000
INDEX_ENTRIES = 0x1800
INDEX_BYTES = INDEX_ENTRIES * 3 * 4
FONT_PIXEL_SIZE = 22
GRAY_LEVELS = 2
GLYPH_CELL = 24
GLYPH_BYTES = GLYPH_CELL * GLYPH_CELL // 2  # 4bpp, low nibble first

NEWLINE_FAM = 0x80
PAGE_FAM = 0x81
SPEAKER_FLAG_FAM = 0x87
COLOR_SET_FAM = 0x88
COLOR_RESET_FAM = 0x89
SCALE_FAM, SCALE_RESET_FAM = 0x8A, 0x8B
ADVANCE_FAM, ADVANCE_RESET_FAM = 0x8C, 0x8D
RUBY_BASE_FAM, RUBY_TEXT_FAM = 0x90, 0x91
NAME_FAM = 0x93
PARTY_SLOT_FAM = 0xA1

POSITIONAL_FAMS = frozenset({0x88, 0x89, 0x92, 0x9C, 0xA1, 0xA2, 0xA3})
DROP_FAMS = frozenset({0x90, 0x91, 0x93})
STRUCTURAL_FAMS = frozenset({NEWLINE_FAM, PAGE_FAM})
# px allowance per runtime-substituted marker family (mirrors the delivery gate)
MARKER_ALLOWANCE_PX = {0x92: 96, 0xA1: 96, 0xA2: 144, 0xA3: 144}

# The identified slots of the (unchanged) global 24px atlas; codes are 1-based.
GLOBAL_CODE_MAP = {
    **{str(value): value + 1 for value in range(10)},
    **{chr(ord("A") + value): 14 + value for value in range(26)},
    **{chr(ord("a") + value): 40 + value for value in range(26)},
    "-": 11, ".": 12, "'": 13, ",": 258, " ": 232, "　": 233,
    "、": 235, "。": 237, "・": 239, "?": 241, "！": 243,
    "：": 259, "(": 263, "（": 264, "「": 272, "『": 273,
    "+": 278, "～": 283, "…": 284, "♪": 285,
}
GLOBAL_CHARACTER_MAP = {code: ch for ch, code in GLOBAL_CODE_MAP.items()}
MAX_GLOBAL_CODE = max(GLOBAL_CODE_MAP.values())

TOKEN_RE = re.compile(r"⟦([^⟦⟧]*)⟧")
G_SHA8_RE = re.compile(r"[0-9a-f]{8}")
PAGE_MARK = "⟦P⟧"

# --name-patch: the 21 global-atlas slots the name patch replaces
# (syllable -> global code; source: name_patch_progress.md).  Everything else
# in the global font member must stay byte-identical.
NAME_PATCH_FONT_CODES = {
    "알": 148, "쥬": 154, "아": 158, "이": 159, "트": 177, "넬": 181,
    "프": 185, "클": 165, "저": 169, "스": 170, "소": 172, "드": 175,
    "피": 184, "페": 186, "마": 188, "미": 189, "벨": 191, "라": 196,
    "리": 197, "레": 199, "로": 200,
}

DEFAULTS = {
    "controls": WS / "work" / "full_ko" / "control_sizes_full.json",
    "units": WS / "work" / "full_ko" / "translation_units.jsonl",
    "inventory": WS / "work" / "full_ko" / "inventory_containers.json",
    "batches_dir": WS / "work" / "full_ko" / "tr_batches",
    "tr_out_dir": WS / "work" / "full_ko" / "tr_out",
    "catalog": WS / "work" / "mclib_all_decode" / "container_catalog.csv",
    "manifest": WS / "work" / "full_unpack" / "disc1" / "manifests" / "stream_manifest.csv",
    "font": Path(os.environ.get("SO3_FONT", r"D:\ps2\NanumSquareNeo-cBd.ttf")),
    "xdelta": Path(os.environ.get("SO3_XDELTA", r"D:\ps2\xdelta.exe")),
}


class VerifyFatal(Exception):
    """Unrecoverable input/structure problem: abort the whole run."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def sha256(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(data)).hexdigest().lower()


def sha256_file(path: Path, chunk: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def u32(data, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u16(data, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def align4(value: int) -> int:
    return (value + 3) & ~3


def is_zero(view: bytes | memoryview) -> bool:
    b = bytes(view)
    return b.count(0) == len(b)


def count_diff_bytes(a: bytes, b: bytes) -> int:
    if a == b:
        return 0
    if _np is not None:
        return int((_np.frombuffer(a, dtype=_np.uint8)
                    != _np.frombuffer(b, dtype=_np.uint8)).sum())
    return sum(x != y for x, y in zip(a, b))


def logical_segment(seg: bytes) -> bytes:
    return seg.rstrip(b"\0") + b"\0"


def read_range(handle, start: int, size: int) -> bytes:
    handle.seek(start)
    data = handle.read(size)
    if len(data) != size:
        raise VerifyFatal(f"short read at 0x{start:X} ({len(data)}/{size})")
    return data


# ---------------------------------------------------------------------------
# error accounting
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, max_kept: int = 4000) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.error_count = 0
        self.warning_count = 0
        self.max_kept = max_kept
        self.by_check = Counter()

    def err(self, check: str, where: str, message: str, **info) -> None:
        self.error_count += 1
        self.by_check[f"error:{check}"] += 1
        if len(self.errors) < self.max_kept:
            entry = {"check": check, "where": where, "message": message}
            if info:
                entry["info"] = info
            self.errors.append(entry)

    def warn(self, check: str, where: str, message: str, **info) -> None:
        self.warning_count += 1
        self.by_check[f"warning:{check}"] += 1
        if len(self.warnings) < self.max_kept:
            entry = {"check": check, "where": where, "message": message}
            if info:
                entry["info"] = info
            self.warnings.append(entry)


# ---------------------------------------------------------------------------
# control table + tokenizer (independent implementation)
# ---------------------------------------------------------------------------

class Controls:
    def __init__(self, path: Path) -> None:
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.fixed: dict[int, int] = {}
        self.zero_terminated: set[int] = set()
        for key, info in spec.items():
            if key.startswith("_"):
                continue
            fam = int(key[:2], 16)
            kind = info.get("kind")
            if kind == "zero_terminated":
                self.zero_terminated.add(fam)
            elif kind == "fixed":
                total = info.get("total_size")
                if not isinstance(total, int) or total < 2:
                    raise VerifyFatal(f"bad fixed control size for {key}")
                self.fixed[fam] = total
            else:
                raise VerifyFatal(f"unknown control kind for {key}")
        if self.fixed.get(NEWLINE_FAM) != 2 or self.fixed.get(PAGE_FAM) != 2:
            raise VerifyFatal("control table lacks 2-byte 8080/8180")


class TokenError(ValueError):
    pass


def tokenize(seg: bytes, ctl: Controls) -> tuple[list[tuple], int]:
    """Strict tokenizer.  Token = ('g', code, raw) | ('c', fam, raw).

    A NUL at a token boundary terminates the message (index returned).
    Non-canonical control pairs (b1 != 0x80) and unknown families are hard
    errors: the corpus tokenizes 183,065/183,065 clean without them, and the
    patcher never emits them.
    """
    tokens: list[tuple] = []
    off = 0
    n = len(seg)
    while off < n:
        b0 = seg[off]
        if b0 == 0:
            return tokens, off
        if b0 < 0x80:
            tokens.append(("g", b0, seg[off:off + 1]))
            off += 1
            continue
        if off + 1 >= n:
            raise TokenError(f"truncated glyph/control at {off}")
        b1 = seg[off + 1]
        if b1 < 0x80:
            tokens.append(("g", (b0 & 0x7F) | (b1 << 7), seg[off:off + 2]))
            off += 2
            continue
        if b1 != 0x80:
            raise TokenError(f"non-canonical control {b0:02x}{b1:02x} at {off}")
        fam = b0
        if fam in ctl.zero_terminated:
            end = seg.find(b"\0", off + 2)
            if end < 0:
                raise TokenError(f"unterminated {fam:02x}80 at {off}")
            tokens.append(("c", fam, seg[off:end + 1]))
            off = end + 1
            continue
        size = ctl.fixed.get(fam)
        if size is None:
            raise TokenError(f"unknown control {fam:02x}80 at {off}")
        if off + size > n:
            raise TokenError(f"truncated control {fam:02x}80 at {off}")
        tokens.append(("c", fam, seg[off:off + size]))
        off += size
    raise TokenError("segment lacks a NUL terminator")


def _is_ctl(token: tuple, fam: int) -> bool:
    return token[0] == "c" and token[1] == fam


# ---------------------------------------------------------------------------
# message analysis (patcher segment semantics, re-implemented)
# ---------------------------------------------------------------------------

@dataclass
class Line:
    tokens: list          # all tokens of the line, original order
    units: list           # ('g', code, raw) glyphs + ('c', fam, raw) positionals
    invisible: list       # non-positional, non-drop control raws, in order


@dataclass
class Analysis:
    tokens: list
    prefix_raw: bytes
    field_tokens: list | None      # None = no 8780 speaker construct
    field_raw: bytes
    delim_raw: bytes
    pages: list                    # list[list[Line]]
    positional_raws: list          # reading order over the body
    variant_prefix: list | None    # inventory secondary speaker pattern tokens
    drop_count: int


def analyze_message(seg: bytes, ctl: Controls) -> Analysis:
    tokens, term = tokenize(seg, ctl)
    if any(seg[term + 1:]):
        raise TokenError("non-zero bytes after message terminator")

    delim_index = None
    for i in range(len(tokens) - 1):
        if _is_ctl(tokens[i], SPEAKER_FLAG_FAM) and _is_ctl(tokens[i + 1], NEWLINE_FAM):
            delim_index = i
            break
    variant_prefix = None
    if delim_index is not None:
        delim_start = delim_index
        if delim_start > 0 and _is_ctl(tokens[delim_start - 1], COLOR_RESET_FAM):
            delim_start -= 1
        j = delim_start
        while j > 0 and (tokens[j - 1][0] == "g" or _is_ctl(tokens[j - 1], NAME_FAM)):
            j -= 1
        prefix = tokens[:j]
        field: list | None = tokens[j:delim_start]
        delim = tokens[delim_start:delim_index + 2]
        body = tokens[delim_index + 2:]
    else:
        prefix, field, delim, body = [], None, [], tokens
        # inventory secondary speaker pattern: a control-only line before the
        # first newline holding exactly one name source (9380/a180), only
        # wrapped in color set/reset — becomes body markers after patching.
        for k, token in enumerate(tokens):
            if token[0] == "g":
                break
            if _is_ctl(token, NEWLINE_FAM):
                head = tokens[:k]
                fams = [t[1] for t in head if t[0] == "c"]
                if (head and len(head) == len(fams)
                        and sum(1 for f in fams if f in (NAME_FAM, PARTY_SLOT_FAM)) == 1
                        and all(f in (NAME_FAM, PARTY_SLOT_FAM, COLOR_SET_FAM,
                                      COLOR_RESET_FAM) for f in fams)):
                    variant_prefix = head
                break

    pages: list[list[Line]] = []
    positional_raws: list[bytes] = []
    drop_count = 0
    lines: list[list] = [[]]
    page_token_lines: list[list[list]] = [lines]
    for token in body:
        if _is_ctl(token, PAGE_FAM):
            lines = [[]]
            page_token_lines.append(lines)
        elif _is_ctl(token, NEWLINE_FAM):
            lines.append([])
        else:
            lines[-1].append(token)
    for token_lines in page_token_lines:
        page: list[Line] = []
        for raw_line in token_lines:
            units: list = []
            invisible: list = []
            for token in raw_line:
                if token[0] == "g":
                    units.append(token)
                elif token[1] in DROP_FAMS:
                    drop_count += 1
                elif token[1] in POSITIONAL_FAMS:
                    units.append(token)
                    positional_raws.append(token[2])
                else:
                    invisible.append(token[2])
            page.append(Line(raw_line, units, invisible))
        pages.append(page)

    return Analysis(
        tokens=tokens,
        prefix_raw=b"".join(t[2] for t in prefix),
        field_tokens=field,
        field_raw=b"".join(t[2] for t in field) if field is not None else b"",
        delim_raw=b"".join(t[2] for t in delim),
        pages=pages,
        positional_raws=positional_raws,
        variant_prefix=variant_prefix,
        drop_count=drop_count,
    )


# ---------------------------------------------------------------------------
# expected-text parsing (inventory text convention -> structure)
# ---------------------------------------------------------------------------
# unit = ('char', ch) | ('pos', n) | ('gtok', sha8)

class TextError(ValueError):
    pass


def parse_expected_line(line: str) -> list[tuple]:
    units: list[tuple] = []
    idx = 0
    for match in TOKEN_RE.finditer(line):
        for ch in line[idx:match.start()]:
            units.append(("char", ch))
        token = match.group(1)
        if token.isdigit():
            units.append(("pos", int(token)))
        elif token.startswith("G:") and G_SHA8_RE.fullmatch(token[2:].lower()):
            units.append(("gtok", token[2:].lower()))
        else:
            raise TextError(f"unresolved marker token ⟦{token}⟧")
        idx = match.end()
    tail = line[idx:]
    if "⟦" in tail or "⟧" in tail:
        raise TextError("unbalanced marker bracket")
    for ch in tail:
        units.append(("char", ch))
    return units


def parse_expected_body(korean: str) -> list[list[list[tuple]]]:
    """Inventory convention: lines split by \\n; a line starting with ⟦P⟧
    begins a new page.  Returns pages -> lines -> unit lists."""
    if "\0" in korean:
        raise TextError("expected text contains NUL")
    pages: list[list[list[tuple]]] = [[]]
    for raw_line in korean.split("\n"):
        line = raw_line
        if line.startswith(PAGE_MARK):
            pages.append([])
            line = line[len(PAGE_MARK):]
        if PAGE_MARK in line:
            raise TextError("⟦P⟧ must appear once, at line start")
        pages[-1].append(parse_expected_line(line))
    return pages


def parse_expected_inline(text: str) -> list[tuple]:
    units = parse_expected_line(text)
    if any(kind == "pos" for kind, _ in units):
        raise TextError("positional marker not allowed in speaker text")
    if "\n" in text:
        raise TextError("speaker text must be a single line")
    return units


def shift_markers(pages: list, delta: int) -> list:
    if delta == 0:
        return pages
    return [[[("pos", value + delta) if kind == "pos" else (kind, value)
              for kind, value in line] for line in page] for page in pages]


def expected_markers_in_order(pages: list) -> list[int]:
    return [value for page in pages for line in page
            for kind, value in line if kind == "pos"]


def strip_marker_chars(korean: str) -> str:
    return TOKEN_RE.sub("", korean).replace("\n", "")


# ---------------------------------------------------------------------------
# archive layout parser (PK1 / PACK / bare SLZ chains) — independent
# ---------------------------------------------------------------------------

class LayoutFail(ValueError):
    pass


@dataclass
class Member:
    offset: int
    mode: int
    comp: int
    unpacked: int
    next_rel: int
    span: int  # bytes to the next member start (last member: 16 + comp)


@dataclass
class Rec:
    tag: bytes | None
    rid: int | None
    aux: int | None
    offset: int
    size: int | None   # PK1 explicit size; PACK/chain: None (implied end)
    end: int
    members: list | None


@dataclass
class Pkg:
    kind: str          # 'pk1' | 'pack' | 'chain'
    start: int
    extent: int
    boundary: int
    rows: list
    header_size: int = 0
    count: int = 0


def walk_chain(data: bytes, start: int, limit: int) -> list[Member]:
    members: list[Member] = []
    cursor = start
    while True:
        if cursor + 16 > limit or data[cursor:cursor + 3] != b"SLZ":
            raise LayoutFail(f"bad SLZ member at 0x{cursor:X}")
        mode = data[cursor + 3]
        comp, unpacked, next_rel = struct.unpack_from("<III", data, cursor + 4)
        if cursor + 16 + comp > limit:
            raise LayoutFail(f"SLZ payload exceeds bounds at 0x{cursor:X}")
        if next_rel:
            if next_rel < 16 + comp or cursor + next_rel + 16 > limit:
                raise LayoutFail(f"bad next_rel at 0x{cursor:X}")
            members.append(Member(cursor, mode, comp, unpacked, next_rel, next_rel))
            cursor += next_rel
        else:
            members.append(Member(cursor, mode, comp, unpacked, 0, 16 + comp))
            return members


def parse_pk1(data: bytes, start: int) -> Pkg:
    if u32(data, start) != 0:
        raise LayoutFail(f"PK1 leading word not zero at 0x{start:X}")
    count, header_size, reserved = struct.unpack_from("<III", data, start + 4)
    if not 1 <= count <= 1000 or header_size != 0x10 + count * 16 or reserved != 0:
        raise LayoutFail(f"PK1 header geometry mismatch at 0x{start:X}")
    if start + header_size > len(data):
        raise LayoutFail(f"truncated PK1 table at 0x{start:X}")
    rows: list[Rec] = []
    previous_end = start + header_size
    for index in range(count):
        tag, rid, size, offset = struct.unpack_from("<4sIII", data, start + 0x10 + index * 16)
        absolute = start + offset
        if size <= 0 or offset < header_size or absolute + size > len(data):
            raise LayoutFail(f"PK1 row {index} outside archive at 0x{start:X}")
        if absolute < previous_end:
            raise LayoutFail(f"PK1 rows overlap/out of order at 0x{start:X} row {index}")
        previous_end = absolute + size
        members = None
        if data[absolute:absolute + 3] == b"SLZ":
            members = walk_chain(data, absolute, absolute + size)
        rows.append(Rec(tag, rid, None, absolute, size, absolute + size, members))
    extent = max(row.end for row in rows)
    return Pkg("pk1", start, extent, -1, rows, header_size, count)


def parse_pack(data: bytes, start: int) -> Pkg:
    if data[start:start + 4] != b"PACK":
        raise LayoutFail(f"PACK magic missing at 0x{start:X}")
    low, count = u16(data, start + 4), u16(data, start + 6)
    header_size = u32(data, start + 8)
    if low != 0 or not 1 <= count <= 1000:
        raise LayoutFail(f"unsupported PACK header at 0x{start:X}")
    if header_size < 0x10 + 4 + (count - 1) * 8 or start + header_size > len(data):
        raise LayoutFail(f"bad PACK header size at 0x{start:X}")
    offsets = [start + header_size]
    auxes = [u32(data, start + 0xC)]
    for k in range(count - 1):
        offsets.append(start + u32(data, start + 0x10 + k * 8))
        auxes.append(u32(data, start + 0x14 + k * 8))
    used = 0x14 + (count - 1) * 8
    if not is_zero(data[start + used:start + header_size]):
        raise LayoutFail(f"non-zero PACK header tail at 0x{start:X}")
    for a, b in zip(offsets, offsets[1:]):
        if not (start < a < b <= len(data)):
            raise LayoutFail(f"PACK offsets not ascending at 0x{start:X}")
    if any((off - start) % 0x80 for off in offsets):
        raise LayoutFail(f"PACK offsets not 0x80-aligned at 0x{start:X}")
    tail = data[offsets[-1]:]
    content_end = offsets[-1] + max(len(tail.rstrip(b"\0")), 1)
    rows: list[Rec] = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else content_end
        members = None
        if data[offset:offset + 3] == b"SLZ":
            members = walk_chain(data, offset, end)
        rows.append(Rec(None, index, auxes[index], offset, None, end, members))
    return Pkg("pack", start, content_end, -1, rows, header_size, count)


def find_boundary(data: bytes, extent: int) -> int:
    pos = extent
    n = len(data)
    step = 1 << 20
    nonzero = -1
    while pos < n:
        block = data[pos:pos + step]
        if block.count(0) != len(block):
            for i, value in enumerate(block):
                if value:
                    nonzero = pos + i
                    break
            break
        pos += len(block)
    if nonzero < 0:
        return n
    quad = nonzero & ~3
    for candidate in (quad - 4, quad):
        if candidate < extent:
            continue
        if not is_zero(data[extent:candidate]):
            break
        head = data[candidate:candidate + 4]
        try:
            if head[:3] == b"SLZ":
                walk_chain(data, candidate, n)
                return candidate
            if head == b"PACK":
                parse_pack(data, candidate)
                return candidate
            if candidate + 16 <= n and u32(data, candidate) == 0:
                parse_pk1(data, candidate)
                return candidate
        except LayoutFail:
            continue
    raise LayoutFail(
        f"unrecognized data after package extent 0x{extent:X} "
        f"(first non-zero at 0x{nonzero:X})")


def parse_layout(data: bytes) -> list[Pkg]:
    packages: list[Pkg] = []
    cursor = 0
    while cursor < len(data):
        head = data[cursor:cursor + 4]
        if head[:3] == b"SLZ":
            members = walk_chain(data, cursor, len(data))
            extent = members[-1].offset + 16 + members[-1].comp
            package = Pkg("chain", cursor, extent, -1,
                          [Rec(None, None, None, cursor, None, extent, members)])
        elif head == b"PACK":
            package = parse_pack(data, cursor)
            package.boundary = len(data)
            if not is_zero(data[package.extent:]):
                raise LayoutFail("non-zero bytes after PACK content")
            packages.append(package)
            return packages
        elif len(head) == 4 and u32(data, cursor) == 0:
            package = parse_pk1(data, cursor)
        else:
            raise LayoutFail(
                f"unrecognized package at 0x{cursor:X}: {data[cursor:cursor + 16].hex()}")
        package.boundary = find_boundary(data, package.extent)
        packages.append(package)
        cursor = package.boundary
    return packages


# ---------------------------------------------------------------------------
# Nanum rendering (independent, verify_hyda parameters)
# ---------------------------------------------------------------------------

class Renderer:
    def __init__(self, font_path: Path) -> None:
        self.font = ImageFont.truetype(str(font_path), FONT_PIXEL_SIZE)
        self.cache: dict[str, tuple[int, bytes]] = {}

    def render(self, character: str) -> tuple[int, bytes]:
        cached = self.cache.get(character)
        if cached is not None:
            return cached
        bbox = self.font.getbbox(character)
        if bbox is None:
            raise VerifyFatal(f"font cannot render {character!r}")
        advance = max(1, min(GLYPH_CELL, round(self.font.getlength(character))))
        image = Image.new("L", (GLYPH_CELL, GLYPH_CELL), 0)
        draw = ImageDraw.Draw(image)
        ink_w, ink_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (GLYPH_CELL - ink_w) // 2 - bbox[0])
        y = max(0, (GLYPH_CELL - ink_h) // 2 - bbox[1])
        draw.text((x, y), character, font=self.font, fill=255)
        pixels = [
            round(value / 255 * (GRAY_LEVELS - 1)) * 15 // (GRAY_LEVELS - 1)
            for value in image.getdata()
        ]
        packed = bytearray()
        for index in range(0, len(pixels), 2):
            packed.append(pixels[index] | (pixels[index + 1] << 4))
        result = (advance, bytes(packed))
        self.cache[character] = result
        return result


# ---------------------------------------------------------------------------
# width engine (mirrors the delivery gate semantics; local widths come from
# the PATCHED atlas — the on-disc ground truth)
# ---------------------------------------------------------------------------

class WidthState:
    __slots__ = ("scale", "spacing")

    def __init__(self) -> None:
        self.scale = 1.0
        self.spacing = 0.0

    def control(self, fam: int, raw: bytes) -> None:
        if fam == SCALE_FAM and len(raw) == 6:
            value = struct.unpack_from("<f", raw, 2)[0]
            if value != 0.0:
                self.scale = value
        elif fam == SCALE_RESET_FAM:
            self.scale = 1.0
        elif fam == ADVANCE_FAM and len(raw) == 6:
            self.spacing = struct.unpack_from("<f", raw, 2)[0]
        elif fam == ADVANCE_RESET_FAM:
            self.spacing = 0.0

    def advance(self, width: int) -> int:
        mult = self.scale if 0.0 < self.scale <= 4.0 else 1.0
        add = self.spacing if 0.0 <= self.spacing <= 128.0 else 0.0
        return int(round(width * mult + add))


# ---------------------------------------------------------------------------
# expectations (ground truth = the PLAN, which IS the statement of what was
# patched after build_full_plan -> plan_fixups -> fit_repair).  tr_out is only
# an optional advisory cross-check; the plan is authoritative.
# ---------------------------------------------------------------------------

@dataclass
class ExpectedMessage:
    korean: str                    # plan text (inventory \n⟦P⟧ convention)
    speaker_korean: str | None
    msg_sha: str                   # inventory anchor for the ORIGINAL segment
    container_sha: str
    text_key: str | None = None    # inventory unit key (for budget/advisory)
    budget_px: int | None = None   # width gate budget (None = ungated)
    body_pages: list = field(default_factory=list)   # parsed, unshifted
    speaker_units: list | None = None                # parsed inline units
    keep_speaker: bool = False


def load_translations(tr_out_dir: Path, log: Log) -> dict[str, dict]:
    """tr_out is advisory only.  Duplicate keys across split batches are the
    norm (build_full_plan deduped) and are recorded as warnings, not errors."""
    result: dict[str, dict] = {}
    files = sorted(glob.glob(str(tr_out_dir / "batch_*_ko.json")))
    for path in files:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warn("tr_out", os.path.basename(path), f"unreadable tr_out file: {exc}")
            continue
        for entry in doc.get("translations", []):
            key = entry.get("key")
            korean = entry.get("korean")
            if not isinstance(key, str) or not isinstance(korean, str) or not korean:
                continue
            speaker = entry.get("speaker_korean")
            # last writer wins (re-translated split batches supersede)
            result[key] = {"korean": korean,
                           "speaker_korean": speaker or None,
                           "source": os.path.basename(path)}
    return result


def load_budgets(batches_dir: Path, log: Log) -> dict[str, int]:
    """text_key -> delivery width budget (px), read from every tr_batch."""
    budgets: dict[str, int] = {}
    for path in sorted(glob.glob(str(batches_dir / "batch_*.json"))):
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warn("inputs", os.path.basename(path), f"unreadable batch file: {exc}")
            continue
        for unit in doc.get("units", []):
            key = unit.get("key")
            if key and key not in budgets:
                budget = unit.get("budget_px")
                if isinstance(budget, int) and budget > 0:
                    budgets[key] = budget
    return budgets


def load_units(path: Path) -> dict[str, dict]:
    units: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            unit = json.loads(line)
            units[unit["key"]] = unit
    return units


def load_catalog_occurrences(path: Path) -> dict[str, list[tuple[int, int]]]:
    occurrences: dict[str, list[tuple[int, int]]] = defaultdict(list)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            occurrences[row["file_sha256"].lower()].append(
                (int(row["archive_id"]), int(row["stream_id"])))
    return dict(occurrences)


def load_manifest(path: Path) -> dict[tuple[int, int], dict]:
    rows: dict[tuple[int, int], dict] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows[(int(row["archive_id"]), int(row["stream_id"]))] = {
                "depth": int(row["depth"]),
                "source_offset": int(row["source_offset"]) if row["source_offset"] else -1,
                "mode": int(row["mode"]) if row["mode"] else -1,
            }
    return rows


def index_inventory(inventory: dict) -> tuple[
        dict[tuple[int, int], str],
        dict[str, dict[int, dict]],
        dict[str, dict]]:
    """Return:
      stream_to_sha : (archive, stream) -> container file_sha256
      msgs_by_sha   : file_sha256 -> {msgid: {"sha": exact, "text_key": key}}
      container_by_sha : file_sha256 -> container record (for jp counts etc.)
    """
    stream_to_sha: dict[tuple[int, int], str] = {}
    msgs_by_sha: dict[str, dict[int, dict]] = {}
    container_by_sha: dict[str, dict] = {}
    for container in inventory["containers"]:
        sha = container["file_sha256"].lower()
        container_by_sha[sha] = container
        for ref in container["refs"]:
            stream_to_sha[(ref["archive"], ref["stream"])] = sha
        table: dict[int, dict] = {}
        for message in container["messages"]:
            table[int(message["id"])] = {
                "sha": message["sha"].lower(),
                "text_key": message.get("text_key"),
            }
        msgs_by_sha[sha] = table
    return stream_to_sha, msgs_by_sha, container_by_sha


def build_expectations_from_plan(
    plan: dict[tuple[int, int], dict[int, dict]],
    inventory: dict,
    budgets: dict[str, int],
    log: Log,
) -> tuple[dict[tuple[int, int], dict[int, ExpectedMessage]], dict]:
    """The plan IS the specification of what was patched.  Build
    (archive, stream) -> msgid -> ExpectedMessage straight from it; every
    planned message must decode to the plan's {korean, speaker_korean}.

    Messages NOT in the plan are intentionally left original (fit_repair drops
    + non-JP): they are verified byte-identical inside verify_container as
    non-target messages, so they need no expectation here.
    """
    stream_to_sha, msgs_by_sha, container_by_sha = index_inventory(inventory)
    expected: dict[tuple[int, int], dict[int, ExpectedMessage]] = defaultdict(dict)
    stats = {
        "planned_streams": len(plan),
        "planned_messages": 0,
        "planned_containers": 0,
        "budget_missing": 0,
        "expectation_errors": 0,
    }
    seen_containers: set[str] = set()

    for (archive_id, stream_id), messages in sorted(plan.items()):
        container_sha = stream_to_sha.get((archive_id, stream_id))
        if container_sha is None:
            log.err("plan", f"{archive_id}:{stream_id}",
                    "planned stream not found in the inventory")
            continue
        seen_containers.add(container_sha)
        inv_msgs = msgs_by_sha.get(container_sha, {})
        for mid, entry in messages.items():
            info = inv_msgs.get(mid)
            if info is None:
                log.err("plan", f"{archive_id}:{stream_id}:{mid}",
                        "planned message id absent from the inventory container")
                stats["expectation_errors"] += 1
                continue
            korean = entry.get("korean")
            if not isinstance(korean, str) or not korean:
                log.err("plan", f"{archive_id}:{stream_id}:{mid}",
                        "plan entry has no korean text")
                stats["expectation_errors"] += 1
                continue
            text_key = info["text_key"]
            budget = budgets.get(text_key) if text_key else None
            if budget is None:
                stats["budget_missing"] += 1
            exp = ExpectedMessage(
                korean=korean,
                speaker_korean=entry.get("speaker_korean"),
                msg_sha=info["sha"],
                container_sha=container_sha,
                text_key=text_key,
                budget_px=budget,
                keep_speaker=bool(entry.get("keep_speaker")),
            )
            try:
                exp.body_pages = parse_expected_body(exp.korean)
                if not exp.keep_speaker and exp.speaker_korean is not None:
                    non_g = [t for t in TOKEN_RE.findall(exp.speaker_korean)
                             if not (t.startswith("G:")
                                     and G_SHA8_RE.fullmatch(t[2:].lower()))]
                    if non_g:
                        raise TextError(
                            f"speaker has unresolvable tokens: {exp.speaker_korean!r}")
                    exp.speaker_units = parse_expected_inline(exp.speaker_korean)
            except TextError as exc:
                log.err("plan", f"{archive_id}:{stream_id}:{mid}", str(exc))
                stats["expectation_errors"] += 1
                continue
            markers = expected_markers_in_order(exp.body_pages)
            if markers != list(range(1, len(markers) + 1)):
                log.err("plan", f"{archive_id}:{stream_id}:{mid}",
                        "plan markers are not exactly 1..N ascending", markers=markers)
                stats["expectation_errors"] += 1
                continue
            expected[(archive_id, stream_id)][mid] = exp
            stats["planned_messages"] += 1
    stats["planned_containers"] = len(seen_containers)
    return dict(expected), stats


def crosscheck_tr_out(
    translations: dict[str, dict],
    expected: dict[tuple[int, int], dict[int, ExpectedMessage]],
    log: Log,
) -> dict:
    """Optional advisory: where a planned message's inventory text_key has a
    tr_out translation, flag (as warnings) any text divergence.  Divergence is
    expected and harmless after plan_fixups/fit_repair — this is informational
    only and never affects the exit code."""
    stats = {"compared": 0, "text_divergent": 0, "speaker_divergent": 0}
    for (archive_id, stream_id), messages in expected.items():
        for mid, exp in messages.items():
            entry = translations.get(exp.text_key or "")
            if entry is None:
                continue
            stats["compared"] += 1
            if entry["korean"] != exp.korean:
                stats["text_divergent"] += 1
            if (entry.get("speaker_korean") or None) != (exp.speaker_korean or None):
                stats["speaker_divergent"] += 1
    if stats["text_divergent"] or stats["speaker_divergent"]:
        log.warn("tr_out", "advisory",
                 "tr_out diverges from the plan (expected after "
                 "plan_fixups/fit_repair)", **stats)
    return stats


# ---------------------------------------------------------------------------
# plan loading and cross-check
# ---------------------------------------------------------------------------

def load_plan(
    plan_path: Path,
    catalog: dict[str, list[tuple[int, int]]],
    log: Log,
) -> dict[tuple[int, int], dict[int, dict]]:
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    plan: dict[tuple[int, int], dict[int, dict]] = {}
    for stream_key, messages in (document.get("streams") or {}).items():
        archive_id, stream_id = (int(v) for v in stream_key.split(":"))
        plan[(archive_id, stream_id)] = {int(mid): dict(v) for mid, v in messages.items()}
    for sha, messages in (document.get("unique") or {}).items():
        occurrences = catalog.get(sha.lower())
        if not occurrences:
            log.err("plan", sha[:12], "unique plan sha absent from container catalog")
            continue
        for occurrence in occurrences:
            if occurrence in plan:
                log.err("plan", f"{occurrence[0]}:{occurrence[1]}", "stream planned twice")
                continue
            plan[occurrence] = {int(mid): dict(v) for mid, v in messages.items()}
    return plan


def canonical_pages(pages: list) -> list:
    return [[list(line) for line in page] for page in pages]


# ---------------------------------------------------------------------------
# container verification (checks 5-7)
# ---------------------------------------------------------------------------

def conservative_local_refs(data: bytes, local_base: int, glyph_count: int) -> set[int]:
    """Every possible local-slot reference, scanning at every byte offset
    (over-protects, never under-protects)."""
    upper = local_base + glyph_count
    protected: set[int] = set()
    n = len(data)
    for off in range(n):
        b0 = data[off]
        if b0 == 0:
            continue
        if b0 < 0x80:
            code = b0
        elif off + 1 < n and data[off + 1] < 0x80:
            code = (b0 & 0x7F) | (data[off + 1] << 7)
        else:
            continue
        if local_base <= code < upper:
            protected.add(code)
    return protected


@dataclass
class ContainerResult:
    ok: bool
    verified_message_ids: list
    characters_rendered: int
    g_bitmaps_verified: int
    width_lines_checked: int
    errors: list  # (check, where, message)
    width_lines_unbudgeted: int = 0


def verify_container(
    where: str,
    original: bytes,
    patched: bytes,
    expects: dict[int, ExpectedMessage],
    plan_entries: dict[int, dict] | None,
    ctl: Controls,
    renderer: Renderer,
    global_widths: bytes,
) -> ContainerResult:
    errors: list[tuple[str, str, str]] = []

    def err(check: str, sub: str, message: str) -> None:
        errors.append((check, f"{where}:{sub}", message))

    result = ContainerResult(False, [], 0, 0, 0, errors)
    try:
        o = Mclib.parse(original)
        p = Mclib.parse(patched)
    except ValueError as exc:
        err("container", "-", f"mclib parse failed: {exc}")
        return result

    if original[:0x10] != patched[:0x10]:
        err("container", "-", "mclib magic/version changed")
    if (o.glyph_width, o.glyph_height, o.glyph_stride) != (
            p.glyph_width, p.glyph_height, p.glyph_stride):
        err("container", "-", "glyph geometry changed")
    if (p.glyph_width, p.glyph_height, p.glyph_stride) != (24, 24, 24):
        err("container", "-", f"unsupported geometry {p.glyph_width}x{p.glyph_height}")
    if o.local_base != p.local_base:
        err("container", "-", "local_base changed")
    if o.mapping_count != p.mapping_count:
        err("container", "-", "mapping count changed")
    if p.glyph_count < o.glyph_count:
        err("container", "-", "local glyph table shrank")
    if [mid for mid, _ in o.rows] != [mid for mid, _ in p.rows]:
        err("container", "-", "message-id table changed")
    if errors:
        return result

    o_by_id: dict[int, list[int]] = defaultdict(list)
    p_by_id: dict[int, list[int]] = defaultdict(list)
    for mid, off in o.rows:
        o_by_id[mid].append(off)
    for mid, off in p.rows:
        p_by_id[mid].append(off)

    target_ids = set(expects)
    for mid in sorted(target_ids):
        if len(o_by_id.get(mid, ())) != 1 or len(p_by_id.get(mid, ())) != 1:
            err("container", str(mid), "target message id missing or ambiguous")
    if errors:
        return result

    # non-target messages logically identical + protection scan
    protected: set[int] = set()
    non_target_offsets = []
    for mid, offsets in o_by_id.items():
        for position, off in enumerate(offsets):
            if mid in target_ids and len(offsets) == 1:
                continue
            non_target_offsets.append((mid, off, p_by_id[mid][position]))
    for mid, o_off, p_off in non_target_offsets:
        if logical_segment(o.segments[o_off]) != logical_segment(p.segments[p_off]):
            err("container", str(mid), "non-target message bytecode changed")
        protected.update(conservative_local_refs(
            o.segments[o_off], o.local_base, o.glyph_count))
    if errors:
        return result

    # analyze originals, collect preserved-chunk protection
    analyses: dict[int, Analysis] = {}
    for mid in sorted(target_ids):
        seg = o.segments[o_by_id[mid][0]]
        exp = expects[mid]
        if sha256(seg) != exp.msg_sha:
            err("container", str(mid), "original message hash mismatch vs inventory")
            continue
        try:
            analysis = analyze_message(seg, ctl)
        except TokenError as exc:
            err("container", str(mid), f"original tokenize failed: {exc}")
            continue
        analyses[mid] = analysis
        chunks = [analysis.prefix_raw, analysis.delim_raw]
        if exp.keep_speaker and analysis.field_tokens is not None:
            chunks.append(analysis.field_raw)
        for page in analysis.pages:
            for line in page:
                for token in line.tokens:
                    if token[0] == "c" and token[1] not in DROP_FAMS:
                        chunks.append(token[2])
        for chunk in chunks:
            if chunk:
                protected.update(conservative_local_refs(chunk, o.local_base, o.glyph_count))
    if errors:
        return result

    # protected glyphs byte-identical
    for code in sorted(protected):
        index = code - o.local_base
        if not 0 <= index < o.glyph_count:
            continue
        if o.widths[index] != p.widths[index]:
            err("font", f"code{code}", "protected glyph width changed")
        if (o.bitmaps[index * GLYPH_BYTES:(index + 1) * GLYPH_BYTES]
                != p.bitmaps[index * GLYPH_BYTES:(index + 1) * GLYPH_BYTES]):
            err("font", f"code{code}", "protected glyph bitmap changed")

    # expected charset (check 6) and decode maps
    use_global = o.local_base > MAX_GLOBAL_CODE
    charset: set[str] = set()
    for exp in expects.values():
        charset.update(strip_marker_chars(exp.korean))
        if exp.speaker_units is not None:
            charset.update(ch for kind, ch in exp.speaker_units if kind == "char")
    charset.discard("\n")
    if use_global:
        charset -= set(GLOBAL_CODE_MAP)

    # A signature is (advance width, 4bpp bitmap).  At 22 px with two gray
    # levels several *distinct* characters legitimately render to the SAME
    # signature ('I'/'l', small Greek letters, '–'/'−', '・'…): the console
    # displays identical pixels, so they are indistinguishable by design.  We
    # therefore group by signature and verify pixel-equivalence, not character
    # identity -- each signature maps to a canonical representative and both the
    # atlas glyphs and the expected characters are reduced to that canon.
    sig_to_canon: dict[tuple[int, bytes], str] = {}
    for character in sorted(charset):
        sig_to_canon.setdefault(renderer.render(character), character)

    def canon(character: str) -> str:
        # Global-atlas characters decode literally through GLOBAL_CHARACTER_MAP,
        # never through the local atlas, so they must not be folded into a local
        # render-equivalence class (e.g. '・' shares a 2-gray bitmap with a small
        # Greek letter but is emitted as global code 239).
        if use_global and character in GLOBAL_CODE_MAP:
            return character
        return sig_to_canon.get(renderer.render(character), character)

    def canon_units(units: list) -> list:
        return [("char", canon(value)) if kind == "char" else (kind, value)
                for kind, value in units]

    result.characters_rendered = len(charset)

    original_bitmap_index: dict[str, list[int]] = defaultdict(list)
    for index in range(o.glyph_count):
        digest = sha256(o.bitmaps[index * GLYPH_BYTES:(index + 1) * GLYPH_BYTES])
        original_bitmap_index[digest].append(index)

    code_map: dict[int, tuple] = {}
    present_sigs: set[tuple[int, bytes]] = set()
    g_slots = 0
    for index in range(p.glyph_count):
        bitmap = bytes(p.bitmaps[index * GLYPH_BYTES:(index + 1) * GLYPH_BYTES])
        width = p.widths[index]
        code = p.local_base + index
        canon_char = sig_to_canon.get((width, bitmap))
        digest = sha256(bitmap)
        original_slots = original_bitmap_index.get(digest, [])
        g_match = any(o.widths[oi] == width for oi in original_slots)
        if canon_char is not None:
            # a Nanum-rendered Korean/Latin slot (may double as an unchanged
            # original bitmap when the two happen to render identically; the
            # Korean mapping is what the message references)
            code_map[code] = ("char", canon_char)
            present_sigs.add((width, bitmap))
        elif g_match:
            code_map[code] = ("gtok", digest[:8])
            g_slots += 1
    for character in sorted(charset):
        if renderer.render(character) not in present_sigs:
            err("font", repr(character), "expected Nanum glyph absent from patched atlas")
    result.g_bitmaps_verified = g_slots

    def decode_glyph(code: int, sub: str) -> tuple | None:
        if use_global and code < o.local_base:
            character = GLOBAL_CHARACTER_MAP.get(code)
            if character is None:
                err("decode", sub, f"unverifiable global code {code}")
                return None
            return ("char", character)
        mapped = code_map.get(code)
        if mapped is None:
            err("decode", sub, f"unverified patched glyph code {code}")
        return mapped

    # per-message verification
    for mid in sorted(target_ids):
        exp = expects[mid]
        analysis = analyses[mid]
        sub = str(mid)
        p_seg = p.segments[p_by_id[mid][0]]
        # NOTE: the patched bytecode may be byte-identical to the original when
        # the patcher reuses the same local slots and only replaces their
        # bitmaps (e.g. codes 301/302 now carry Korean glyphs).  Identity is
        # therefore NOT a "did not change" signal; the decode-back below is the
        # real check -- a genuinely unpatched message decodes to the original
        # Japanese and fails the Korean comparison.
        try:
            pa = analyze_message(p_seg, ctl)
        except TokenError as exc:
            err("decode", sub, f"patched tokenize failed: {exc}")
            continue

        # ----- expected structure (with variant speaker expansion)
        body_pages = canonical_pages(exp.body_pages)
        speaker_expect = exp.speaker_units
        keep_speaker = exp.keep_speaker
        if analysis.field_tokens is None and analysis.variant_prefix is not None \
                and exp.speaker_korean is not None:
            # variant pattern: speaker becomes body-line-0 markers/literals
            shift = sum(1 for t in analysis.variant_prefix
                        if t[0] == "c" and t[1] in POSITIONAL_FAMS)
            if keep_speaker or speaker_expect is None:
                err("decode", sub, "variant speaker pattern cannot be kept verbatim")
                continue
            line0: list[tuple] = []
            next_marker = 1
            for token in analysis.variant_prefix:
                if token[1] in POSITIONAL_FAMS:
                    line0.append(("pos", next_marker))
                    next_marker += 1
                elif token[1] == NAME_FAM:
                    line0.extend(speaker_expect)
                # a180 is positional; anything else here is invisible
            body_pages = shift_markers(body_pages, shift)
            if not body_pages:
                body_pages = [[]]
            body_pages[0].insert(0, line0)
            speaker_expect = None
            keep_speaker = False
        elif analysis.field_tokens is None and exp.speaker_korean is not None \
                and not exp.keep_speaker:
            err("decode", sub, "speaker_korean given but original has no speaker construct")
            continue
        if analysis.field_tokens is not None and exp.speaker_korean is None \
                and not exp.keep_speaker:
            err("decode", sub, "original has a speaker construct but no speaker_korean")
            continue

        # marker completeness vs original positional controls
        markers = expected_markers_in_order(body_pages)
        if markers != list(range(1, len(analysis.positional_raws) + 1)):
            err("decode", sub,
                "expected markers do not cover the original positional controls")
            continue

        # ----- structural identity
        if analysis.field_tokens is not None:
            if pa.field_tokens is None:
                err("decode", sub, "patched message lost its speaker construct")
                continue
            o_prefix_field = analysis.prefix_raw
            if pa.prefix_raw != o_prefix_field:
                err("decode", sub, "speaker prefix bytes changed")
            if pa.delim_raw != analysis.delim_raw:
                err("decode", sub, "speaker/body delimiter bytes changed")
            if keep_speaker:
                if pa.field_raw != analysis.field_raw:
                    err("decode", sub, "kept speaker field changed")
            else:
                decoded_speaker: list[tuple] = []
                speaker_ok = True
                for token in pa.field_tokens:
                    if token[0] != "g":
                        err("decode", sub, "patched speaker field is not literal glyphs")
                        speaker_ok = False
                        break
                    decoded = decode_glyph(token[1], sub)
                    if decoded is None:
                        speaker_ok = False
                        break
                    decoded_speaker.append(decoded)
                if not speaker_ok:
                    continue
                if decoded_speaker != canon_units(speaker_expect or []):
                    err("decode", sub, "speaker decode mismatch")
                    continue
        else:
            if pa.field_tokens is not None:
                err("decode", sub, "patched message gained a speaker construct")
                continue

        if len(pa.pages) != len(analysis.pages):
            err("decode", sub, "page count changed")
            continue
        page_line_mismatch = False
        for p_index, (o_page, p_page) in enumerate(zip(analysis.pages, pa.pages)):
            if len(o_page) != len(p_page):
                err("decode", sub, f"line count changed on page {p_index}")
                page_line_mismatch = True
        if page_line_mismatch:
            continue

        # control signature: invisibles per line, positionals in reading order
        signature_ok = True
        for p_index, (o_page, p_page) in enumerate(zip(analysis.pages, pa.pages)):
            for l_index, (o_line, p_line) in enumerate(zip(o_page, p_page)):
                if o_line.invisible != p_line.invisible:
                    err("decode", sub,
                        f"invisible control signature changed at page {p_index} "
                        f"line {l_index}")
                    signature_ok = False
        if pa.positional_raws != analysis.positional_raws:
            err("decode", sub, "positional control sequence/operands changed")
            signature_ok = False
        if pa.drop_count:
            err("decode", sub, "dropped-family control (9080/9180/9380) present in body")
            signature_ok = False
        if not signature_ok:
            continue

        # decode-back: patched visible text == expected.  Lines are decoded
        # and measured in one deterministic pass over the whole patched token
        # stream (prefix+field+delim+body), so scale and spacing controls
        # apply exactly where they sit on disc.
        marker_counter = 0
        decode_ok = True
        width_state = WidthState()
        line_px: dict[tuple[int, int], int] = {}
        line_fams: dict[tuple[int, int], list[int]] = {}
        consumed = 0
        if pa.field_tokens is not None:
            head_len = 0
            head_raw = pa.prefix_raw + pa.field_raw + pa.delim_raw
            acc = b""
            for token in pa.tokens:
                if len(acc) >= len(head_raw):
                    break
                acc += token[2]
                head_len += 1
            if acc != head_raw:
                err("decode", sub, "internal: head token accounting failed")
                continue
            for token in pa.tokens[:head_len]:
                if token[0] == "c":
                    width_state.control(token[1], token[2])
                # speaker glyph widths are not gated
            consumed = head_len
        body_tokens = pa.tokens[consumed:]
        page_index = 0
        line_index = 0
        current_line: list[tuple] = []
        current_px = 0
        current_fams: list[int] = []
        decoded_pages = [[]]

        def flush_line() -> None:
            nonlocal current_line, current_px, current_fams
            decoded_pages[-1].append(current_line)
            line_px[(page_index, line_index)] = current_px
            line_fams[(page_index, line_index)] = current_fams
            current_line, current_px, current_fams = [], 0, []

        for token in body_tokens:
            if token[0] == "c" and token[1] == PAGE_FAM:
                flush_line()
                decoded_pages.append([])
                page_index += 1
                line_index = 0
            elif token[0] == "c" and token[1] == NEWLINE_FAM:
                flush_line()
                line_index += 1
            elif token[0] == "g":
                decoded = decode_glyph(token[1], sub)
                if decoded is None:
                    decode_ok = False
                    break
                current_line.append(decoded)
                code = token[1]
                if use_global and code < p.local_base:
                    width = global_widths[code - 1] if 0 <= code - 1 < len(global_widths) else 24
                else:
                    gi = code - p.local_base
                    width = p.widths[gi] if 0 <= gi < p.glyph_count else 24
                current_px += width_state.advance(width)
            elif token[1] in POSITIONAL_FAMS:
                marker_counter += 1
                current_line.append(("pos", marker_counter))
                current_fams.append(token[1])
            else:
                width_state.control(token[1], token[2])
        if not decode_ok:
            continue
        flush_line()

        if len(body_pages) > len(decoded_pages):
            err("decode", sub, "expected more pages than the original structure")
            continue
        text_mismatch = False
        for pg in range(len(decoded_pages)):
            expected_lines = body_pages[pg] if pg < len(body_pages) else []
            decoded_lines = decoded_pages[pg]
            if len(expected_lines) > len(decoded_lines):
                err("decode", sub, f"expected more lines than original on page {pg}")
                text_mismatch = True
                break
            for ln in range(len(decoded_lines)):
                expected_units = expected_lines[ln] if ln < len(expected_lines) else []
                if decoded_lines[ln] != canon_units(expected_units):
                    err("decode", sub,
                        f"Korean body mismatch at page {pg} line {ln}")
                    text_mismatch = True
                    break
            if text_mismatch:
                break
        if text_mismatch:
            continue

        # width gate (check 7) — from the PATCHED atlas widths.  When no
        # delivery budget is available for this message's unit the line width
        # is not gated (tracked as width_lines_unbudgeted).
        budget = exp.budget_px
        if budget is None:
            result.width_lines_unbudgeted += len(line_px)
            result.verified_message_ids.append(mid)
            continue
        for (pg, ln), px in sorted(line_px.items()):
            allowance = sum(MARKER_ALLOWANCE_PX.get(fam, 0)
                            for fam in line_fams[(pg, ln)])
            result.width_lines_checked += 1
            if px + allowance > budget:
                err("width", sub,
                    f"line exceeds budget at page {pg} line {ln}: "
                    f"{px}+{allowance} > {budget}")

        result.verified_message_ids.append(mid)

    result.ok = not errors
    return result


# ---------------------------------------------------------------------------
# archive verification (check 4 wrapper)
# ---------------------------------------------------------------------------

def locate_member(packages: list[Pkg], source_offset: int):
    for p_index, package in enumerate(packages):
        for r_index, row in enumerate(package.rows):
            if row.members is None:
                continue
            for c_index, member in enumerate(row.members):
                if member.offset == source_offset:
                    return p_index, r_index, c_index
    return None


def _tail_ok(patched: bytes, original: bytes, start: int, end: int, moved: bool) -> bool:
    """Padding bytes after a member/record are ignored by the engine (it reads
    only `comp` bytes).  A safe, corruption-catching invariant: when the record
    was shifted (strategy B) every padding byte must be zero; when it stayed in
    place (strategy A) each byte must be zero OR byte-identical to the original
    at that offset.  In-place growth-cap zeroing leaves the ORIGINAL record's
    own trailing padding untouched, so the region is legitimately a mix of
    fresh zeros and surviving original bytes."""
    region = patched[start:end]
    if is_zero(region):
        return True
    if moved:
        return False
    original_region = original[start:end]
    return all(pb == 0 or pb == ob for pb, ob in zip(region, original_region))


def verify_archive(
    archive_id: int,
    original: bytes,
    patched: bytes,
    targets: dict[int, tuple[int, dict[int, ExpectedMessage], dict[int, dict] | None]],
    ctl: Controls,
    renderer: Renderer,
    global_widths: bytes,
    log: Log,
    memo: dict,
    container_totals: dict,
    verified_occurrences: set,
) -> dict:
    """targets: source_offset -> (stream_id, expects, plan_entries)."""
    where = f"archive{archive_id}"
    report: dict = {"archive_id": archive_id, "bytes": len(original)}
    if len(original) != len(patched):
        log.err("archive", where, "archive length changed")
        return report
    try:
        o_pkgs = parse_layout(original)
    except LayoutFail as exc:
        log.err("archive", where, f"original layout parse failed: {exc}")
        return report
    try:
        p_pkgs = parse_layout(patched)
    except LayoutFail as exc:
        log.err("archive", where, f"patched layout parse failed: {exc}")
        return report
    if len(o_pkgs) != len(p_pkgs):
        log.err("archive", where, "package count changed")
        return report

    address_of: dict[int, tuple[int, int, int]] = {}
    for source_offset in targets:
        address = locate_member(o_pkgs, source_offset)
        if address is None:
            log.err("archive", where, f"no SLZ member at source offset 0x{source_offset:X}")
        else:
            address_of[source_offset] = address
    targeted_packages: dict[int, dict[tuple[int, int], int]] = defaultdict(dict)
    for source_offset, (p_index, r_index, c_index) in address_of.items():
        targeted_packages[p_index][(r_index, c_index)] = source_offset

    packages_report = []
    for p_index, (o_pkg, p_pkg) in enumerate(zip(o_pkgs, p_pkgs)):
        sub = f"{where}:pkg{p_index}"
        if (o_pkg.kind, o_pkg.start, o_pkg.boundary) != (p_pkg.kind, p_pkg.start, p_pkg.boundary):
            log.err("archive", sub, "package identity/boundary changed")
            continue
        if (o_pkg.header_size, o_pkg.count) != (p_pkg.header_size, p_pkg.count):
            log.err("archive", sub, "package header shape changed")
            continue
        if p_index not in targeted_packages:
            if original[o_pkg.start:o_pkg.boundary] != patched[p_pkg.start:p_pkg.boundary]:
                log.err("archive", sub, "untargeted package changed")
            continue
        if len(o_pkg.rows) != len(p_pkg.rows):
            log.err("archive", sub, "row count changed")
            continue

        # header identity fields
        if o_pkg.kind == "pk1":
            for r_index in range(o_pkg.count):
                o_entry = original[o_pkg.start + 0x10 + r_index * 16:
                                   o_pkg.start + 0x10 + r_index * 16 + 8]
                p_entry = patched[p_pkg.start + 0x10 + r_index * 16:
                                  p_pkg.start + 0x10 + r_index * 16 + 8]
                if o_entry != p_entry:
                    log.err("archive", sub, f"PK1 row {r_index} tag/id changed")
            if original[o_pkg.start:o_pkg.start + 0x10] != patched[p_pkg.start:p_pkg.start + 0x10]:
                log.err("archive", sub, "PK1 header prologue changed")
        elif o_pkg.kind == "pack":
            if original[o_pkg.start:o_pkg.start + 0x10] != patched[p_pkg.start:p_pkg.start + 0x10]:
                log.err("archive", sub, "PACK header prologue changed")
            for r_index, (o_row, p_row) in enumerate(zip(o_pkg.rows, p_pkg.rows)):
                if o_row.aux != p_row.aux:
                    log.err("archive", sub, f"PACK record {r_index} aux changed")
            if not is_zero(patched[p_pkg.start + 0x14 + (p_pkg.count - 1) * 8:
                                   p_pkg.start + p_pkg.header_size]):
                log.err("archive", sub, "PACK header tail not zero")

        targeted_rows: dict[int, set[int]] = defaultdict(set)
        for (r_index, c_index) in targeted_packages[p_index]:
            targeted_rows[r_index].add(c_index)

        previous_end = o_pkg.start + o_pkg.header_size if o_pkg.kind != "chain" else o_pkg.start
        p_previous_end = previous_end
        for r_index, (o_row, p_row) in enumerate(zip(o_pkg.rows, p_pkg.rows)):
            row_sub = f"{sub}:row{r_index}"
            if (o_row.tag, o_row.rid) != (p_row.tag, p_row.rid):
                log.err("archive", row_sub, "row identity changed")
                continue
            # inter-record gap in the patched package: zero or preserved
            moved = p_row.offset != o_row.offset
            if not _tail_ok(patched, original, p_previous_end, p_row.offset, moved):
                log.err("archive", row_sub, "non-zero unpreserved gap before record")
            previous_end = o_row.end
            p_previous_end = p_row.end

            terminal_pack = o_pkg.kind == "pack" and r_index == len(o_pkg.rows) - 1
            if r_index not in targeted_rows:
                o_content = original[o_row.offset:o_row.end]
                p_content = patched[p_row.offset:p_row.end]
                if terminal_pack:
                    if (p_content[:len(o_content)] != o_content
                            or not is_zero(p_content[len(o_content):])):
                        log.err("archive", row_sub, "non-target terminal record changed")
                elif o_row.size is not None:
                    if p_row.size not in (o_row.size, align4(o_row.size)):
                        log.err("archive", row_sub, "non-target record size changed")
                    elif (p_content[:o_row.size] != o_content
                          or not is_zero(p_content[o_row.size:])):
                        log.err("archive", row_sub, "non-target record content changed")
                elif p_content != o_content:
                    log.err("archive", row_sub, "non-target record content changed")
                continue

            # targeted row: member-level checks
            if o_row.members is None or p_row.members is None:
                log.err("archive", row_sub, "targeted row lost its SLZ chain")
                continue
            if len(o_row.members) != len(p_row.members):
                log.err("archive", row_sub, "member count changed")
                continue
            last = len(o_row.members) - 1
            for c_index, (o_m, p_m) in enumerate(zip(o_row.members, p_row.members)):
                member_sub = f"{row_sub}:m{c_index}"
                member_moved = p_m.offset != o_m.offset
                if c_index not in targeted_rows[r_index]:
                    o_end = o_m.offset + (o_m.span if c_index != last else 16 + o_m.comp)
                    p_end = p_m.offset + (p_m.span if c_index != last else 16 + p_m.comp)
                    if original[o_m.offset:o_end] != patched[p_m.offset:p_end]:
                        log.err("archive", member_sub, "non-target member changed")
                    continue
                # target member: SLZ header sanity + round trip
                if p_m.mode != o_m.mode:
                    log.err("archive", member_sub,
                            f"SLZ mode changed {o_m.mode} -> {p_m.mode}")
                    continue
                if c_index == last and p_m.next_rel != o_m.next_rel:
                    log.err("archive", member_sub, "terminal next_rel changed")
                    continue
                if c_index != last and p_m.next_rel < 16 + p_m.comp:
                    log.err("archive", member_sub, "next_rel cuts the payload")
                    continue
                payload = patched[p_m.offset + 16:p_m.offset + 16 + p_m.comp]
                try:
                    decoded_patched = decompress_slz_payload(payload, p_m.mode, p_m.unpacked)
                except ValueError as exc:
                    log.err("archive", member_sub, f"target SLZ decompress failed: {exc}")
                    continue
                if len(decoded_patched) != p_m.unpacked:
                    log.err("archive", member_sub, "SLZ round-trip size mismatch")
                    continue
                member_limit = (p_m.offset + p_m.span if c_index != last
                                else (p_row.end if o_pkg.kind != "chain" else p_pkg.boundary))
                if p_m.offset + 16 + p_m.comp > member_limit:
                    log.err("archive", member_sub, "member exceeds its allocation")
                    continue
                if not _tail_ok(patched, original, p_m.offset + 16 + p_m.comp,
                                member_limit, member_moved):
                    log.err("archive", member_sub, "member padding not zero/preserved")

                # container-level verification (checks 5-7)
                source_offset = targeted_packages[p_index][(r_index, c_index)]
                stream_id, expects, plan_entries = targets[source_offset]
                o_payload = original[o_m.offset + 16:o_m.offset + 16 + o_m.comp]
                try:
                    decoded_original = decompress_slz_payload(o_payload, o_m.mode, o_m.unpacked)
                except ValueError as exc:
                    log.err("archive", member_sub, f"original SLZ decompress failed: {exc}")
                    continue
                fingerprint = hashlib.sha256(json.dumps(
                    {str(mid): [expects[mid].korean, expects[mid].speaker_korean,
                                expects[mid].keep_speaker, expects[mid].budget_px]
                     for mid in sorted(expects)}, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                memo_key = (sha256(decoded_original), sha256(decoded_patched), fingerprint)
                cached = memo.get(memo_key)
                container_where = f"{where}:s{stream_id}"
                if cached is None:
                    cached = verify_container(
                        container_where, decoded_original, decoded_patched, expects,
                        plan_entries, ctl, renderer, global_widths)
                    memo[memo_key] = cached
                    container_totals["unique_verified"] += 1
                    container_totals["characters_rendered"] += cached.characters_rendered
                    container_totals["g_bitmaps_verified"] += cached.g_bitmaps_verified
                    container_totals["width_lines_checked"] += cached.width_lines_checked
                    container_totals["width_lines_unbudgeted"] += cached.width_lines_unbudgeted
                else:
                    container_totals["memo_hits"] += 1
                for check, sub_where, message in cached.errors:
                    log.err(check, sub_where, message)
                # Per-message success is independent of whole-container ok: a
                # message that passed all its own checks counts as applied even
                # if a sibling message in the same container failed.
                container_totals["verified_messages"] += len(cached.verified_message_ids)
                for mid in cached.verified_message_ids:
                    verified_occurrences.add((archive_id, stream_id, mid))
                if cached.ok:
                    container_totals["verified_containers"] += 1

        # gap after the last record to the boundary
        if o_pkg.kind != "pack":
            last_end = p_pkg.rows[-1].end if p_pkg.rows else p_pkg.start
            if not _tail_ok(patched, original, max(last_end, p_pkg.extent), p_pkg.boundary,
                            False):
                log.err("archive", sub, "package tail gap not zero/preserved")

        packages_report.append({
            "package": p_index, "kind": o_pkg.kind,
            "rows": len(o_pkg.rows),
            "targeted_members": len(targeted_packages.get(p_index, {})),
        })

    report["packages"] = packages_report
    report["targets"] = len(targets)
    return report


# ---------------------------------------------------------------------------
# ISO-level checks
# ---------------------------------------------------------------------------

def verify_equal_stream(orig, patch, start: int, size: int, log: Log,
                        chunk: int = 16 * 1024 * 1024) -> bool:
    orig.seek(start)
    patch.seek(start)
    remaining = size
    position = start
    ok = True
    while remaining:
        take = min(chunk, remaining)
        a = orig.read(take)
        b = patch.read(take)
        if a != b:
            for i in range(min(len(a), len(b))):
                if a[i] != b[i]:
                    log.err("diff_scope", f"0x{position + i:X}",
                            "ISO difference outside planned archive extents")
                    break
            ok = False
            break
        remaining -= take
        position += take
    return ok


def verify_diff_scope(original_path: Path, patched_path: Path,
                      extents: list[tuple[int, int, str]], log: Log,
                      iso_size: int = ORIGINAL_ISO_SIZE) -> dict:
    """extents: (start, size, label), sorted, non-overlapping."""
    total_diff = 0
    per_extent = {}
    with original_path.open("rb") as orig, patched_path.open("rb") as patch:
        cursor = 0
        for start, size, label in extents:
            if start < cursor:
                log.err("diff_scope", label, "overlapping allowed extents")
                return {"total_diff_bytes": -1}
            if not verify_equal_stream(orig, patch, cursor, start - cursor, log):
                return {"total_diff_bytes": -1}
            a = read_range(orig, start, size)
            b = read_range(patch, start, size)
            diff = count_diff_bytes(a, b)
            if diff == 0:
                log.err("diff_scope", label, "planned extent did not change")
            per_extent[label] = diff
            total_diff += diff
            cursor = start + size
        if not verify_equal_stream(orig, patch, cursor, iso_size - cursor, log):
            return {"total_diff_bytes": -1}
    return {"total_diff_bytes": total_diff, "per_extent_diff_bytes": per_extent}


# ---------------------------------------------------------------------------
# --name-patch: whitelisted extents verified via so3_name_patch delegation
# plus an independent global-font 21-slot check
# ---------------------------------------------------------------------------

def read_slz_at(handle, offset: int) -> bytes:
    header = read_range(handle, offset, 16)
    if header[:3] != b"SLZ":
        raise VerifyFatal(f"SLZ header missing at ISO offset 0x{offset:X}")
    mode, comp, unpacked = header[3], u32(header, 4), u32(header, 8)
    payload = read_range(handle, offset + 16, comp)
    return decompress_slz_payload(payload, mode, unpacked)


def name_patch_extents(np_module) -> list[tuple[int, int, str]]:
    extents = []
    for member in (np_module.FONT_MEMBER, np_module.S0068_MEMBER,
                   np_module.S1069_MEMBER):
        extents.append((member["iso_offset"],
                        member["boundary"] - member["iso_offset"],
                        f"name:{member['name']}"))
    sle = np_module.SLE_EXTENT
    extents.append((sle["iso_offset"], sle["size"], f"name:{sle['name']}"))
    return extents


def verify_name_patch_checks(original_iso: Path, patched_iso: Path,
                             np_module, log: Log) -> dict:
    report: dict = {"delegated_to": "so3_name_patch.verify_name_patch"}
    # 1) delegated content verification of all four extents
    try:
        delegated = np_module.verify_name_patch(patched_iso)
        report["verify_name_patch_ok"] = bool(delegated.get("ok"))
        report["tables"] = delegated.get("tables")
        report["font_replaced_slots"] = (delegated.get("font") or {}).get(
            "replaced_slots")
    except np_module.NamePatchError as exc:
        log.err("name_patch", "verify_name_patch", str(exc)[:600])
        report["verify_name_patch_ok"] = False
        return report
    except Exception as exc:  # noqa: BLE001 - delegation boundary
        log.err("name_patch", "verify_name_patch", f"delegation failed: {exc}")
        report["verify_name_patch_ok"] = False
        return report

    # 2) independent global-font slot audit against the documented 21 slots
    member = np_module.FONT_MEMBER
    try:
        with original_iso.open("rb") as handle:
            original_font = read_slz_at(handle, member["iso_offset"])
        with patched_iso.open("rb") as handle:
            patched_font = read_slz_at(handle, member["iso_offset"])
    except (ValueError, VerifyFatal) as exc:
        log.err("name_patch", "font", f"global font member extraction failed: {exc}")
        return report
    if sha256(original_font).upper() != str(member["sha"]).upper():
        log.err("name_patch", "font", "original global font member hash mismatch")
        return report
    if len(patched_font) != len(original_font):
        log.err("name_patch", "font", "global font member size changed")
        return report
    if not patched_font.startswith(b"so3mclib "):
        log.err("name_patch", "font", "patched global font is not an so3mclib")
        return report
    width_start = u32(original_font, 0x18)
    bitmap_start = u32(original_font, 0x1C)
    glyph_count = u32(original_font, 0x20)
    if glyph_count != 292:
        log.err("name_patch", "font", f"unexpected global glyph count {glyph_count}")
        return report
    documented_slots = sorted(code - 1 for code in NAME_PATCH_FONT_CODES.values())
    if len(documented_slots) != 21 or len(set(documented_slots)) != 21:
        raise VerifyFatal("documented name-patch slot table is inconsistent")
    allowed = set()
    for slot in documented_slots:
        allowed.add((width_start + slot, width_start + slot + 1))
        allowed.add((bitmap_start + slot * GLYPH_BYTES,
                     bitmap_start + (slot + 1) * GLYPH_BYTES))
    mask = bytearray(patched_font)
    for start, end in allowed:
        mask[start:end] = original_font[start:end]
    outside_diff = count_diff_bytes(bytes(mask), original_font)
    if outside_diff:
        log.err("name_patch", "font",
                f"{outside_diff} bytes changed outside the 21 documented slots")
    unchanged_slots = []
    for slot in documented_slots:
        old_bitmap = original_font[bitmap_start + slot * GLYPH_BYTES:
                                   bitmap_start + (slot + 1) * GLYPH_BYTES]
        new_bitmap = patched_font[bitmap_start + slot * GLYPH_BYTES:
                                  bitmap_start + (slot + 1) * GLYPH_BYTES]
        if old_bitmap == new_bitmap:
            unchanged_slots.append(slot)
    if unchanged_slots:
        log.err("name_patch", "font",
                f"documented slots not actually replaced: {unchanged_slots}")
    delegated_slots = report.get("font_replaced_slots")
    if delegated_slots is not None and sorted(delegated_slots) != documented_slots:
        log.err("name_patch", "font",
                "so3_name_patch replaced-slot set differs from the documented set",
                delegated=delegated_slots, documented=documented_slots)
    report["independent_font_audit"] = {
        "documented_slots": documented_slots,
        "bytes_changed_outside_slots": outside_diff,
        "all_21_slots_replaced": not unchanged_slots,
        "untouched_slots_verified": 292 - len(documented_slots),
    }
    return report


def run_xdelta(xdelta: Path, original: Path, patched: Path, temp_dir: Path,
               log: Log) -> dict:
    report: dict = {"tool": str(xdelta)}
    try:
        probe = subprocess.run([str(xdelta), "--help"], capture_output=True, text=True,
                               timeout=60)
        banner = (probe.stdout or "") + (probe.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warn("xdelta", str(xdelta), f"xdelta unavailable: {exc}")
        report["available"] = False
        return report
    if "xdelta" not in banner.lower():
        log.warn("xdelta", str(xdelta), "tool does not identify as xdelta")
        report["available"] = False
        return report
    report["available"] = True
    temp_dir.mkdir(parents=True, exist_ok=True)
    delta = temp_dir / "verify_full_iso.xdelta.tmp"
    redone = temp_dir / "verify_full_iso.reapply.tmp"
    try:
        for path in (delta, redone):
            path.unlink(missing_ok=True)
        encode = subprocess.run(
            [str(xdelta), "-e", "-f", "-B", str(1 << 28), "-s", str(original),
             str(patched), str(delta)],
            capture_output=True, text=True, timeout=3600)
        if encode.returncode != 0 or not delta.exists():
            log.err("xdelta", "encode", f"xdelta encode failed: {encode.stderr[:300]}")
            return report
        report["delta_bytes"] = delta.stat().st_size
        decode = subprocess.run(
            [str(xdelta), "-d", "-f", "-B", str(1 << 28), "-s", str(original),
             str(delta), str(redone)],
            capture_output=True, text=True, timeout=3600)
        if decode.returncode != 0 or not redone.exists():
            log.err("xdelta", "decode", f"xdelta decode failed: {decode.stderr[:300]}")
            return report
        if redone.stat().st_size != patched.stat().st_size:
            log.err("xdelta", "roundtrip", "re-applied ISO size mismatch")
            return report
        redone_hash = sha256_file(redone)
        patched_hash = sha256_file(patched)
        report["roundtrip_ok"] = redone_hash == patched_hash
        if redone_hash != patched_hash:
            log.err("xdelta", "roundtrip", "re-applied ISO is not byte-identical")
    finally:
        delta.unlink(missing_ok=True)
        redone.unlink(missing_ok=True)
    return report


def load_global_widths(iso: Path, index: list[int], catalog_path: Path,
                       manifest: dict) -> bytes:
    """Advance-width table of the (unchanged) global 1.72 atlas, read straight
    from the ORIGINAL ISO."""
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not row["version"].strip().endswith("1.72"):
                continue
            key = (int(row["archive_id"]), int(row["stream_id"]))
            info = manifest.get(key)
            if info is None or info["depth"] != 0:
                continue
            start = index[key[0]] * SECTOR + info["source_offset"]
            with iso.open("rb") as handle_iso:
                header = read_range(handle_iso, start, 16)
                if header[:3] != b"SLZ":
                    continue
                mode = header[3]
                comp, unpacked = u32(header, 4), u32(header, 8)
                payload = read_range(handle_iso, start + 16, comp)
            decoded = decompress_slz_payload(payload, mode, unpacked)
            # the 1.72 global container has table_start == 0 (no messages),
            # which Mclib.parse rejects; read the width table straight from
            # the header instead.
            if not decoded.startswith(b"so3mclib "):
                continue
            width_start = u32(decoded, 0x18)
            glyph_count = u32(decoded, 0x20)
            widths = decoded[width_start:width_start + glyph_count]
            if len(widths) >= 292:
                return bytes(widths)
    raise VerifyFatal("global 1.72 width table not found on the original ISO")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Independent full-ISO verifier for the SO3 DC Korean patch")
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--patched", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-from-plan", dest="expected_from_plan",
                        action="store_true", default=True,
                        help="derive expectations from the plan (default, always on)")
    parser.add_argument("--tr-out-crosscheck", action="store_true",
                        help="also load tr_out and report (as warnings) where it "
                             "diverges from the plan (advisory only)")
    parser.add_argument("--tr-out-dir", type=Path, default=DEFAULTS["tr_out_dir"])
    parser.add_argument("--batches-dir", type=Path, default=DEFAULTS["batches_dir"])
    parser.add_argument("--units", type=Path, default=DEFAULTS["units"])
    parser.add_argument("--inventory", type=Path, default=DEFAULTS["inventory"])
    parser.add_argument("--controls", type=Path, default=DEFAULTS["controls"])
    parser.add_argument("--catalog", type=Path, default=DEFAULTS["catalog"])
    parser.add_argument("--manifest", type=Path, default=DEFAULTS["manifest"])
    parser.add_argument("--font", type=Path, default=DEFAULTS["font"])
    parser.add_argument("--expected-original-sha256", default=ORIGINAL_ISO_SHA256,
                        help="SHA-256 pin of the ORIGINAL ISO (default: disc 1; "
                             "disc 2: 349FF944...70288)")
    parser.add_argument("--expected-original-size", type=int, default=ORIGINAL_ISO_SIZE,
                        help="byte size pin of the ORIGINAL/patched ISO "
                             "(default: disc 1 = 4,689,854,464; disc 2 = 4,685,955,072)")
    parser.add_argument("--expected-patched-sha256")
    parser.add_argument("--allow-untranslated", action="store_true",
                        help="deprecated no-op: the plan defines exactly what is "
                             "patched, so intentional Japanese messages are not errors")
    parser.add_argument("--name-patch", action="store_true",
                        help="the ISO also carries so3_name_patch (run AFTER "
                             "the message patch): whitelist its four extents, "
                             "delegate their content to verify_name_patch and "
                             "audit the global font's 21 documented slots")
    parser.add_argument("--xdelta", type=Path, default=DEFAULTS["xdelta"])
    parser.add_argument("--skip-xdelta", action="store_true")
    parser.add_argument("--xdelta-temp-dir", type=Path)
    args = parser.parse_args(argv)

    started = time.time()
    log = Log()
    report: dict = {
        "schema_version": 1,
        "tool": "verify_full_iso",
        "original_iso": str(args.original),
        "patched_iso": str(args.patched),
        "plan": str(args.plan),
        "checks": {},
    }

    try:
        # ---- check 1: sizes, hashes, font pin
        if args.original.resolve() == args.patched.resolve():
            raise VerifyFatal("original and patched ISO alias the same file")
        expected_iso_size = args.expected_original_size
        expected_original_sha256 = args.expected_original_sha256.upper()
        original_size = args.original.stat().st_size
        patched_size = args.patched.stat().st_size
        if original_size != expected_iso_size:
            log.err("sizes", "original", f"size {original_size} != {expected_iso_size}")
        if patched_size != expected_iso_size:
            log.err("sizes", "patched", f"size {patched_size} != {expected_iso_size}")
        if log.error_count:
            raise VerifyFatal("ISO size mismatch")
        original_hash = sha256_file(args.original)
        if original_hash != expected_original_sha256:
            log.err("sizes", "original", "original ISO SHA-256 mismatch",
                    got=original_hash)
            raise VerifyFatal("original ISO SHA-256 mismatch")
        patched_hash = sha256_file(args.patched)
        if args.expected_patched_sha256:
            if patched_hash != args.expected_patched_sha256.upper():
                log.err("sizes", "patched", "patched ISO SHA-256 mismatch",
                        got=patched_hash)
        font_hash = sha256_file(args.font)
        if font_hash != NANUM_FONT_SHA256:
            log.err("sizes", "font", "Nanum font SHA-256 mismatch", got=font_hash)
            raise VerifyFatal("font SHA-256 mismatch")
        report["checks"]["sizes_and_hashes"] = {
            "iso_size": expected_iso_size,
            "original_sha256": original_hash,
            "patched_sha256": patched_hash,
            "font_sha256": font_hash,
        }

        # ---- check 2: hidden index
        with args.original.open("rb") as orig, args.patched.open("rb") as patch:
            raw_equal = (read_range(orig, INDEX_OFFSET, INDEX_BYTES)
                         == read_range(patch, INDEX_OFFSET, INDEX_BYTES))
        if not raw_equal:
            log.err("hidden_index", "raw", "encoded hidden index block changed")
        original_index = read_index(args.original)
        patched_index = read_index(args.patched)
        decoded_equal = original_index == patched_index
        if not decoded_equal:
            log.err("hidden_index", "decoded", "decoded 6,144-entry index changed")
        report["checks"]["hidden_index"] = {
            "raw_identical": raw_equal,
            "decoded_entries": INDEX_ENTRIES * 3,
            "decoded_identical": decoded_equal,
        }
        if not (raw_equal and decoded_equal):
            raise VerifyFatal("hidden index changed")

        # ---- inputs (the PLAN is authoritative; tr_out is advisory only)
        ctl = Controls(args.controls)
        renderer = Renderer(args.font)
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        budgets = load_budgets(args.batches_dir, log)
        catalog = load_catalog_occurrences(args.catalog)
        manifest = load_manifest(args.manifest)
        global_widths = load_global_widths(args.original, original_index,
                                           args.catalog, manifest)

        plan = load_plan(args.plan, catalog, log)
        if not plan:
            raise VerifyFatal(f"plan {args.plan} resolved to no streams")
        expected, coverage_stats = build_expectations_from_plan(
            plan, inventory, budgets, log)

        if args.tr_out_crosscheck:
            translations = load_translations(args.tr_out_dir, log)
            report["checks"]["tr_out_advisory"] = crosscheck_tr_out(
                translations, expected, log)

        # nested/manifest resolution: every planned occurrence must be a
        # patchable (depth 0) stream present in the manifest
        by_archive: dict[int, dict[int, tuple[int, dict, dict | None]]] = defaultdict(dict)
        for (archive_id, stream_id), expects in sorted(expected.items()):
            info = manifest.get((archive_id, stream_id))
            if info is None:
                log.err("plan", f"{archive_id}:{stream_id}",
                        "planned stream absent from stream manifest")
                continue
            if info["depth"] != 0:
                log.err("plan", f"{archive_id}:{stream_id}",
                        "planned stream is nested (depth>0): unpatchable")
                continue
            by_archive[archive_id][info["source_offset"]] = (
                stream_id, expects, plan.get((archive_id, stream_id)))

        # ---- check 3: diff scope
        extents = []
        archive_extents = []
        for archive_id in sorted(by_archive):
            start = original_index[archive_id] * SECTOR
            size = original_index[INDEX_ENTRIES + archive_id] * SECTOR
            if start <= 0 or size <= 0:
                log.err("archive", f"archive{archive_id}", "absent from hidden index")
                continue
            extents.append((start, size, f"archive{archive_id}"))
            archive_extents.append((start, size, archive_id))
        np_module = None
        if args.name_patch:
            import so3_name_patch as np_module  # delegation target (not under test here)
            for start, size, label in name_patch_extents(np_module):
                for a_start, a_size, archive_id in archive_extents:
                    if start < a_start + a_size and a_start < start + size:
                        raise VerifyFatal(
                            f"name-patch extent {label} overlaps planned "
                            f"archive {archive_id}: unsupported")
                extents.append((start, size, label))
        extents.sort()
        report["checks"]["diff_scope"] = {
            "planned_archives": len(archive_extents),
            "name_patch_extents": len(extents) - len(archive_extents),
        }
        report["checks"]["diff_scope"].update(
            verify_diff_scope(args.original, args.patched, extents, log,
                              iso_size=expected_iso_size))

        # ---- checks 4-7 per archive
        memo: dict = {}
        container_totals = Counter({
            "unique_verified": 0, "memo_hits": 0, "verified_containers": 0,
            "verified_messages": 0, "characters_rendered": 0,
            "g_bitmaps_verified": 0, "width_lines_checked": 0,
            "width_lines_unbudgeted": 0,
        })
        verified_occurrences: set[tuple[int, int, int]] = set()
        archive_reports = []
        with args.original.open("rb") as orig, args.patched.open("rb") as patch:
            for start, size, archive_id in archive_extents:
                original_bytes = read_range(orig, start, size)
                patched_bytes = read_range(patch, start, size)
                archive_reports.append(verify_archive(
                    archive_id, original_bytes, patched_bytes,
                    by_archive[archive_id], ctl, renderer, global_widths,
                    log, memo, container_totals, verified_occurrences))
        report["checks"]["archives"] = archive_reports
        report["checks"]["containers"] = dict(container_totals)

        # ---- name patch (delegated extents + independent font audit)
        if args.name_patch and np_module is not None:
            report["checks"]["name_patch"] = verify_name_patch_checks(
                args.original, args.patched, np_module, log)

        # ---- check 8: coverage (plan semantics)
        # The plan is the spec: every planned message must have been verified
        # as changed + decoded to the plan text.  Messages NOT in the plan
        # (fit_repair drops, non-JP) are proven byte-identical to the original
        # inside verify_container ("non-target message" checks) and by the
        # diff-scope check for whole non-planned archives, so they need no
        # per-message reconciliation here.
        missing_occurrences = 0
        for (archive_id, stream_id), expects in sorted(expected.items()):
            for mid in sorted(expects):
                if (archive_id, stream_id, mid) not in verified_occurrences:
                    missing_occurrences += 1
                    log.err("coverage", f"{archive_id}:{stream_id}:{mid}",
                            "planned message was not verified as applied")
        planned_occurrences = sum(len(v) for v in expected.values())
        coverage_stats["planned_occurrences"] = planned_occurrences
        coverage_stats["verified_occurrences"] = len(verified_occurrences)
        coverage_stats["missing_occurrences"] = missing_occurrences
        report["checks"]["coverage"] = coverage_stats

        # ---- check 9: xdelta
        if args.skip_xdelta:
            report["checks"]["xdelta"] = {"skipped": True}
        else:
            temp_dir = args.xdelta_temp_dir or args.patched.parent
            report["checks"]["xdelta"] = run_xdelta(
                args.xdelta, args.original, args.patched, temp_dir, log)

    except VerifyFatal as exc:
        log.err("fatal", "-", str(exc))

    ok = log.error_count == 0
    report["ok"] = ok
    report["status"] = ("static_verification_complete_runtime_unverified"
                        if ok else "failed")
    report["errors_total"] = log.error_count
    report["warnings_total"] = log.warning_count
    report["counts_by_check"] = dict(sorted(log.by_check.items()))
    report["errors"] = log.errors
    report["warnings"] = log.warnings
    report["elapsed_seconds"] = round(time.time() - started, 1)

    rendered = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(f"verify_full_iso: {'OK' if ok else 'FAILED'} "
          f"(errors={log.error_count}, warnings={log.warning_count}, "
          f"{report['elapsed_seconds']}s)")
    for entry in log.errors[:25]:
        print(f"  ERROR [{entry['check']}] {entry['where']}: {entry['message']}")
    if log.error_count > 25:
        print(f"  ... {log.error_count - 25} more errors (see report)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

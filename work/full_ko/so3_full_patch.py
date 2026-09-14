#!/usr/bin/env python3
"""Generic full-disc mclib Korean patcher core for Star Ocean 3 DC (PS2).

Data-driven, fail-closed.  Reuses the proven primitives from
publish/so3dc-korean-tools (so3_repack, patch_hyda_dialogue) and the optimal
SLZ mode-2 compressor (slz_optimal).  See PATCHER_SPEC.md / MASTER_PLAN.md.

Layers
------
1. segment codec  : tokenize -> analyze -> (decode to marked-up text | encode
                    translated text back to bytecode).  Structure (8080 lines,
                    8180 pages), positional controls (markers), invisible
                    controls (proportional re-anchoring), speaker construct
                    ([8980?] 8780 8080), drops (ruby 9080/9180, name 9380).
2. container      : rebuild_container -- full mclib re-layout with local atlas
                    rebuild (protect/reuse/append; glyph_count==0 supported).
3. archive        : parse_archive_layout (PK1 / PACK / top-level SLZ chain)
                    and rebuild_archive (strategy A in-place, B joint reflow).
4. orchestration  : patch_archives / simulate over a plan built from the
                    stream manifest; ISO write with re-read verification and
                    atomic promote.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
PUBLISH = WS / "publish" / "so3dc-korean-tools"
if not (PUBLISH / "so3_repack.py").exists():
    PUBLISH = WS  # repo checkout: the clone root IS the workspace
FULL_KO = Path(__file__).resolve().parent
for _p in (str(PUBLISH), str(PUBLISH / "tools"), str(FULL_KO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Proven primitives (provenance: publish/so3dc-korean-tools).
from so3_repack import (  # noqa: E402
    Mclib,
    align,
    compress_slz_mode2,
    decompress_slz_payload,
    encode_glyph_code,
    read_index,
)
from patch_hyda_dialogue import (  # noqa: E402
    GLOBAL_CODE_MAP,
    conservative_local_codes,
    logical_segment,
    optimize_glyph_order,
    render_glyphs_at_size,
)
from slz_optimal import compress_slz_mode2_optimal  # noqa: E402
from decode_mclib_text import G as GLOBAL_TRANSCRIPTION  # noqa: E402

SECTOR = 0x800
CONTROL_TABLE_PATH = FULL_KO / "control_sizes_full.json"
STREAM_MANIFEST_PATH = WS / "work" / "full_unpack" / "disc1" / "manifests" / "stream_manifest.csv"
CONTAINER_CATALOG_PATH = WS / "work" / "mclib_all_decode" / "container_catalog.csv"
BITMAP_MAP_24_PATH = WS / "work" / "font_ocr" / "glyph_mapping_ordered_24.json"
DEFAULT_FONT_PATH = Path(os.environ.get("SO3_FONT", r"D:\ps2\NanumSquareNeo-cBd.ttf"))
FONT_GRAY_LEVELS = 2
FONT_PIXEL_SIZE = 22

FAM_NEWLINE = 0x80
FAM_PAGE = 0x81
FAM_SPEAKER = 0x87
FAM_COLOR_RESET = 0x89
# Task-mandated classification (control_sizes_full.json families, byte b0).
DEFAULT_POSITIONAL = frozenset({0x92, 0xA1, 0xA2, 0xA3, 0x9C, 0x88, 0x89})
DEFAULT_DROP = frozenset({0x90, 0x91, 0x93})

NEWLINE_RAW = b"\x80\x80"
PAGE_RAW = b"\x81\x80"
# ⟦n⟧ positional / ⟦P⟧ page break / ⟦G:sha8⟧ preserve-bitmap glyph
MARKER_RE = re.compile(r"⟦(P|[0-9]+|G:[0-9a-fA-F]{8})⟧")
G_TOKEN_RE = re.compile(r"⟦G:([0-9a-fA-F]{8})⟧")
PAGE_MARK = "⟦P⟧"

GLOBAL_CODE_TO_CHAR = {i + 1: GLOBAL_TRANSCRIPTION[i] for i in range(len(GLOBAL_TRANSCRIPTION))}
_JP_RE = re.compile(r"[ぁ-ゟァ-ヿ一-鿿々〇]")


class PatchError(ValueError):
    """Base class: all fail-closed rejections carry a structured message."""

    def __init__(self, message: str, **info: object) -> None:
        super().__init__(message if not info else f"{message} | {json.dumps(info, ensure_ascii=False, default=str)}")
        self.info = info


class SegmentError(PatchError):
    pass


class ContainerError(PatchError):
    pass


class LayoutError(PatchError):
    pass


class FitError(PatchError):
    pass


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(bytes(data)).hexdigest().lower()


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def align4(value: int) -> int:
    return (value + 3) & ~3


def has_nonzero(buffer: bytes | bytearray | memoryview, start: int = 0, end: int | None = None) -> bool:
    view = memoryview(buffer)[start:end]
    return view.nbytes != bytes(view).count(0)


def first_nonzero(data: bytes, start: int, chunk: int = 1 << 20) -> int:
    """Index of the first non-zero byte at/after start, or -1."""
    pos = start
    n = len(data)
    while pos < n:
        block = data[pos:pos + chunk]
        if block.count(0) != len(block):
            for i, value in enumerate(block):
                if value:
                    return pos + i
        pos += len(block)
    return -1


# ---------------------------------------------------------------------------
# control table
# ---------------------------------------------------------------------------

class ControlTable:
    """Authoritative opcode table (ELF triple-cross-verified)."""

    def __init__(self, spec: dict[str, object]) -> None:
        self.fixed: dict[int, int] = {}
        self.zero_terminated: set[int] = set()
        for key, info in spec.items():
            if key.startswith("_"):
                continue
            fam = int(key[:2], 16)
            if not isinstance(info, dict):
                raise PatchError(f"bad control spec entry {key}")
            if info.get("kind") == "zero_terminated":
                self.zero_terminated.add(fam)
            elif info.get("kind") == "fixed":
                total = info.get("total_size")
                if not isinstance(total, int) or total < 2:
                    raise PatchError(f"bad fixed control size for {key}")
                self.fixed[fam] = total
            else:
                raise PatchError(f"unknown control kind for {key}")

    @classmethod
    def load(cls, path: Path = CONTROL_TABLE_PATH) -> "ControlTable":
        return cls(json.loads(path.read_text(encoding="utf-8")))


_DEFAULT_TABLE: ControlTable | None = None


def default_control_table() -> ControlTable:
    global _DEFAULT_TABLE
    if _DEFAULT_TABLE is None:
        _DEFAULT_TABLE = ControlTable.load()
    return _DEFAULT_TABLE


# ---------------------------------------------------------------------------
# tokenizer / segment analysis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Token:
    kind: str            # 'glyph' | 'control'
    raw: bytes
    code: int | None = None
    fam: int | None = None


def tokenize_segment(seg: bytes, table: ControlTable) -> tuple[list[Token], int]:
    """Tokenize from offset 0 to the message terminator (returned index)."""
    tokens: list[Token] = []
    off = 0
    n = len(seg)
    while off < n:
        b0 = seg[off]
        if b0 == 0:
            return tokens, off
        if b0 < 0x80:
            tokens.append(Token("glyph", seg[off:off + 1], code=b0))
            off += 1
            continue
        if off + 1 >= n:
            raise SegmentError(f"truncated glyph/control at {off}")
        b1 = seg[off + 1]
        if b1 < 0x80:
            code = (b0 & 0x7F) | (b1 << 7)
            tokens.append(Token("glyph", seg[off:off + 2], code=code))
            off += 2
            continue
        if b1 != 0x80:
            raise SegmentError(f"non-canonical control {b0:02x}{b1:02x} at {off}")
        fam = b0
        if fam in table.zero_terminated:
            end = seg.find(b"\0", off + 2)
            if end < 0:
                raise SegmentError(f"unterminated zero-terminated control {fam:02x}80 at {off}")
            raw = seg[off:end + 1]
            off = end + 1
        else:
            size = table.fixed.get(fam)
            if size is None:
                raise SegmentError(f"unknown control {fam:02x}80 at {off}")
            if off + size > n:
                raise SegmentError(f"truncated control {fam:02x}80 at {off}")
            raw = seg[off:off + size]
            off += size
        tokens.append(Token("control", raw, fam=fam))
    raise SegmentError("segment lacks a NUL terminator")


def _is_ctrl(token: Token, fam: int) -> bool:
    return token.kind == "control" and token.fam == fam


@dataclass
class LinePlan:
    tokens: list[Token]                 # original line tokens (drops included)
    units: list[Token]                  # glyphs + positional controls
    invisible: list[tuple[int, Token]]  # (visible-unit position, token)


@dataclass
class SegmentPlan:
    original: bytes
    prefix_raw: bytes
    speaker_field_tokens: list[Token] | None   # None = no speaker construct
    speaker_field_raw: bytes
    delim_raw: bytes
    pages: list[list[LinePlan]]
    positional_tokens: list[Token]             # reading order over the body
    dropped_tokens: int


def analyze_segment(
    seg: bytes,
    table: ControlTable | None = None,
    positional: frozenset[int] = DEFAULT_POSITIONAL,
    drop: frozenset[int] = DEFAULT_DROP,
    structural_page: bool = True,
) -> SegmentPlan:
    """structural_page=False treats 8180 as a plain invisible control
    (legacy Hyda semantics for marker-less translations)."""
    table = table or default_control_table()
    tokens, term = tokenize_segment(seg, table)
    if any(seg[term + 1:]):
        raise SegmentError("non-zero bytes after message terminator")

    # Speaker construct: first (8780, 8080) control pair; optional 8980 before;
    # the speaker field is the maximal run of glyph/9380 tokens right before.
    delim_index = None
    for i in range(len(tokens) - 1):
        if _is_ctrl(tokens[i], FAM_SPEAKER) and _is_ctrl(tokens[i + 1], FAM_NEWLINE):
            delim_index = i
            break
    if delim_index is not None:
        delim_start = delim_index
        if delim_start > 0 and _is_ctrl(tokens[delim_start - 1], FAM_COLOR_RESET):
            delim_start -= 1
        j = delim_start
        while j > 0 and (tokens[j - 1].kind == "glyph" or _is_ctrl(tokens[j - 1], 0x93)):
            j -= 1
        prefix = tokens[:j]
        speaker_field: list[Token] | None = tokens[j:delim_start]
        delim = tokens[delim_start:delim_index + 2]
        body = tokens[delim_index + 2:]
    else:
        prefix, speaker_field, delim, body = [], None, [], tokens

    pages: list[list[LinePlan]] = []
    positional_tokens: list[Token] = []
    dropped = 0
    line_tokens: list[Token] = []
    lines: list[list[Token]] = [line_tokens]
    page_lines: list[list[list[Token]]] = [lines]
    for token in body:
        if structural_page and _is_ctrl(token, FAM_PAGE):
            lines = [[]]
            page_lines.append(lines)
        elif _is_ctrl(token, FAM_NEWLINE):
            lines.append([])
        else:
            lines[-1].append(token)
    for raw_lines in page_lines:
        planned: list[LinePlan] = []
        for raw_line in raw_lines:
            units: list[Token] = []
            invisible: list[tuple[int, Token]] = []
            for token in raw_line:
                if token.kind == "glyph":
                    units.append(token)
                elif token.fam in drop:
                    dropped += 1
                elif token.fam in positional:
                    units.append(token)
                    positional_tokens.append(token)
                else:
                    invisible.append((len(units), token))
            planned.append(LinePlan(raw_line, units, invisible))
        pages.append(planned)

    return SegmentPlan(
        original=seg,
        prefix_raw=b"".join(t.raw for t in prefix),
        speaker_field_tokens=speaker_field,
        speaker_field_raw=b"".join(t.raw for t in speaker_field) if speaker_field is not None else b"",
        delim_raw=b"".join(t.raw for t in delim),
        pages=pages,
        positional_tokens=positional_tokens,
        dropped_tokens=dropped,
    )


def preserved_byte_chunks(plan: SegmentPlan, keep_speaker: bool) -> list[bytes]:
    """Byte regions of a translated message that survive verbatim.

    They can carry glyph-code look-alikes (e.g. 9C80 payload glyphs, kept
    speaker glyphs), so the atlas protection scan must cover them.
    """
    chunks = [plan.prefix_raw, plan.delim_raw]
    if keep_speaker and plan.speaker_field_tokens is not None:
        chunks.append(plan.speaker_field_raw)
    for lines in plan.pages:
        for line in lines:
            for token in line.tokens:
                if token.kind == "control" and token.fam not in DEFAULT_DROP:
                    chunks.append(token.raw)
    return [chunk for chunk in chunks if chunk]


# ---------------------------------------------------------------------------
# text codec
# ---------------------------------------------------------------------------

def struct_to_text(
    plan: SegmentPlan,
    code_to_char: dict[int, str],
) -> tuple[str, str | None, int]:
    """Decode a SegmentPlan to marked-up text.

    Returns (body_text, speaker_text | None, unknown_glyph_count).  Body pages
    are joined with ⟦P⟧, lines with \\n; the i-th positional control becomes
    ⟦i⟧ (1-based, reading order).  Dropped controls are not represented.
    """
    unknown = 0
    counter = 0
    page_texts: list[str] = []
    for lines in plan.pages:
        line_texts: list[str] = []
        for line in lines:
            parts: list[str] = []
            for unit in line.units:
                if unit.kind == "glyph":
                    ch = code_to_char.get(unit.code)
                    if ch is None:
                        unknown += 1
                        ch = "〓"  # 〓
                    parts.append(ch)
                else:
                    counter += 1
                    parts.append(f"⟦{counter}⟧")
            line_texts.append("".join(parts))
        page_texts.append("\n".join(line_texts))
    body = PAGE_MARK.join(page_texts)
    speaker = None
    if plan.speaker_field_tokens is not None:
        parts = []
        for token in plan.speaker_field_tokens:
            if token.kind == "glyph":
                ch = code_to_char.get(token.code)
                if ch is None:
                    unknown += 1
                    ch = "〓"
                parts.append(ch)
            else:
                unknown += 1
                parts.append("〓")
        speaker = "".join(parts)
    return body, speaker, unknown


TextUnit = tuple[str, object]  # ('char', ch) | ('pos', int) | ('bitmap', sha8)


def parse_translated_text(text: str) -> list[list[list[TextUnit]]]:
    """Parse translated text into pages -> lines -> units."""
    if "\0" in text:
        raise SegmentError("translated text contains NUL")
    pages: list[list[list[TextUnit]]] = [[[]]]

    def push(ch: str) -> None:
        if ch == "\n":
            pages[-1].append([])
        else:
            pages[-1][-1].append(("char", ch))

    idx = 0
    for match in MARKER_RE.finditer(text):
        for ch in text[idx:match.start()]:
            push(ch)
        idx = match.end()
        value = match.group(1)
        if value == "P":
            # The inventory renders a page break as a line-initial "\n⟦P⟧", so the
            # newline just before ⟦P⟧ leaves a spurious empty trailing line on the
            # current page. The original bytes put the 8180 page break directly
            # after the line's content (no 8080 newline), so drop that empty line
            # to keep the page's line count equal to the original's.
            if len(pages[-1]) > 1 and not pages[-1][-1]:
                pages[-1].pop()
            pages.append([[]])
        elif value.startswith("G:"):
            pages[-1][-1].append(("bitmap", value[2:].lower()))
        else:
            pages[-1][-1].append(("pos", int(value)))
    tail = text[idx:]
    if "⟦" in tail or "⟧" in tail:
        raise SegmentError(f"unbalanced marker brackets in translated text: {text!r}")
    for ch in tail:
        push(ch)
    for page in pages:
        for line in page:
            for kind, val in line:
                if kind == "char" and ("⟦" in str(val) or "⟧" in str(val)):
                    raise SegmentError("stray marker bracket")
    return pages


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text).replace("\n", "")


def parse_inline_units(text: str) -> list[TextUnit]:
    """Single-line unit list (chars + ⟦G:⟧ bitmap tokens only) for speakers."""
    pages = parse_translated_text(text)
    if len(pages) != 1 or len(pages[0]) != 1:
        raise SegmentError(f"inline text must be a single line: {text!r}")
    for kind, _ in pages[0][0]:
        if kind == "pos":
            raise SegmentError(f"positional marker not allowed here: {text!r}")
    return pages[0][0]


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------

def _encode_unit(
    kind: str,
    value: object,
    char_to_code: dict[str, int],
    bitmap_code_map: dict[str, int] | None,
) -> bytes:
    if kind == "char":
        code = char_to_code.get(value)
        if code is None:
            raise SegmentError(f"no glyph code for character {value!r}")
        return encode_glyph_code(code)
    if kind == "bitmap":
        code = (bitmap_code_map or {}).get(value)
        if code is None:
            raise SegmentError(f"unresolved preserve-bitmap token ⟦G:{value}⟧")
        return encode_glyph_code(code)
    raise AssertionError(f"unexpected unit kind {kind}")


def _emit_line(
    line: LinePlan,
    new_units: Sequence[TextUnit],
    char_to_code: dict[str, int],
    positional_tokens: Sequence[Token],
    bitmap_code_map: dict[str, int] | None = None,
) -> bytes:
    orig_n = len(line.units)
    new_n = len(new_units)
    anchored: dict[int, list[bytes]] = defaultdict(list)
    for pos, token in line.invisible:
        anchor = round(pos * new_n / orig_n) if orig_n else 0
        anchor = max(0, min(new_n, anchor))
        anchored[anchor].append(token.raw)
    out = bytearray()
    for boundary in range(new_n + 1):
        for raw in anchored.get(boundary, ()):
            out += raw
        if boundary < new_n:
            kind, value = new_units[boundary]
            if kind == "pos":
                out += positional_tokens[value - 1].raw
            else:
                out += _encode_unit(kind, value, char_to_code, bitmap_code_map)
    return bytes(out)


def encode_translated_segment(
    original_seg: bytes,
    korean: str,
    char_to_code: dict[str, int],
    control_table: ControlTable | None = None,
    *,
    speaker_korean: str | None = None,
    keep_speaker: bool = False,
    positional: frozenset[int] = DEFAULT_POSITIONAL,
    drop: frozenset[int] = DEFAULT_DROP,
    structural_page: bool = True,
    bitmap_code_map: dict[str, int] | None = None,
    plan: SegmentPlan | None = None,
) -> bytes:
    """Re-encode one message with the translated text.

    Structure rules (hard errors on violation):
    * translated pages/lines must not exceed the original counts; original
      trailing pages/lines beyond the translation must carry no visible units
      (they are preserved verbatim, Hyda's structural-tail rule generalized);
    * ⟦i⟧ markers must be exactly 1..N in reading order, N = number of
      positional control instances in the original body; each original
      instance is emitted verbatim (operands intact) at its marker position;
    * ⟦G:sha8⟧ preserve-bitmap tokens are in-line visible units resolved via
      bitmap_code_map (built by rebuild_container from the original atlas);
    * invisible controls are re-anchored proportionally within their line;
    * ruby 9080/9180 and 9380 are dropped (names arrive literalized);
    * the speaker construct ([8980?] 8780 8080) keeps prefix/delimiter bytes
      and replaces field glyphs with the literal Korean speaker.
    """
    if plan is None:
        plan = analyze_segment(original_seg, control_table, positional, drop, structural_page)

    korean_pages = parse_translated_text(korean)
    if len(korean_pages) > len(plan.pages):
        raise SegmentError(
            f"translation has {len(korean_pages)} pages, original has {len(plan.pages)}")
    markers = [value for page in korean_pages for line in page for kind, value in line if kind == "pos"]
    if markers != list(range(1, len(plan.positional_tokens) + 1)):
        raise SegmentError(
            "positional marker mismatch",
            expected=len(plan.positional_tokens), got=markers)

    for p, lines in enumerate(plan.pages):
        new_lines = korean_pages[p] if p < len(korean_pages) else []
        if len(new_lines) > len(lines):
            raise SegmentError(
                f"translation page {p} has {len(new_lines)} lines, original has {len(lines)}")
        for l, line in enumerate(lines):
            if l >= len(new_lines) and line.units:
                raise SegmentError(
                    f"original page {p} line {l} has visible units but no translated line")

    # speaker
    if plan.speaker_field_tokens is None:
        if speaker_korean is not None:
            raise SegmentError("speaker_korean given but the segment has no speaker construct")
        head = plan.prefix_raw  # always b"" (no construct -> no prefix split)
    else:
        if keep_speaker:
            if speaker_korean is not None:
                raise SegmentError("keep_speaker with speaker_korean")
            field = plan.speaker_field_raw
        else:
            if speaker_korean is None:
                raise SegmentError("segment has a speaker construct; speaker_korean required (or keep_speaker)")
            field = b"".join(
                _encode_unit(kind, value, char_to_code, bitmap_code_map)
                for kind, value in parse_inline_units(speaker_korean))
        head = plan.prefix_raw + field + plan.delim_raw

    page_bufs: list[bytes] = []
    for p, lines in enumerate(plan.pages):
        new_lines = korean_pages[p] if p < len(korean_pages) else []
        line_bufs: list[bytes] = []
        for l, line in enumerate(lines):
            units = new_lines[l] if l < len(new_lines) else []
            line_bufs.append(_emit_line(
                line, units, char_to_code, plan.positional_tokens, bitmap_code_map))
        page_bufs.append(NEWLINE_RAW.join(line_bufs))
    body = PAGE_RAW.join(page_bufs)
    return head + body + b"\0"


# ---------------------------------------------------------------------------
# glyph helpers
# ---------------------------------------------------------------------------

def load_bitmap_unicode_map(path: Path = BITMAP_MAP_24_PATH) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for glyph in data["glyphs"]:
        if glyph.get("unicode"):
            result[glyph["bitmap_sha256"]] = glyph["unicode"]
    return result


def container_code_to_char(
    parsed: Mclib,
    bitmap_map: dict[str, str],
    *,
    unlabeled_g_tokens: bool = False,
) -> dict[int, str]:
    """code -> unicode for one container (global transcription + OCR bitmaps).

    unlabeled_g_tokens=True maps unlabeled local glyphs to the inventory's
    preserve-bitmap token ⟦G:sha8⟧ instead of leaving them undecodable."""
    result: dict[int, str] = {}
    if parsed.local_base != 1:
        result.update(GLOBAL_CODE_TO_CHAR)
    glyph_bytes = parsed.glyph_stride * parsed.glyph_height // 2
    for index in range(parsed.glyph_count):
        bitmap = parsed.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]
        digest = hashlib.sha256(bitmap).hexdigest()
        ch = bitmap_map.get(digest)
        if ch is not None:
            result[parsed.local_base + index] = ch
        elif unlabeled_g_tokens:
            result[parsed.local_base + index] = f"⟦G:{digest[:8]}⟧"
    return result


def identity_code_maps(parsed: Mclib) -> tuple[dict[int, str], dict[str, int]]:
    """Bijective PUA (plane 16) code<->char maps covering every possible code."""
    upper = max(parsed.local_base + parsed.glyph_count, 0x125)
    code_to_char = {code: chr(0x100000 + code) for code in range(1, upper + 0x40)}
    return code_to_char, {ch: code for code, ch in code_to_char.items()}


def render_korean_glyphs(text: str, font_path: Path) -> list[tuple[int, bytes]]:
    """Nanum raster identical to the shipped Hyda pipeline (verifier-compatible)."""
    return render_glyphs_at_size(text, font_path, FONT_PIXEL_SIZE, FONT_GRAY_LEVELS)


def neighbor_order(characters: list[str], rendered: list[tuple[int, bytes]]) -> tuple[list[str], list[tuple[int, bytes]]]:
    """Bitmap nearest-neighbour ordering (SLZ locality lever)."""
    n = len(characters)
    if n < 3:
        return characters, rendered
    if n <= 192:
        return optimize_glyph_order(characters, rendered)
    import numpy as np
    bits = np.unpackbits(
        np.frombuffer(b"".join(bm for _, bm in rendered), dtype=np.uint8).reshape(n, -1), axis=1)
    order = [0]
    remaining = np.ones(n, dtype=bool)
    remaining[0] = False
    current = bits[0]
    for _ in range(n - 1):
        idx = np.flatnonzero(remaining)
        dists = np.count_nonzero(bits[idx] != current, axis=1)
        pick = idx[int(np.argmin(dists))]
        order.append(int(pick))
        remaining[pick] = False
        current = bits[pick]
    return [characters[i] for i in order], [rendered[i] for i in order]


# ---------------------------------------------------------------------------
# container rebuild
# ---------------------------------------------------------------------------

def conservative_codes(data: bytes, local_base: int, glyph_count: int) -> set[int]:
    """Conservative local-slot scan; extends the proven Hyda scan to bases
    below 0x80 whose local codes can appear as single bytes."""
    result = conservative_local_codes(data, local_base, glyph_count)
    if local_base < 0x80:
        upper = min(local_base + glyph_count, 0x80)
        for value in set(data):
            if local_base <= value < upper:
                result.add(value)
    return result


@dataclass
class TranslationEntry:
    korean: str
    speaker_korean: str | None = None
    keep_speaker: bool = False


def _as_entry(value: object) -> TranslationEntry:
    if isinstance(value, TranslationEntry):
        return value
    if isinstance(value, dict):
        return TranslationEntry(
            korean=value["korean"],
            speaker_korean=value.get("speaker_korean"),
            keep_speaker=bool(value.get("keep_speaker", False)),
        )
    raise PatchError(f"bad translation entry: {value!r}")


def rebuild_container(
    decoded: bytes,
    translations: dict[int, object],
    *,
    table: ControlTable | None = None,
    font_path: Path | None = None,
    positional: frozenset[int] = DEFAULT_POSITIONAL,
    drop: frozenset[int] = DEFAULT_DROP,
    identity_code_map: dict[str, int] | None = None,
    optimize_bitmap_layout: bool = False,
    zero_unused_glyphs: bool = True,
    structural_page: bool = True,
) -> tuple[bytes, dict[str, object]]:
    """Rebuild one mclib with translated messages and a rebuilt local atlas.

    zero_unused_glyphs (capacity lever 1, spec default): local slots that are
    neither protected nor newly assigned are provably unreferenced after the
    rebuild; their bitmaps are zero-filled so they compress away.  Identity
    mode never touches the atlas.

    Post-conditions (verified here, AssertionError on violation):
    * every translated message decodes back to its Korean text/speaker;
    * every non-translated message stays logically identical;
    * every protected glyph keeps its width and bitmap;
    * geometry and local_base are unchanged.
    """
    table = table or default_control_table()
    parsed = Mclib.parse(decoded)
    if (parsed.glyph_width, parsed.glyph_height, parsed.glyph_stride) != (24, 24, 24):
        raise ContainerError(
            f"unsupported glyph geometry {parsed.glyph_width}x{parsed.glyph_height}x{parsed.glyph_stride}")
    if parsed.table_start != 0x80:
        raise ContainerError(f"unexpected mapping table offset 0x{parsed.table_start:X}")
    glyph_bytes = parsed.glyph_stride * parsed.glyph_height // 2

    entries = {int(mid): _as_entry(v) for mid, v in translations.items()}
    offsets_of: dict[int, list[int]] = defaultdict(list)
    for mid, off in parsed.rows:
        offsets_of[mid].append(off)
    missing = sorted(mid for mid in entries if mid not in offsets_of)
    if missing:
        raise ContainerError(f"translated message ids absent from mclib: {missing[:10]}")
    ambiguous = sorted(mid for mid in entries if len(offsets_of[mid]) != 1)
    if ambiguous:
        raise ContainerError(f"translated message ids ambiguous in mclib: {ambiguous[:10]}")
    target_offsets = {offsets_of[mid][0]: mid for mid in entries}

    # analyze targets, collect protection
    plans: dict[int, SegmentPlan] = {}
    for offset, mid in target_offsets.items():
        try:
            plans[mid] = analyze_segment(parsed.segments[offset], table, positional, drop, structural_page)
        except SegmentError as exc:
            raise ContainerError(f"message {mid}: {exc}") from exc
    protected: set[int] = set()
    for offset, segment in parsed.segments.items():
        if offset not in target_offsets:
            protected.update(conservative_codes(segment, parsed.local_base, parsed.glyph_count))
    for mid, entry in entries.items():
        for chunk in preserved_byte_chunks(plans[mid], entry.keep_speaker):
            protected.update(conservative_codes(chunk, parsed.local_base, parsed.glyph_count))

    # ⟦G:sha8⟧ preserve-bitmap tokens: resolve against the ORIGINAL atlas.
    # The source slot is protected in place (bitmap + advance width survive at
    # their original code), which subsumes copying and dedupes for free.
    g_needed: set[str] = set()
    for entry in entries.values():
        g_needed.update(m.lower() for m in G_TOKEN_RE.findall(entry.korean))
        if entry.speaker_korean is not None:
            g_needed.update(m.lower() for m in G_TOKEN_RE.findall(entry.speaker_korean))
    bitmap_code_map: dict[str, int] = {}
    if g_needed:
        by_prefix: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for index in range(parsed.glyph_count):
            digest = hashlib.sha256(
                parsed.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]).hexdigest()
            by_prefix[digest[:8]][digest].append(index)
        for sha8 in sorted(g_needed):
            groups = by_prefix.get(sha8)
            if not groups:
                raise ContainerError(
                    f"⟦G:{sha8}⟧ matches no local glyph bitmap in this container")
            if len(groups) > 1:
                raise ContainerError(
                    f"⟦G:{sha8}⟧ is ambiguous: {len(groups)} distinct bitmaps share the prefix")
            source_index = min(next(iter(groups.values())))
            code = parsed.local_base + source_index
            protected.add(code)
            bitmap_code_map[sha8] = code

    # character inventory
    use_global = parsed.local_base > max(GLOBAL_CODE_MAP.values())
    needed: set[str] = set()
    for entry in entries.values():
        needed.update(strip_markers(entry.korean))
        if entry.speaker_korean is not None:
            needed.update(strip_markers(entry.speaker_korean))
    if use_global:
        needed -= set(GLOBAL_CODE_MAP)

    appended = 0
    reused = 0
    rendered: list[tuple[int, bytes]] = []
    assigned_indices: list[int] = []
    if identity_code_map is not None:
        char_to_code = dict(identity_code_map)
        missing_chars = sorted(ch for ch in needed if ch not in char_to_code)
        if missing_chars:
            raise ContainerError(f"identity map lacks characters: {missing_chars[:10]}")
        characters: list[str] = []
    else:
        if font_path is None:
            raise ContainerError("font_path required unless identity_code_map is given")
        characters = sorted(needed)  # Unicode order (compression locality)
        rendered = render_korean_glyphs("".join(characters), font_path) if characters else []
        if len(rendered) != len(characters):
            raise AssertionError("glyph renderer count mismatch")
        if optimize_bitmap_layout and characters:
            characters, rendered = neighbor_order(characters, rendered)
        reusable = [i for i in range(parsed.glyph_count) if parsed.local_base + i not in protected]
        reused = min(len(reusable), len(characters))
        assigned_indices = reusable[:reused]
        appended = len(characters) - reused
        assigned_indices.extend(range(parsed.glyph_count, parsed.glyph_count + appended))
        codes = [parsed.local_base + i for i in assigned_indices]
        if codes and max(codes) >= 0x4000:
            raise ContainerError("local glyph codes exceed the tested two-byte range")
        char_to_code = dict(zip(characters, codes))
        if use_global:
            char_to_code.update({ch: code for ch, code in GLOBAL_CODE_MAP.items()})

    # encode targets
    replacements: dict[int, bytes] = {}
    per_message: list[dict[str, object]] = []
    for offset, mid in sorted(target_offsets.items()):
        entry = entries[mid]
        original = parsed.segments[offset]
        try:
            new_segment = encode_translated_segment(
                original, entry.korean, char_to_code, table,
                speaker_korean=entry.speaker_korean, keep_speaker=entry.keep_speaker,
                positional=positional, drop=drop, structural_page=structural_page,
                bitmap_code_map=bitmap_code_map, plan=plans[mid],
            )
        except SegmentError as exc:
            raise ContainerError(f"message {mid}: {exc}") from exc
        replacements[offset] = new_segment
        per_message.append({
            "message_id": mid,
            "old_bytes": len(original),
            "new_bytes": len(new_segment),
        })

    # re-layout
    ordered_offsets = sorted(parsed.segments)
    new_text = bytearray()
    offset_remap: dict[int, int] = {}
    for old_offset in ordered_offsets:
        offset_remap[old_offset] = len(new_text)
        new_text.extend(replacements.get(old_offset, parsed.segments[old_offset]))
    new_count = parsed.glyph_count + appended
    new_table_start = 0x80
    new_text_start = align(new_table_start + parsed.mapping_count * 8)
    if new_count:
        new_width_start = align(new_text_start + len(new_text))
        new_bitmap_start = align(new_width_start + new_count)
        new_size = align(new_bitmap_start + new_count * glyph_bytes)
    else:
        # still-empty atlas: keep the original (zero) section pointers
        new_width_start = parsed.width_start
        new_bitmap_start = parsed.bitmap_start
        new_size = align(new_text_start + len(new_text))
    rebuilt = bytearray(new_size)
    rebuilt[:0x80] = decoded[:0x80]
    for field_offset, value in (
        (0x10, new_table_start), (0x14, new_text_start), (0x18, new_width_start),
        (0x1C, new_bitmap_start), (0x20, new_count), (0x40, new_size),
    ):
        struct.pack_into("<I", rebuilt, field_offset, value)
    for index, (mid, old_offset) in enumerate(parsed.rows):
        struct.pack_into("<II", rebuilt, new_table_start + index * 8, mid, offset_remap[old_offset])
    rebuilt[new_text_start:new_text_start + len(new_text)] = new_text
    rebuilt[new_width_start:new_width_start + parsed.glyph_count] = parsed.widths
    rebuilt[new_bitmap_start:new_bitmap_start + len(parsed.bitmaps)] = parsed.bitmaps
    for index, (width, bitmap) in zip(assigned_indices, rendered):
        if not 1 <= width <= parsed.glyph_width or len(bitmap) != glyph_bytes:
            raise AssertionError("rendered glyph has invalid geometry")
        rebuilt[new_width_start + index] = width
        start = new_bitmap_start + index * glyph_bytes
        rebuilt[start:start + glyph_bytes] = bitmap
    zeroed_glyphs = 0
    if zero_unused_glyphs and identity_code_map is None:
        assigned_set = set(assigned_indices)
        for index in range(parsed.glyph_count):
            if index in assigned_set or parsed.local_base + index in protected:
                continue
            start = new_bitmap_start + index * glyph_bytes
            rebuilt[start:start + glyph_bytes] = b"\0" * glyph_bytes
            rebuilt[new_width_start + index] = 1
            zeroed_glyphs += 1

    # post-conditions
    checked = Mclib.parse(bytes(rebuilt))
    if checked.local_base != parsed.local_base or checked.mapping_count != parsed.mapping_count:
        raise AssertionError("container identity changed")
    if (checked.glyph_width, checked.glyph_height, checked.glyph_stride) != (24, 24, 24):
        raise AssertionError("glyph geometry changed")
    for mid, old_offset in parsed.rows:
        new_segment = checked.segments[offset_remap[old_offset]]
        if old_offset not in target_offsets:
            if logical_segment(new_segment) != logical_segment(parsed.segments[old_offset]):
                raise AssertionError(f"non-target message {mid} changed")
    for code in sorted(protected):
        index = code - parsed.local_base
        if not 0 <= index < parsed.glyph_count:
            continue
        if checked.widths[index] != parsed.widths[index]:
            raise AssertionError(f"protected glyph width changed for code {code}")
        if (checked.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]
                != parsed.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]):
            raise AssertionError(f"protected glyph bitmap changed for code {code}")

    # decode-back proof for every translated message
    code_to_char: dict[int, str] = {}
    for ch, code in char_to_code.items():
        if code in code_to_char and code_to_char[code] != ch:
            raise AssertionError(f"char_to_code is not invertible at code {code}")
        code_to_char[code] = ch
    for sha8, code in bitmap_code_map.items():
        token = f"⟦G:{sha8}⟧"
        if code in code_to_char and code_to_char[code] != token:
            raise AssertionError(f"bitmap code {code} collides with a character mapping")
        code_to_char[code] = token
    for offset, mid in target_offsets.items():
        entry = entries[mid]
        new_plan = analyze_segment(checked.segments[offset_remap[offset]], table, positional, drop, structural_page)
        body, speaker, unknown = struct_to_text(new_plan, code_to_char)
        if unknown and not entry.keep_speaker:
            raise AssertionError(f"message {mid}: unknown glyphs after rebuild")
        expected_pages = parse_translated_text(entry.korean)
        got_pages = parse_translated_text(body)
        if len(got_pages) != len(new_plan.pages):
            raise AssertionError(f"message {mid}: page structure mismatch after rebuild")
        for p, got_lines in enumerate(got_pages):
            exp_lines = expected_pages[p] if p < len(expected_pages) else []
            for l, got_line in enumerate(got_lines):
                exp_line = exp_lines[l] if l < len(exp_lines) else []
                if got_line != exp_line:
                    raise AssertionError(
                        f"message {mid}: decode-back mismatch page {p} line {l}")
        if not entry.keep_speaker and entry.speaker_korean is not None:
            if parse_inline_units(speaker or "") != parse_inline_units(entry.speaker_korean):
                raise AssertionError(f"message {mid}: speaker decode-back mismatch")

    report = {
        "message_count": len(entries),
        "old_mclib_bytes": len(decoded),
        "new_mclib_bytes": len(rebuilt),
        "old_glyph_count": parsed.glyph_count,
        "new_glyph_count": new_count,
        "protected_local_glyphs": len(protected),
        "reused_local_glyphs": reused,
        "appended_local_glyphs": appended,
        "zeroed_unused_glyphs": zeroed_glyphs,
        "preserved_bitmap_glyphs": len(bitmap_code_map),
        "translation_character_count": len(characters) if identity_code_map is None else 0,
        "bitmap_layout_optimized": optimize_bitmap_layout,
        "identity_mode": identity_code_map is not None,
        "messages": per_message,
    }
    return bytes(rebuilt), report


# ---------------------------------------------------------------------------
# compression
# ---------------------------------------------------------------------------

def compress_checked(data: bytes) -> bytes:
    """Optimal-parse SLZ mode 2 with a mandatory round-trip; greedy fallback."""
    try:
        compressed = compress_slz_mode2_optimal(data)
        if decompress_slz_payload(compressed, 2, len(data)) == data:
            return compressed
    except Exception:
        pass
    compressed = compress_slz_mode2(data)
    if decompress_slz_payload(compressed, 2, len(data)) != data:
        raise PatchError("SLZ mode-2 round-trip failed for both compressors")
    return compressed


# ---------------------------------------------------------------------------
# archive layout
# ---------------------------------------------------------------------------

@dataclass
class Member:
    offset: int          # absolute within archive
    mode: int
    comp: int
    unpacked: int
    next_rel: int
    span: int            # offset .. next member start (last: 16 + comp)


@dataclass
class Row:
    tag: bytes | None
    rid: int | None
    aux: int | None
    offset: int          # absolute record start
    size: int | None     # PK1 size; PACK: implied (None for the last record)
    end: int             # absolute record end (implied for PACK)
    members: list[Member] | None  # SLZ chain content, else None


@dataclass
class Package:
    kind: str            # 'pk1' | 'pack' | 'chain'
    start: int
    extent: int          # absolute content end
    boundary: int        # absolute start of the next package (or archive end)
    rows: list[Row]
    header_size: int = 0
    count: int = 0


def _walk_chain(data: bytes, start: int, limit: int) -> list[Member]:
    members: list[Member] = []
    cursor = start
    while True:
        if cursor + 16 > limit or data[cursor:cursor + 3] != b"SLZ":
            raise LayoutError(f"bad SLZ chain member at 0x{cursor:X}")
        mode = data[cursor + 3]
        comp, unpacked, next_rel = struct.unpack_from("<III", data, cursor + 4)
        if cursor + 16 + comp > limit:
            raise LayoutError(f"SLZ member payload exceeds bounds at 0x{cursor:X}")
        if next_rel:
            if next_rel < 16 + comp or cursor + next_rel + 16 > limit:
                raise LayoutError(f"bad next_rel at 0x{cursor:X}")
            members.append(Member(cursor, mode, comp, unpacked, next_rel, next_rel))
            cursor += next_rel
        else:
            members.append(Member(cursor, mode, comp, unpacked, 0, 16 + comp))
            return members


def _parse_pk1_package(data: bytes, start: int) -> Package:
    if u32(data, start) != 0:
        raise LayoutError(f"PK1 leading word not zero at 0x{start:X}")
    count, header_size, reserved = struct.unpack_from("<III", data, start + 4)
    if not 1 <= count <= 1000 or header_size != 0x10 + count * 16 or reserved != 0:
        raise LayoutError(f"PK1 header geometry mismatch at 0x{start:X}")
    if start + header_size > len(data):
        raise LayoutError(f"truncated PK1 table at 0x{start:X}")
    rows: list[Row] = []
    for index in range(count):
        tag, rid, size, offset = struct.unpack_from("<4sIII", data, start + 0x10 + index * 16)
        absolute = start + offset
        if size <= 0 or offset < header_size or absolute + size > len(data):
            raise LayoutError(f"PK1 row {index} outside archive at 0x{start:X}")
        members = None
        if data[absolute:absolute + 3] == b"SLZ":
            members = _walk_chain(data, absolute, absolute + size)
        rows.append(Row(tag, rid, None, absolute, size, absolute + size, members))
    extent = max(row.end for row in rows)
    return Package("pk1", start, extent, -1, rows, header_size, count)


def _parse_pack_package(data: bytes, start: int) -> Package:
    if data[start:start + 4] != b"PACK":
        raise LayoutError(f"PACK magic missing at 0x{start:X}")
    low, count = struct.unpack_from("<HH", data, start + 4)
    header_size = u32(data, start + 8)
    if low != 0 or not 1 <= count <= 1000:
        raise LayoutError(f"unsupported PACK header at 0x{start:X}")
    if header_size < 0x10 + 4 + (count - 1) * 8 or start + header_size > len(data):
        raise LayoutError(f"bad PACK header size at 0x{start:X}")
    offsets = [start + header_size]
    auxes = [u32(data, start + 0xC)]
    for k in range(count - 1):
        offsets.append(start + u32(data, start + 0x10 + k * 8))
        auxes.append(u32(data, start + 0x14 + k * 8))
    used = 0x14 + (count - 1) * 8
    if any(data[start + used:start + header_size]):
        raise LayoutError(f"non-zero PACK header tail at 0x{start:X}")
    for a, b in zip(offsets, offsets[1:]):
        if not (start < a < b <= len(data)):
            raise LayoutError(f"PACK offsets not ascending at 0x{start:X}")
    if any((off - start) % 0x80 for off in offsets):
        raise LayoutError(f"PACK record offsets not 0x80-aligned at 0x{start:X}")
    tail = data[offsets[-1]:]
    content_end = offsets[-1] + max(len(tail.rstrip(b"\0")), 1)
    rows: list[Row] = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else content_end
        members = None
        if data[offset:offset + 3] == b"SLZ":
            members = _walk_chain(data, offset, end)
        rows.append(Row(None, index, auxes[index], offset, None, end, members))
    return Package("pack", start, content_end, -1, rows, header_size, count)


def _find_next_package(data: bytes, extent: int) -> int:
    """Boundary = start of the next package; all bytes in between must be zero."""
    nonzero = first_nonzero(data, extent)
    if nonzero < 0:
        return len(data)
    q = nonzero & ~3
    for candidate in (q - 4, q):
        if candidate < extent:
            continue
        if has_nonzero(data, extent, candidate):
            break
        head = data[candidate:candidate + 4]
        try:
            if head[:3] == b"SLZ":
                _walk_chain(data, candidate, len(data))
                return candidate
            if head == b"PACK":
                _parse_pack_package(data, candidate)
                return candidate
            if candidate + 16 <= len(data) and u32(data, candidate) == 0:
                _parse_pk1_package(data, candidate)
                return candidate
        except LayoutError:
            continue
    raise LayoutError(
        f"unrecognized data after package extent 0x{extent:X} (first non-zero at 0x{nonzero:X}: "
        f"{data[q:q + 16].hex()})")


def parse_archive_layout(data: bytes) -> list[Package]:
    packages: list[Package] = []
    cursor = 0
    while cursor < len(data):
        head = data[cursor:cursor + 4]
        if head[:3] == b"SLZ":
            members = _walk_chain(data, cursor, len(data))
            extent = members[-1].offset + 16 + members[-1].comp
            package = Package("chain", cursor, extent, -1,
                              [Row(None, None, None, cursor, None, extent, members)])
        elif head == b"PACK":
            package = _parse_pack_package(data, cursor)
            package.boundary = len(data)
            packages.append(package)
            return packages  # PACK record sizes are implied: it must be terminal
        elif len(head) == 4 and u32(data, cursor) == 0:
            package = _parse_pk1_package(data, cursor)
        else:
            raise LayoutError(f"unrecognized package at 0x{cursor:X}: {data[cursor:cursor + 16].hex()}")
        package.boundary = _find_next_package(data, package.extent)
        packages.append(package)
        cursor = package.boundary
    return packages


def locate_stream(packages: list[Package], source_offset: int) -> tuple[int, int, int]:
    """(package_idx, row_idx, chain_idx) of the SLZ member at source_offset."""
    for p, package in enumerate(packages):
        for r, row in enumerate(package.rows):
            if row.members is None:
                continue
            for c, member in enumerate(row.members):
                if member.offset == source_offset:
                    return p, r, c
    raise LayoutError(f"no SLZ member at archive offset 0x{source_offset:X}")


# ---------------------------------------------------------------------------
# archive rebuild (strategies A and B)
# ---------------------------------------------------------------------------

def _member_new_bytes(comp: bytes, unpacked: int, next_rel: int) -> bytes:
    return b"SLZ\x02" + struct.pack("<III", len(comp), unpacked, next_rel) + comp


def _rebuild_record(
    data: bytes,
    row: Row,
    replacements: dict[int, tuple[bytes, int]],
) -> bytes:
    """New record content; non-target members byte-identical (with padding)."""
    if not replacements:
        return bytes(data[row.offset:row.end])
    if row.members is None:
        raise FitError("record with replacements has no SLZ content")
    out = bytearray()
    last = len(row.members) - 1
    for index, member in enumerate(row.members):
        if index in replacements:
            comp, unpacked = replacements[index]
            if index == last:
                out += _member_new_bytes(comp, unpacked, 0)
            else:
                span = align4(16 + len(comp))
                body = _member_new_bytes(comp, unpacked, span)
                out += body + b"\0" * (span - len(body))
        else:
            end = member.offset + member.span if index != last else row.end
            out += data[member.offset:end]
    return bytes(out)


@dataclass
class TargetResult:
    address: tuple[int, int, int]
    old_comp: int
    old_unpacked: int
    new_comp: int
    new_unpacked: int
    container_report: dict[str, object]
    strategy: str = ""


def rebuild_archive(
    data: bytes,
    targets: dict[tuple[int, int, int], dict[int, object]],
    *,
    table: ControlTable | None = None,
    font_path: Path | None = None,
    positional: frozenset[int] = DEFAULT_POSITIONAL,
    drop: frozenset[int] = DEFAULT_DROP,
    structural_page: bool = True,
    identity_code_map: dict[str, int] | None = None,
    allow_bitmap_optimize_retry: bool = True,
    archive_id: int | None = None,
    compressor: Callable[[bytes], bytes] = compress_checked,
) -> tuple[bytes, dict[str, object]]:
    """Rebuild targeted SLZ members inside one archive.

    Strategy A (in-place) per member, then strategy B (joint package reflow).
    Package boundaries and archive length are invariant; non-target bytes are
    byte-identical (possibly shifted within their package by B).
    """
    table = table or default_control_table()
    packages = parse_archive_layout(data)
    by_package: dict[int, dict[tuple[int, int], dict[int, object]]] = defaultdict(dict)
    for (p, r, c), translations in targets.items():
        if not (0 <= p < len(packages)):
            raise LayoutError(f"target package {p} out of range", archive=archive_id)
        row = packages[p].rows[r] if 0 <= r < len(packages[p].rows) else None
        if row is None or row.members is None or not (0 <= c < len(row.members)):
            raise LayoutError(f"target member ({p},{r},{c}) not found", archive=archive_id)
        by_package[p][(r, c)] = translations

    result = bytearray(data)
    package_reports: list[dict[str, object]] = []
    target_results: dict[tuple[int, int, int], TargetResult] = {}

    for p in sorted(by_package):
        package = packages[p]
        wanted = by_package[p]
        optimize = {key: False for key in wanted}
        attempt = 0
        while True:
            attempt += 1
            rebuilt: dict[tuple[int, int], tuple[bytes, bytes, dict[str, object], Member]] = {}
            for (r, c), translations in wanted.items():
                member = package.rows[r].members[c]
                decoded = decompress_slz_payload(
                    data[member.offset + 16:member.offset + 16 + member.comp],
                    member.mode, member.unpacked)
                mclib_new, creport = rebuild_container(
                    decoded, translations, table=table, font_path=font_path,
                    positional=positional, drop=drop, structural_page=structural_page,
                    identity_code_map=identity_code_map,
                    optimize_bitmap_layout=optimize[(r, c)])
                comp = compressor(mclib_new)
                if decompress_slz_payload(comp, 2, len(mclib_new)) != mclib_new:
                    raise PatchError("compressor round-trip failed")
                rebuilt[(r, c)] = (mclib_new, comp, creport, member)

            # strategy A: every member fits its own allocation, nothing moves.
            # Growth is only allowed into bytes verified zero in the original.
            allocations: dict[tuple[int, int], int] = {}
            for (r, c), (mclib_new, comp, creport, member) in rebuilt.items():
                row = package.rows[r]
                last = c == len(row.members) - 1
                if last:
                    limit = package.boundary if package.kind == "chain" else row.end
                else:
                    limit = member.offset + member.span
                grow_zone_clean = not has_nonzero(data, member.offset + 16 + member.comp, limit)
                allocations[(r, c)] = (limit - member.offset) if grow_zone_clean else 16 + member.comp

            if all(16 + len(comp) <= allocations[key]
                   for key, (_, comp, _, _) in rebuilt.items()):
                for (r, c), (mclib_new, comp, creport, member) in rebuilt.items():
                    body = _member_new_bytes(comp, len(mclib_new), member.next_rel)
                    new_end = member.offset + len(body)
                    zero_end = member.offset + allocations[(r, c)]
                    result[member.offset:new_end] = body
                    result[new_end:zero_end] = b"\0" * (zero_end - new_end)
                    target_results[(p, r, c)] = TargetResult(
                        (p, r, c), member.comp, member.unpacked, len(comp), len(mclib_new),
                        creport, "A")
                package_reports.append({
                    "package": p, "kind": package.kind, "strategy": "A",
                    "available_gap": package.boundary - package.extent,
                    "gap_consumed": 0,
                })
                break

            # strategy B: joint reflow of the whole package
            try:
                new_bytes = _reflow_package(data, package, rebuilt)
            except FitError as exc:
                if allow_bitmap_optimize_retry and identity_code_map is None and not all(optimize.values()):
                    optimize = {key: True for key in wanted}
                    continue
                raise FitError(
                    "package reflow does not fit",
                    archive=archive_id, package=p,
                    needed=exc.info.get("needed"), available=exc.info.get("available"),
                    gap=package.boundary - package.extent,
                    records={f"{r}:{c}": {"old": member.comp, "new": len(comp)}
                             for (r, c), (_, comp, _, member) in rebuilt.items()},
                ) from exc
            result[package.start:package.boundary] = new_bytes
            for (r, c), (mclib_new, comp, creport, member) in rebuilt.items():
                target_results[(p, r, c)] = TargetResult(
                    (p, r, c), member.comp, member.unpacked, len(comp), len(mclib_new),
                    creport, "B")
            old_content = package.extent - package.start
            new_content = _content_len(new_bytes)
            package_reports.append({
                "package": p, "kind": package.kind, "strategy": "B",
                "available_gap": package.boundary - package.extent,
                "gap_consumed": new_content - old_content,
                "bitmap_optimized": any(optimize.values()),
                "attempts": attempt,
            })
            break

    new_data = bytes(result)
    if len(new_data) != len(data):
        raise AssertionError("archive length changed")
    _verify_archive_rebuild(data, new_data, packages, by_package, target_results)
    report = {
        "archive_id": archive_id,
        "packages": package_reports,
        "targets": {
            f"{p}:{r}:{c}": {
                "strategy": tr.strategy,
                "old_compressed": tr.old_comp,
                "new_compressed": tr.new_comp,
                "old_unpacked": tr.old_unpacked,
                "new_unpacked": tr.new_unpacked,
                "container": tr.container_report,
            }
            for (p, r, c), tr in sorted(target_results.items())
        },
    }
    return new_data, report


def _content_len(package_bytes: bytes) -> int:
    return len(package_bytes.rstrip(b"\0"))


def _reflow_package(
    data: bytes,
    package: Package,
    rebuilt: dict[tuple[int, int], tuple[bytes, bytes, dict[str, object], Member]],
) -> bytes:
    """New bytes for [package.start, package.boundary): targets resized,
    non-targets byte-identical but shifted; boundary must not move."""
    replacements_by_row: dict[int, dict[int, tuple[bytes, int]]] = defaultdict(dict)
    for (r, c), (mclib_new, comp, _, _) in rebuilt.items():
        replacements_by_row[r][c] = (comp, len(mclib_new))

    available = package.boundary - package.start
    if package.kind == "chain":
        if len(package.rows) != 1:
            raise AssertionError("chain package must have one row")
        record = _rebuild_record(data, package.rows[0], replacements_by_row.get(0, {}))
        needed = len(record)
        if needed > available:
            raise FitError("chain reflow exceeds gap", needed=needed, available=available)
        return record + b"\0" * (available - needed)

    if package.kind == "pk1":
        header = bytearray(data[package.start:package.start + package.header_size])
        cursor = package.header_size
        out = bytearray()
        prev_orig_end = package.header_size
        for index, row in enumerate(package.rows):
            orig_gap = (row.offset - package.start) - prev_orig_end
            if orig_gap < 0:
                raise LayoutError("PK1 rows overlap")
            record = _rebuild_record(data, row, replacements_by_row.get(index, {}))
            new_size = align4(len(record))
            record = record + b"\0" * (new_size - len(record))
            new_offset = cursor + orig_gap
            out += b"\0" * orig_gap + record
            struct.pack_into("<II", header, 0x10 + index * 16 + 8, new_size, new_offset)
            cursor = new_offset + new_size
            prev_orig_end = (row.offset - package.start) + row.size
        needed = package.header_size + len(out)
        if needed > available:
            raise FitError("PK1 reflow exceeds gap", needed=needed, available=available)
        return bytes(header) + bytes(out) + b"\0" * (available - needed)

    if package.kind == "pack":
        header = bytearray(data[package.start:package.start + package.header_size])
        out = bytearray()
        cursor = package.header_size
        prev_orig_end = package.header_size
        for index, row in enumerate(package.rows):
            orig_start_rel = row.offset - package.start
            pad = (-cursor) % 0x80
            extra = orig_start_rel - ((prev_orig_end + 0x7F) & ~0x7F)
            if extra < 0:
                extra = 0
            new_offset = cursor + pad + extra
            record = _rebuild_record(data, row, replacements_by_row.get(index, {}))
            out += b"\0" * (new_offset - cursor) + record
            if index > 0:
                struct.pack_into("<I", header, 0x10 + (index - 1) * 8, new_offset)
            elif new_offset != package.header_size:
                raise AssertionError("PACK record 0 must stay at header_size")
            cursor = new_offset + len(record)
            prev_orig_end = row.end - package.start
        needed = len(out) + package.header_size
        if needed > available:
            raise FitError("PACK reflow exceeds gap", needed=needed, available=available)
        return bytes(header) + bytes(out) + b"\0" * (available - needed)

    raise AssertionError(f"unknown package kind {package.kind}")


def _verify_archive_rebuild(
    original: bytes,
    patched: bytes,
    packages: list[Package],
    by_package: dict[int, dict[tuple[int, int], dict[int, object]]],
    target_results: dict[tuple[int, int, int], TargetResult],
) -> None:
    """Independent re-parse of the patched archive against invariants."""
    new_packages = parse_archive_layout(patched)
    if len(new_packages) != len(packages):
        raise AssertionError("package count changed")
    for p, (old_pkg, new_pkg) in enumerate(zip(packages, new_packages)):
        if (old_pkg.kind, old_pkg.start, old_pkg.boundary) != (new_pkg.kind, new_pkg.start, new_pkg.boundary):
            raise AssertionError(f"package {p} identity/boundary changed")
        if p not in by_package:
            if patched[old_pkg.start:old_pkg.boundary] != original[old_pkg.start:old_pkg.boundary]:
                raise AssertionError(f"untargeted package {p} changed")
            continue
        if len(new_pkg.rows) != len(old_pkg.rows):
            raise AssertionError(f"package {p} row count changed")
        targeted_rows = defaultdict(set)
        for (r, c) in by_package[p]:
            targeted_rows[r].add(c)
        for r, (old_row, new_row) in enumerate(zip(old_pkg.rows, new_pkg.rows)):
            if (old_row.tag, old_row.rid) != (new_row.tag, new_row.rid):
                raise AssertionError(f"package {p} row {r} identity changed")
            if old_pkg.kind == "pack" and old_row.aux != new_row.aux:
                raise AssertionError(f"package {p} record {r} aux changed")
            terminal_pack = old_pkg.kind == "pack" and r == len(old_pkg.rows) - 1
            if r not in targeted_rows:
                old_content = original[old_row.offset:old_row.end]
                new_content = patched[new_row.offset:new_row.end]
                if terminal_pack:
                    # implied-size terminal record: content bytes must survive
                    if new_content[:len(old_content)] != old_content:
                        raise AssertionError(f"package {p} terminal record changed")
                elif new_content != old_content:
                    raise AssertionError(f"package {p} non-target row {r} changed")
                continue
            # targeted row: member-level verification
            if old_row.members is None or new_row.members is None:
                raise AssertionError(f"package {p} row {r} lost its SLZ chain")
            if len(new_row.members) != len(old_row.members):
                raise AssertionError(f"package {p} row {r} member count changed")
            last = len(old_row.members) - 1
            for c, (old_m, new_m) in enumerate(zip(old_row.members, new_row.members)):
                if c in targeted_rows[r]:
                    tr = target_results[(p, r, c)]
                    decoded = decompress_slz_payload(
                        patched[new_m.offset + 16:new_m.offset + 16 + new_m.comp],
                        new_m.mode, new_m.unpacked)
                    if len(decoded) != tr.new_unpacked or new_m.comp != tr.new_comp:
                        raise AssertionError(f"target ({p},{r},{c}) verification mismatch")
                    Mclib.parse(decoded)  # structural sanity
                else:
                    old_end = old_m.offset + (old_m.span if c != last else 16 + old_m.comp)
                    new_end = new_m.offset + (new_m.span if c != last else 16 + new_m.comp)
                    if (original[old_m.offset:old_end]
                            != patched[new_m.offset:new_end]):
                        raise AssertionError(
                            f"package {p} row {r} non-target member {c} changed")


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------

def load_stream_manifest(path: Path = STREAM_MANIFEST_PATH) -> dict[tuple[int, int], dict[str, str]]:
    result: dict[tuple[int, int], dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            result[(int(row["archive_id"]), int(row["stream_id"]))] = row
    return result


def expand_unique_to_occurrences(
    translations_by_sha: dict[str, dict[int, object]],
    catalog_path: Path = CONTAINER_CATALOG_PATH,
) -> dict[tuple[int, int], dict[int, object]]:
    """Map unique-container translations (file_sha256) to every on-disc stream."""
    wanted = {sha.lower() for sha in translations_by_sha}
    result: dict[tuple[int, int], dict[int, object]] = {}
    with catalog_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            sha = row["file_sha256"].lower()
            if sha in wanted:
                result[(int(row["archive_id"]), int(row["stream_id"]))] = translations_by_sha[sha]
    return result


def build_plan(
    iso: Path,
    stream_targets: dict[tuple[int, int], dict[int, object]],
    manifest: dict[tuple[int, int], dict[str, str]] | None = None,
) -> dict[int, dict[tuple[int, int, int], dict[int, object]]]:
    """Resolve (archive, stream) targets to (package, row, chain) addresses."""
    manifest = manifest or load_stream_manifest()
    nested = [key for key in stream_targets
              if key in manifest and int(manifest[key]["depth"]) > 0]
    if nested:
        raise PatchError(
            "nested (depth>0) streams cannot be patched",
            nested=[f"{a}:{s}" for a, s in sorted(nested)])
    missing = [key for key in stream_targets if key not in manifest]
    if missing:
        raise PatchError("streams absent from manifest", missing=missing[:10])

    index = read_index(iso)
    plan: dict[int, dict[tuple[int, int, int], dict[int, object]]] = defaultdict(dict)
    by_archive: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for archive_id, stream_id in stream_targets:
        by_archive[archive_id].append((archive_id, stream_id))
    with iso.open("rb") as handle:
        for archive_id, keys in sorted(by_archive.items()):
            start = index[archive_id] * SECTOR
            size = index[0x1800 + archive_id] * SECTOR
            if start <= 0 or size <= 0:
                raise PatchError(f"archive {archive_id} absent from hidden index")
            handle.seek(start)
            data = handle.read(size)
            packages = parse_archive_layout(data)
            for key in keys:
                source_offset = int(manifest[key]["source_offset"])
                address = locate_stream(packages, source_offset)
                if address in plan[archive_id]:
                    raise PatchError(f"duplicate plan address {address} in archive {archive_id}")
                plan[archive_id][address] = stream_targets[key]
    return dict(plan)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def patch_archives(
    iso_in: Path,
    iso_out: Path | None,
    plan: dict[int, dict[tuple[int, int, int], dict[int, object]]],
    *,
    font_path: Path | None = None,
    table: ControlTable | None = None,
    positional: frozenset[int] = DEFAULT_POSITIONAL,
    drop: frozenset[int] = DEFAULT_DROP,
    structural_page: bool = True,
    identity_code_map: dict[str, int] | None = None,
    simulate: bool = False,
    allow_partial: bool = False,
) -> dict[str, object]:
    """Patch every archive in the plan; strategies A/B only, fail-closed.

    In simulate mode no ISO is produced; the report carries per-container
    old/new compressed sizes, per-package gap consumption and verdicts.
    """
    table = table or default_control_table()
    index = read_index(iso_in)
    archive_reports: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    patched: list[tuple[int, int, bytes]] = []

    with iso_in.open("rb") as handle:
        for archive_id in sorted(plan):
            start = index[archive_id] * SECTOR
            size = index[0x1800 + archive_id] * SECTOR
            if start <= 0 or size <= 0:
                failures.append({"archive": archive_id, "error": "absent from hidden index"})
                continue
            handle.seek(start)
            data = handle.read(size)
            try:
                new_data, report = rebuild_archive(
                    data, plan[archive_id], table=table, font_path=font_path,
                    positional=positional, drop=drop, structural_page=structural_page,
                    identity_code_map=identity_code_map, archive_id=archive_id)
            except (PatchError, AssertionError, ValueError) as exc:
                info = getattr(exc, "info", {})
                failures.append({"archive": archive_id, "error": str(exc), **info})
                continue
            report["iso_start"] = start
            report["archive_bytes"] = size
            report["changed"] = new_data != data
            archive_reports.append(report)
            if not simulate:
                patched.append((start, size, new_data))

    result: dict[str, object] = {
        "schema_version": 1,
        "mode": "simulate" if simulate else "patch",
        "input_iso": str(iso_in),
        "archives_planned": len(plan),
        "archives_ok": len(archive_reports),
        "archives_failed": len(failures),
        "failures": failures,
        "archives": archive_reports,
    }
    if simulate:
        return result
    if failures and not allow_partial:
        raise PatchError("archive failures; no ISO written", failures=failures)
    if iso_out is None:
        raise PatchError("iso_out required unless simulating")
    if iso_out.exists():
        raise PatchError(f"refusing to overwrite output ISO: {iso_out}")

    iso_out.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=iso_out.parent, prefix=f".{iso_out.name}.", suffix=".tmp", delete=False)
    temp_path = Path(handle.name)
    handle.close()
    try:
        shutil.copyfile(iso_in, temp_path)
        with temp_path.open("r+b") as out:
            for start, size, data in patched:
                out.seek(start)
                out.write(data)
                if out.tell() != start + size:
                    raise AssertionError("archive write length mismatch")
        if temp_path.stat().st_size != iso_in.stat().st_size:
            raise AssertionError("output ISO size changed")
        with temp_path.open("rb") as check:
            for start, size, data in patched:
                check.seek(start)
                if check.read(size) != data:
                    raise AssertionError(f"re-read verification failed at 0x{start:X}")
        os.replace(temp_path, iso_out)
    finally:
        temp_path.unlink(missing_ok=True)
    result["output_iso"] = str(iso_out)
    result["output_iso_sha256"] = sha256_file(iso_out)
    return result


def simulate(
    iso_in: Path,
    plan: dict[int, dict[tuple[int, int, int], dict[int, object]]],
    **kwargs: object,
) -> dict[str, object]:
    return patch_archives(iso_in, None, plan, simulate=True, **kwargs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _plan_from_json(
    iso: Path,
    document: dict[str, object],
    catalog_path: Path = CONTAINER_CATALOG_PATH,
    manifest_path: Path = STREAM_MANIFEST_PATH,
) -> dict[int, dict[tuple[int, int, int], dict[int, object]]]:
    streams: dict[tuple[int, int], dict[int, object]] = {}
    for key, translations in document.get("streams", {}).items():
        archive_id, stream_id = (int(v) for v in key.split(":"))
        streams[(archive_id, stream_id)] = {int(m): v for m, v in translations.items()}
    unique = document.get("unique", {})
    if unique:
        expanded = expand_unique_to_occurrences(
            {sha: {int(m): v for m, v in tr.items()} for sha, tr in unique.items()},
            catalog_path=catalog_path)
        for key, value in expanded.items():
            if key in streams:
                raise PatchError(f"stream {key} planned twice")
            streams[key] = value
    return build_plan(iso, streams, load_stream_manifest(manifest_path))


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso_in", type=Path)
    parser.add_argument("iso_out", type=Path, nargs="?")
    parser.add_argument("--plan", type=Path, required=True,
                        help='JSON: {"streams": {"aid:sid": {msgid: {korean, speaker_korean}}}, "unique": {...}}')
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT_PATH)
    parser.add_argument("--catalog", type=Path, default=CONTAINER_CATALOG_PATH,
                        help="container_catalog.csv used to expand `unique` plan "
                             "entries to on-disc occurrences (disc-2: full_ko_d2 catalog)")
    parser.add_argument("--stream-manifest", type=Path, default=STREAM_MANIFEST_PATH,
                        help="stream_manifest.csv used to resolve (archive, stream) "
                             "to SLZ member addresses (disc-2: disc2 manifests)")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.plan.read_text(encoding="utf-8"))
    plan = _plan_from_json(args.iso_in, document,
                           catalog_path=args.catalog, manifest_path=args.stream_manifest)
    if args.simulate:
        report = simulate(args.iso_in, plan, font_path=args.font)
    else:
        if args.iso_out is None:
            parser.error("iso_out is required unless --simulate")
        report = patch_archives(args.iso_in, args.iso_out, plan, font_path=args.font)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not report.get("failures") else 1


if __name__ == "__main__":
    sys.exit(main())

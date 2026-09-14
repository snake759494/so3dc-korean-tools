#!/usr/bin/env python3
"""Patch the 653 verified Hyda/Grantier dialogue occurrences into Korean.

This patcher is intentionally data driven and fail closed.  It consumes the
verified Japanese catalogue and a translation file grouped by exact segment
SHA-256.  Every physical (archive, stream, message) occurrence must be covered
exactly once before an output ISO is created.

Korean glyphs are stored in each scene's local 24 px mclib.  Slots that are
referenced by any non-target message are protected; target-only/unused slots
are reused first and only the deficit is appended.  The target SLZ record is
then rebuilt inside the first PK1 package.  Following records may move within
the verified zero gap, but later PK1 packages and all unrelated records remain
byte identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from so3_repack import (  # noqa: E402
    Mclib,
    align,
    compress_slz_mode2,
    decompress_slz_payload,
    encode_glyph_code,
    read_index,
    render_glyphs,
)


SUPPORTED_ISO_SIZE = 4_689_854_464
SUPPORTED_ISO_SHA256 = "95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826"
FONT_FILENAME = "NanumSquareNeo-cBd.ttf"
FONT_SHA256 = "4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767"
FONT_GRAY_LEVELS = 2
SECTOR = 0x800

EXPECTED_TRANSLATION_ENTRIES = 434
EXPECTED_OCCURRENCES = 653
EXPECTED_BANKS = 24

# stream_id is an analysis identity; each selected mclib is also required to
# be row 1 of the archive's first PK1 package before it can be changed.
BANK_STREAMS = {
    1204: 5829,
    1206: 5984,
    1208: 6005,
    1210: 6040,
    1212: 6061,
    1214: 6120,
    1216: 6141,
    1218: 6169,
    1220: 6194,
    1222: 6258,
    1224: 6438,
    1226: 6459,
    1228: 6543,
    1230: 6621,
    1232: 6653,
    1234: 6684,
    1236: 6715,
    1243: 6989,
    1245: 7017,
    1247: 7081,
    1249: 7154,
    1251: 7227,
    1253: 7300,
    1255: 7404,
}

CONTROL_SIZES = {
    b"\x80\x80": 2,  # newline
    b"\x81\x80": 2,
    b"\x82\x80": 3,
    b"\x84\x80": 2,
    b"\x85\x80": 6,
    b"\x86\x80": 6,
    b"\x87\x80": 2,
    b"\x88\x80": 3,
    b"\x89\x80": 2,
    b"\x8a\x80": 6,
    b"\x8b\x80": 2,
    b"\x90\x80": 2,  # ruby base start; removed with Japanese body glyphs
    b"\x92\x80": 3,
    b"\x93\x80": 3,  # visible character-name reference; translated literally
    b"\x94\x80": 6,
    b"\x95\x80": 6,
    b"\x9c\x80": 5,
    b"\x9d\x80": 3,
    b"\x9e\x80": 6,
    b"\x9f\x80": 2,
}
ZERO_TERMINATED_CONTROLS = {b"\x91\x80"}  # ruby metadata
NEWLINE = b"\x80\x80"
CHARACTER_REFERENCE = b"\x93\x80"
RUBY_CONTROLS = {b"\x90\x80", b"\x91\x80"}
SPACE_CODE = 232
TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")

# Directly identified slots in the unchanged global 24 px atlas.  Reusing
# these prevents punctuation, numbers and Latin labels from consuming a local
# Nanum bitmap in every scene.
GLOBAL_CODE_MAP = {
    **{str(value): value + 1 for value in range(10)},
    **{chr(ord("A") + value): 14 + value for value in range(26)},
    **{chr(ord("a") + value): 40 + value for value in range(26)},
    "-": 11,
    ".": 12,
    "'": 13,
    # Confirmed by the first-dialogue patch's global-atlas audit.
    ",": 258,
    " ": 232,
    "　": 233,
    "、": 235,
    "。": 237,
    "・": 239,
    "?": 241,
    "！": 243,
    "：": 259,
    "(": 263,
    "（": 264,
    "「": 272,
    "『": 273,
    "+": 278,
    "～": 283,
    "…": 284,
    "♪": 285,
}


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def p32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def reject_path_aliases(**paths: Path | None) -> None:
    active = [(name, path) for name, path in paths.items() if path is not None]
    for index, (left_name, left) in enumerate(active):
        for right_name, right in active[index + 1 :]:
            aliases = _path_key(left) == _path_key(right)
            if not aliases:
                try:
                    aliases = left.exists() and right.exists() and os.path.samefile(left, right)
                except OSError:
                    aliases = False
            if aliases:
                raise ValueError(
                    f"path collision: {right_name} aliases {left_name}: {right}"
                )


@dataclass(frozen=True)
class Occurrence:
    archive_id: int
    stream_id: int
    message_id: int


@dataclass(frozen=True)
class Translation:
    exact_sha256: str
    japanese: str | None
    korean: str
    speaker_korean: str | None


@dataclass(frozen=True)
class Token:
    kind: str
    raw: bytes
    code: int | None = None


def occurrence_from_dict(item: dict[str, object]) -> Occurrence:
    try:
        occurrence = Occurrence(
            int(item["archive_id"]),
            int(item["stream_id"]),
            int(item["message_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid translation occurrence: {item!r}") from exc
    expected_stream = BANK_STREAMS.get(occurrence.archive_id)
    if expected_stream != occurrence.stream_id:
        raise ValueError(
            f"occurrence outside verified Hyda banks: {occurrence}"
        )
    if occurrence.message_id < 0:
        raise ValueError(f"negative message id: {occurrence}")
    return occurrence


def _normalize_text(text: str, field: str) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{field} must be a string")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\0" in text:
        raise ValueError(f"{field} contains NUL")
    return text


def expand_name_tokens(text: str) -> str:
    """Encode ``{페이트}``-style translation tokens as literal Korean."""

    previous = None
    while previous != text:
        previous = text
        text = TOKEN_PATTERN.sub(lambda match: match.group(1), text)
    if "{" in text or "}" in text:
        raise ValueError(f"unbalanced translation token braces: {text!r}")
    return text


def load_patch_plan(
    catalogue_path: Path,
    translation_path: Path,
    *,
    expected_entries: int | None = EXPECTED_TRANSLATION_ENTRIES,
    expected_occurrences: int | None = EXPECTED_OCCURRENCES,
) -> tuple[dict[Occurrence, dict[str, object]], dict[Occurrence, Translation]]:
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
    rows = catalogue.get("dialogues")
    compact = False
    if not isinstance(rows, list):
        rows = catalogue.get("occurrences")
        compact = True
    if not isinstance(rows, list):
        raise ValueError("catalogue must contain dialogues or compact occurrences")

    source: dict[Occurrence, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("catalogue dialogue row must be an object")
        occurrence = occurrence_from_dict(row)
        if occurrence in source:
            raise ValueError(f"duplicate catalogue occurrence: {occurrence}")
        digest = str(row.get("exact_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"bad catalogue SHA-256 for {occurrence}")
        raw_hex = row.get("raw_bytes_hex")
        if raw_hex is not None:
            if not isinstance(raw_hex, str) or sha256(bytes.fromhex(raw_hex)) != digest:
                raise ValueError(f"catalogue raw bytes/hash mismatch for {occurrence}")
        elif not compact:
            raise ValueError(f"full catalogue raw bytes missing for {occurrence}")
        line_count = row.get("source_line_count")
        if line_count is None and isinstance(row.get("japanese"), str):
            line_count = len(_normalize_text(row["japanese"], "catalogue Japanese").split("\n"))
            row = {**row, "source_line_count": line_count}
        if not isinstance(line_count, int) or line_count <= 0:
            raise ValueError(f"invalid source_line_count for {occurrence}")
        speaker = row.get("speaker")
        evidence = row.get("evidence")
        if not isinstance(speaker, dict) or not isinstance(speaker.get("mode"), str):
            raise ValueError(f"speaker metadata missing for {occurrence}")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("body_start_offset"), int):
            raise ValueError(f"body offset metadata missing for {occurrence}")
        source[occurrence] = row

    document = json.loads(translation_path.read_text(encoding="utf-8"))
    entries = document.get("translations")
    if not isinstance(entries, list):
        raise ValueError("translation document must contain a translations array")
    if expected_entries is not None and len(entries) != expected_entries:
        raise ValueError(
            f"translation entry count {len(entries)} != {expected_entries}"
        )

    by_hash: set[str] = set()
    plan: dict[Occurrence, Translation] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("translation entry must be an object")
        digest = str(entry.get("exact_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid translation SHA-256: {digest!r}")
        if digest in by_hash:
            raise ValueError(f"duplicate translation exact_sha256: {digest}")
        by_hash.add(digest)
        japanese_value = entry.get("japanese")
        japanese = (
            _normalize_text(japanese_value, "japanese")
            if japanese_value is not None
            else None
        )
        korean = expand_name_tokens(_normalize_text(entry.get("korean"), "korean"))
        if not korean:
            raise ValueError(f"empty Korean translation for {digest}")
        speaker = entry.get("speaker_korean")
        if speaker is not None:
            speaker = expand_name_tokens(_normalize_text(speaker, "speaker_korean"))
            if not speaker or "\n" in speaker:
                raise ValueError(f"invalid speaker_korean for {digest}")
        translation = Translation(digest, japanese, korean, speaker)
        occurrences = entry.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            raise ValueError(f"translation {digest} has no occurrences")
        for raw_occurrence in occurrences:
            if not isinstance(raw_occurrence, dict):
                raise ValueError(f"invalid occurrence in translation {digest}")
            occurrence = occurrence_from_dict(raw_occurrence)
            if occurrence in plan:
                raise ValueError(f"translation occurrence covered twice: {occurrence}")
            source_row = source.get(occurrence)
            if source_row is None:
                raise ValueError(f"translation occurrence absent from catalogue: {occurrence}")
            if str(source_row["exact_sha256"]).lower() != digest:
                raise ValueError(f"translation hash mismatch for {occurrence}")
            if len(korean.split("\n")) != int(source_row["source_line_count"]):
                raise ValueError(f"translation line count mismatch for {occurrence}")
            source_japanese_value = source_row.get("japanese")
            if japanese is not None and source_japanese_value is not None:
                source_japanese = _normalize_text(
                    source_japanese_value, "catalogue japanese"
                )
                if source_japanese != japanese:
                    raise ValueError(f"translation Japanese source mismatch for {occurrence}")
            mode = source_row.get("speaker", {}).get("mode")
            if mode == "implicit_or_continuation":
                if speaker is not None:
                    raise ValueError(
                        f"implicit speaker must not be literalized for {occurrence}"
                    )
            elif speaker is None:
                raise ValueError(f"speaker_korean is required for {occurrence}")
            plan[occurrence] = translation

    if expected_occurrences is not None:
        if len(source) != expected_occurrences:
            raise ValueError(f"catalogue occurrence count {len(source)} != {expected_occurrences}")
        if len(plan) != expected_occurrences:
            raise ValueError(f"translated occurrence count {len(plan)} != {expected_occurrences}")
    if set(plan) != set(source):
        order = lambda item: (item.archive_id, item.stream_id, item.message_id)
        missing = sorted(set(source) - set(plan), key=order)
        extra = sorted(set(plan) - set(source), key=order)
        raise ValueError(
            f"translation coverage mismatch: missing={missing[:5]}, extra={extra[:5]}"
        )
    if expected_occurrences is not None:
        banks = {(item.archive_id, item.stream_id) for item in plan}
        if len(banks) != EXPECTED_BANKS:
            raise ValueError(f"translated bank count {len(banks)} != {EXPECTED_BANKS}")
    return source, plan


def glyph_at(data: bytes, offset: int) -> tuple[int | None, int]:
    first = data[offset]
    if first == 0:
        return None, 0
    if first < 0x80:
        return first, 1
    if offset + 1 < len(data) and data[offset + 1] < 0x80:
        return (first & 0x7F) | (data[offset + 1] << 7), 2
    return None, 0


def tokenize_body(data: bytes, start: int) -> tuple[list[Token], int]:
    tokens: list[Token] = []
    offset = start
    while offset < len(data):
        if data[offset] == 0:
            return tokens, offset
        code, size = glyph_at(data, offset)
        if code is not None:
            tokens.append(Token("glyph", data[offset : offset + size], code))
            offset += size
            continue
        if offset + 1 >= len(data):
            raise ValueError(f"truncated body opcode at {offset}")
        opcode = data[offset : offset + 2]
        if opcode in ZERO_TERMINATED_CONTROLS:
            end = data.find(b"\0", offset + 2)
            if end < 0:
                raise ValueError(f"unterminated body control {opcode.hex()} at {offset}")
            raw = data[offset : end + 1]
            offset = end + 1
        else:
            size = CONTROL_SIZES.get(opcode)
            if size is None or offset + size > len(data):
                raise ValueError(f"unsupported body control {opcode.hex()} at {offset}")
            raw = data[offset : offset + size]
            offset += size
        tokens.append(Token("control", raw))
    raise ValueError("dialogue body has no NUL terminator")


def split_token_lines(tokens: Sequence[Token]) -> list[list[Token]]:
    lines: list[list[Token]] = [[]]
    for token in tokens:
        if token.kind == "control" and token.raw == NEWLINE:
            lines.append([])
        else:
            lines[-1].append(token)
    return lines


def _visible_units(tokens: Sequence[Token]) -> int:
    return sum(
        token.kind == "glyph"
        or (token.kind == "control" and token.raw.startswith(CHARACTER_REFERENCE))
        for token in tokens
    )


def replace_line_tokens(
    tokens: Sequence[Token],
    translated: str,
    encode_character: Callable[[str], bytes],
) -> bytes:
    """Replace glyphs while retaining non-ruby controls at proportional anchors."""

    characters = list(translated)
    original_units = _visible_units(tokens)
    controls: list[tuple[int, bytes]] = []
    position = 0
    for token in tokens:
        if token.kind == "glyph":
            position += 1
            continue
        opcode = token.raw[:2]
        if opcode == CHARACTER_REFERENCE:
            # The authoritative Korean translation contains the literal name.
            position += 1
            continue
        if opcode in RUBY_CONTROLS:
            continue
        if original_units:
            anchor = round(position * len(characters) / original_units)
        else:
            anchor = 0
        controls.append((max(0, min(len(characters), anchor)), token.raw))

    output = bytearray()
    cursor = 0
    for boundary in range(len(characters) + 1):
        while cursor < len(controls) and controls[cursor][0] == boundary:
            output.extend(controls[cursor][1])
            cursor += 1
        if boundary < len(characters):
            output.extend(encode_character(characters[boundary]))
    if cursor != len(controls):
        raise AssertionError("control anchor ordering failure")
    return bytes(output)


def replace_dialogue_segment(
    original: bytes,
    source_row: dict[str, object],
    translation: Translation,
    encode_character: Callable[[str], bytes],
) -> bytes:
    digest = str(source_row["exact_sha256"]).lower()
    if sha256(original) != digest or digest != translation.exact_sha256:
        raise ValueError("source segment SHA-256 mismatch")
    if source_row.get("raw_bytes_hex") is not None and original.hex() != source_row["raw_bytes_hex"]:
        raise ValueError("source segment bytes differ from catalogue")

    evidence = source_row["evidence"]
    body_start = int(evidence["body_start_offset"])
    speaker_mode = source_row["speaker"]["mode"]
    if speaker_mode == "implicit_or_continuation":
        prefix = original[:body_start]
    else:
        field_start = int(evidence["speaker_field_start_offset"])
        field_end = int(evidence["speaker_field_end_offset"])
        if not 0 <= field_start <= field_end <= body_start <= len(original):
            raise ValueError("invalid speaker/body offsets in catalogue")
        if translation.speaker_korean is None:
            raise ValueError("explicit speaker lacks speaker_korean")
        speaker_bytes = b"".join(encode_character(ch) for ch in translation.speaker_korean)
        prefix = original[:field_start] + speaker_bytes + original[field_end:body_start]

    tokens, terminator = tokenize_body(original, body_start)
    korean_lines = translation.korean.split("\n")
    token_lines = split_token_lines(tokens)
    source_line_count = int(source_row["source_line_count"])
    if source_line_count != len(korean_lines):
        raise ValueError(
            "translation must preserve line count: "
            f"source={source_line_count}, Korean={len(korean_lines)}, bytecode={len(token_lines)}"
        )

    # The catalogue decoder strips leading/trailing newlines.  Four copies of
    # the simulator settings panel have one final control-only bytecode line
    # (8480/8180) after the last decoded text line.  Preserve such structural
    # tail lines verbatim; never accept an extra line containing visible data.
    if len(token_lines) < source_line_count:
        raise ValueError(
            "dialogue bytecode has fewer lines than the decoded source: "
            f"source={source_line_count}, bytecode={len(token_lines)}"
        )
    structural_tail = token_lines[source_line_count:]
    if any(_visible_units(line) for line in structural_tail):
        raise ValueError("unmatched bytecode line contains visible glyphs")
    output_lines = korean_lines + [""] * len(structural_tail)

    body = bytearray()
    for index, (line_tokens, line) in enumerate(zip(token_lines, output_lines)):
        if index:
            body.extend(NEWLINE)
        body.extend(replace_line_tokens(line_tokens, line, encode_character))
    body.append(0)
    if any(original[terminator + 1 :]):
        raise ValueError("non-zero bytes follow dialogue terminator")
    return prefix + bytes(body)


def conservative_local_codes(data: bytes, local_base: int, glyph_count: int) -> set[int]:
    """Find every possible local operand, including false positives in controls.

    Over-marking merely reduces reusable capacity; under-marking could corrupt a
    non-target message, so this scan intentionally examines every byte offset.
    """

    result: set[int] = set()
    upper = local_base + glyph_count
    for offset in range(len(data) - 1):
        first, second = data[offset], data[offset + 1]
        if first >= 0x80 and second < 0x80:
            code = (first & 0x7F) | (second << 7)
            if local_base <= code < upper:
                result.add(code)
    return result


def logical_segment(data: bytes) -> bytes:
    """Discard allocation padding while retaining one bytecode terminator."""

    if not data or not data.endswith(b"\0"):
        raise ValueError("mclib segment lacks a NUL terminator")
    return data.rstrip(b"\0") + b"\0"


def _ordered_characters(texts: Iterable[str]) -> list[str]:
    # Unicode order keeps related Hangul syllables adjacent and improves the
    # game's short-window SLZ compression compared with dialogue order.
    return sorted({
        character
        for text in texts
        for character in text
        if character != "\n" and character not in GLOBAL_CODE_MAP
    })


def render_glyphs_at_size(
    text: str,
    font_path: Path,
    pixel_size: int,
    gray_levels: int = FONT_GRAY_LEVELS,
) -> list[tuple[int, bytes]]:
    """Nanum renderer matching so3_repack, with an explicit pixel size."""

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), pixel_size)
    result: list[tuple[int, bytes]] = []
    for character in text:
        bbox = font.getbbox(character)
        if bbox is None:
            raise ValueError(f"font has no drawable glyph for {character!r}")
        advance = max(1, min(24, round(font.getlength(character))))
        image = Image.new("L", (24, 24), 0)
        draw = ImageDraw.Draw(image)
        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (24 - ink_width) // 2 - bbox[0])
        y = max(0, (24 - ink_height) // 2 - bbox[1])
        draw.text((x, y), character, font=font, fill=255)
        pixels = [
            round(value / 255 * (gray_levels - 1)) * 15 // (gray_levels - 1)
            for value in image.getdata()
        ]
        bitmap = bytes(
            pixels[index] | (pixels[index + 1] << 4)
            for index in range(0, len(pixels), 2)
        )
        result.append((advance, bitmap))
    return result


def optimize_glyph_order(
    characters: list[str], rendered: list[tuple[int, bytes]]
) -> tuple[list[str], list[tuple[int, bytes]]]:
    """Choose a deterministic nearest-neighbour bitmap order for short SLZ windows."""

    if len(characters) < 3:
        return characters, rendered
    glyph = dict(zip(characters, rendered))
    distances = {
        (left, right): sum(a != b for a, b in zip(glyph[left][1], glyph[right][1]))
        for left in characters
        for right in characters
    }
    best: tuple[int, str, list[str]] | None = None
    for start in characters:
        remaining = set(characters)
        remaining.remove(start)
        order = [start]
        cost = 0
        while remaining:
            previous = order[-1]
            following = min(
                remaining, key=lambda item: (distances[previous, item], ord(item))
            )
            cost += distances[previous, following]
            order.append(following)
            remaining.remove(following)
        candidate = (cost, "".join(order), order)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    assert best is not None
    ordered = best[2]
    return ordered, [glyph[character] for character in ordered]


def rebuild_mclib(
    decoded: bytes,
    rows: dict[int, tuple[dict[str, object], Translation]],
    font_path: Path,
    *,
    glyph_renderer: Callable[..., list[tuple[int, bytes]]] = render_glyphs,
    font_pixel_size: int = 22,
    optimize_bitmap_layout: bool = False,
) -> tuple[bytes, dict[str, object]]:
    parsed = Mclib.parse(decoded)
    if (parsed.glyph_width, parsed.glyph_height, parsed.glyph_stride) != (24, 24, 24):
        raise ValueError("Hyda dialogue patch requires a 24x24x24 local font")
    offsets_by_id = {message_id: offset for message_id, offset in parsed.rows}
    if len(offsets_by_id) != len(parsed.rows):
        raise ValueError("duplicate mclib message ids are unsupported")
    missing = sorted(set(rows) - set(offsets_by_id))
    if missing:
        raise ValueError(f"translated message ids absent from mclib: {missing}")

    target_offsets = {offsets_by_id[message_id] for message_id in rows}
    protected_codes: set[int] = set()
    for offset, segment in parsed.segments.items():
        if offset not in target_offsets:
            protected_codes.update(
                conservative_local_codes(segment, parsed.local_base, parsed.glyph_count)
            )
    reusable_indices = [
        index
        for index in range(parsed.glyph_count)
        if parsed.local_base + index not in protected_codes
    ]

    text_sources: list[str] = []
    for source_row, translation in rows.values():
        if translation.speaker_korean is not None:
            text_sources.append(translation.speaker_korean)
        text_sources.append(translation.korean)
    characters = _ordered_characters(text_sources)
    if characters and glyph_renderer is render_glyphs and font_pixel_size != 22:
        rendered = render_glyphs_at_size(
            "".join(characters), font_path, font_pixel_size
        )
    else:
        rendered = glyph_renderer(
            "".join(characters),
            font_path,
            parsed.glyph_width,
            parsed.glyph_height,
            parsed.glyph_stride,
            gray_levels=FONT_GRAY_LEVELS,
        ) if characters else []
    if len(rendered) != len(characters):
        raise AssertionError("glyph renderer returned the wrong glyph count")
    if optimize_bitmap_layout:
        characters, rendered = optimize_glyph_order(characters, rendered)

    reused_count = min(len(reusable_indices), len(characters))
    assigned_indices = reusable_indices[:reused_count]
    append_count = len(characters) - reused_count
    assigned_indices.extend(range(parsed.glyph_count, parsed.glyph_count + append_count))
    codes = [parsed.local_base + index for index in assigned_indices]
    if codes and max(codes) >= 0x4000:
        raise ValueError("local Korean glyph codes exceed the tested encoding range")
    code_map = dict(zip(characters, codes))

    def encode_character(character: str) -> bytes:
        global_code = GLOBAL_CODE_MAP.get(character)
        if global_code is not None:
            return encode_glyph_code(global_code)
        try:
            return encode_glyph_code(code_map[character])
        except KeyError as exc:
            raise ValueError(f"no Korean glyph mapping for {character!r}") from exc

    replacements: dict[int, bytes] = {}
    changed: list[dict[str, object]] = []
    for message_id, (source_row, translation) in rows.items():
        old_offset = offsets_by_id[message_id]
        original = parsed.segments[old_offset]
        replacement = replace_dialogue_segment(
            original, source_row, translation, encode_character
        )
        replacements[old_offset] = replacement
        changed.append(
            {
                "message_id": message_id,
                "exact_sha256": translation.exact_sha256,
                "old_bytes": len(original),
                "new_bytes": len(replacement),
                "old_sha256": sha256(original),
                "new_sha256": sha256(replacement),
            }
        )

    ordered_offsets = sorted(parsed.segments)
    new_text = bytearray()
    offset_remap: dict[int, int] = {}
    for old_offset in ordered_offsets:
        offset_remap[old_offset] = len(new_text)
        new_text.extend(replacements.get(old_offset, parsed.segments[old_offset]))

    glyph_bytes = parsed.glyph_stride * parsed.glyph_height // 2
    new_count = parsed.glyph_count + append_count
    new_table_start = 0x80
    new_text_start = align(new_table_start + parsed.mapping_count * 8)
    new_width_start = align(new_text_start + len(new_text))
    new_bitmap_start = align(new_width_start + new_count)
    new_size = align(new_bitmap_start + new_count * glyph_bytes)
    rebuilt = bytearray(new_size)
    rebuilt[:0x80] = decoded[:0x80]
    for field_offset, value in (
        (0x10, new_table_start),
        (0x14, new_text_start),
        (0x18, new_width_start),
        (0x1C, new_bitmap_start),
        (0x20, new_count),
        (0x40, new_size),
    ):
        p32(rebuilt, field_offset, value)
    for index, (message_id, old_offset) in enumerate(parsed.rows):
        struct.pack_into(
            "<II", rebuilt, new_table_start + index * 8,
            message_id, offset_remap[old_offset],
        )
    rebuilt[new_text_start : new_text_start + len(new_text)] = new_text
    rebuilt[new_width_start : new_width_start + parsed.glyph_count] = parsed.widths
    rebuilt[new_bitmap_start : new_bitmap_start + len(parsed.bitmaps)] = parsed.bitmaps

    for index, (width, bitmap) in zip(assigned_indices, rendered):
        if not 1 <= width <= parsed.glyph_width or len(bitmap) != glyph_bytes:
            raise ValueError("rendered local glyph has invalid geometry")
        rebuilt[new_width_start + index] = width
        start = new_bitmap_start + index * glyph_bytes
        rebuilt[start : start + glyph_bytes] = bitmap

    checked = Mclib.parse(bytes(rebuilt))
    new_offsets_by_id = {message_id: offset for message_id, offset in checked.rows}
    for message_id, old_offset in parsed.rows:
        old_segment = parsed.segments[old_offset]
        new_segment = checked.segments[new_offsets_by_id[message_id]]
        if message_id in rows:
            if sha256(new_segment) == sha256(old_segment):
                raise AssertionError(f"target message {message_id} did not change")
        elif logical_segment(new_segment) != logical_segment(old_segment):
            raise AssertionError(f"non-target message {message_id} changed")

    protected_indices = {code - parsed.local_base for code in protected_codes}
    for index in protected_indices:
        old_start = index * glyph_bytes
        new_start = index * glyph_bytes
        if checked.widths[index] != parsed.widths[index]:
            raise AssertionError(f"protected glyph width {index} changed")
        if checked.bitmaps[new_start : new_start + glyph_bytes] != parsed.bitmaps[
            old_start : old_start + glyph_bytes
        ]:
            raise AssertionError(f"protected glyph bitmap {index} changed")

    return bytes(rebuilt), {
        "message_count": len(rows),
        "old_mclib_bytes": len(decoded),
        "new_mclib_bytes": len(rebuilt),
        "old_glyph_count": parsed.glyph_count,
        "new_glyph_count": new_count,
        "translation_characters": "".join(characters),
        "translation_character_count": len(characters),
        "font_pixel_size": font_pixel_size,
        "font_gray_levels": FONT_GRAY_LEVELS,
        "bitmap_layout_optimized": optimize_bitmap_layout,
        "global_characters_reused": "".join(sorted({
            character
            for text in text_sources
            for character in text
            if character in GLOBAL_CODE_MAP
        })),
        "protected_local_glyphs": len(protected_codes),
        "reusable_local_glyphs": len(reusable_indices),
        "reused_local_glyphs": reused_count,
        "appended_local_glyphs": append_count,
        "changed_messages": sorted(changed, key=lambda item: int(item["message_id"])),
    }


def parse_pk1_table(archive: bytes | bytearray) -> list[tuple[bytes, int, int, int]]:
    if len(archive) < 16 or u32(archive, 0) != 0:
        raise ValueError("PK1 leading word mismatch")
    count, header_size, reserved = struct.unpack_from("<III", archive, 4)
    if not 1 <= count <= 1000 or header_size != 0x10 + count * 16 or reserved != 0:
        raise ValueError("PK1 header geometry mismatch")
    if header_size > len(archive):
        raise ValueError("truncated PK1 table")
    rows = [
        struct.unpack_from("<4sIII", archive, 0x10 + index * 16)
        for index in range(count)
    ]
    for _, _, size, offset in rows:
        if size <= 0 or offset < header_size or offset + size > len(archive):
            raise ValueError("PK1 record is outside archive")
    return rows


def first_package_boundary(archive: bytes, rows: Sequence[tuple[bytes, int, int, int]]) -> tuple[int, int]:
    first_end = max(offset + size for _, _, size, offset in rows)
    candidates: list[int] = []
    for offset in range((first_end + 3) & ~3, len(archive) - 15, 4):
        zero, count, header_size, reserved = struct.unpack_from("<IIII", archive, offset)
        if (
            zero == 0
            and 1 <= count <= 1000
            and header_size == 0x10 + count * 16
            and reserved == 0
            and offset + header_size <= len(archive)
        ):
            try:
                parse_pk1_table(archive[offset:])
            except ValueError:
                continue
            candidates.append(offset)
    boundary = min(candidates, default=len(archive))
    if any(archive[first_end:boundary]):
        raise ValueError("bytes between first PK1 package and boundary are not zero")
    return first_end, boundary


def decode_inner_slz(
    archive: bytes | bytearray, member_offset: int
) -> tuple[bytes, dict[str, int]]:
    header = bytes(archive[member_offset : member_offset + 16])
    if len(header) != 16 or header[:3] != b"SLZ":
        raise ValueError(f"SLZ header missing at archive+0x{member_offset:X}")
    mode, compressed, unpacked, next_rel = (
        header[3], u32(header, 4), u32(header, 8), u32(header, 12)
    )
    payload = bytes(archive[member_offset + 16 : member_offset + 16 + compressed])
    if len(payload) != compressed:
        raise ValueError("truncated SLZ payload")
    return decompress_slz_payload(payload, mode, unpacked), {
        "mode": mode,
        "compressed": compressed,
        "unpacked": unpacked,
        "next_rel": next_rel,
    }


def replace_first_pk1_record(
    original: bytes,
    target_index: int,
    new_record: bytes,
) -> tuple[bytes, dict[str, int]]:
    rows = parse_pk1_table(original)
    if not 0 <= target_index < len(rows):
        raise ValueError("target PK1 row index is outside table")
    tag, record_id, old_size, target_offset = rows[target_index]
    first_end, boundary = first_package_boundary(original, rows)
    new_size = (len(new_record) + 3) & ~3
    new_record = new_record + b"\0" * (new_size - len(new_record))
    delta = new_size - old_size
    new_first_end = first_end + delta
    if new_first_end > boundary:
        raise ValueError(
            f"archive PK1 growth {delta} exceeds verified gap {boundary - first_end} "
            f"by {new_first_end - boundary} bytes"
        )

    rebuilt = bytearray(original)
    suffix = original[target_offset + old_size : first_end]
    rebuilt[target_offset : target_offset + new_size] = new_record
    suffix_start = target_offset + new_size
    rebuilt[suffix_start : suffix_start + len(suffix)] = suffix
    rebuilt[new_first_end:boundary] = b"\0" * (boundary - new_first_end)
    p32(rebuilt, 0x18 + target_index * 16, new_size)
    for index, (_, _, _, offset) in enumerate(rows):
        if offset > target_offset:
            p32(rebuilt, 0x1C + index * 16, offset + delta)

    checked = parse_pk1_table(rebuilt)
    for index, (old_row, new_row) in enumerate(zip(rows, checked)):
        old_tag, old_id, old_record_size, old_offset = old_row
        new_tag, new_id, new_record_size, new_offset = new_row
        if (new_tag, new_id) != (old_tag, old_id):
            raise AssertionError(f"PK1 row {index} identity changed")
        if index == target_index:
            if (new_record_size, new_offset) != (new_size, old_offset):
                raise AssertionError("target PK1 metadata mismatch")
        else:
            expected_offset = old_offset + delta if old_offset > target_offset else old_offset
            if (new_record_size, new_offset) != (old_record_size, expected_offset):
                raise AssertionError(f"PK1 row {index} relocation mismatch")
            if bytes(rebuilt[new_offset : new_offset + new_record_size]) != original[
                old_offset : old_offset + old_record_size
            ]:
                raise AssertionError(f"non-target PK1 row {index} changed")
    if bytes(rebuilt[boundary:]) != original[boundary:]:
        raise AssertionError("later PK1 packages changed")
    return bytes(rebuilt), {
        "target_tag": tag.decode("ascii", "replace"),
        "target_record_id": record_id,
        "old_record_size": old_size,
        "new_record_size": new_size,
        "record_growth": delta,
        "old_first_package_end": first_end,
        "new_first_package_end": new_first_end,
        "next_package_or_archive_end": boundary,
        "old_gap": boundary - first_end,
        "new_gap": boundary - new_first_end,
    }


def patch_archive(
    original: bytes,
    archive_id: int,
    rows: dict[int, tuple[dict[str, object], Translation]],
    font_path: Path,
) -> tuple[bytes, dict[str, object]]:
    table = parse_pk1_table(original)
    if len(table) < 2:
        raise ValueError(f"archive {archive_id} has no row-1 scene mclib")
    tag, record_id, record_size, member_offset = table[1]
    if (tag, record_id) != (b"DCMS", 0):
        raise ValueError(f"archive {archive_id} row 1 is not the scene DCMS record")
    decoded, old_slz = decode_inner_slz(original, member_offset)
    if old_slz["mode"] != 2 or old_slz["next_rel"] != 0:
        raise ValueError(f"archive {archive_id} target is not an unchained mode-2 SLZ")
    # Bitmap-neighbour ordering keeps archive 1245's appended Nanum glyph
    # stream inside its 488-byte tail gap.  Every bank retains a 22 px raster.
    rebuilt_mclib, mclib_details = rebuild_mclib(
        decoded,
        rows,
        font_path,
        font_pixel_size=22,
        optimize_bitmap_layout=archive_id == 1245,
    )
    compressed = compress_slz_mode2(rebuilt_mclib)
    if decompress_slz_payload(compressed, 2, len(rebuilt_mclib)) != rebuilt_mclib:
        raise AssertionError("rebuilt mclib SLZ round trip failed")
    record = b"SLZ\x02" + struct.pack(
        "<III", len(compressed), len(rebuilt_mclib), old_slz["next_rel"]
    ) + compressed
    rebuilt_archive, pk1_details = replace_first_pk1_record(original, 1, record)
    verified_rows = parse_pk1_table(rebuilt_archive)
    verified_mclib, new_slz = decode_inner_slz(rebuilt_archive, verified_rows[1][3])
    if verified_mclib != rebuilt_mclib:
        raise AssertionError("archive mclib verification mismatch")
    return rebuilt_archive, {
        "archive_id": archive_id,
        "stream_id": BANK_STREAMS[archive_id],
        "old_archive_sha256": sha256(original),
        "new_archive_sha256": sha256(rebuilt_archive),
        "archive_bytes": len(original),
        "old_slz_compressed": old_slz["compressed"],
        "new_slz_compressed": new_slz["compressed"],
        **pk1_details,
        **mclib_details,
    }


def patch_iso(
    input_iso: Path,
    output_iso: Path,
    catalogue_path: Path,
    translation_path: Path,
    font_path: Path,
) -> dict[str, object]:
    reject_path_aliases(
        input_iso=input_iso,
        output_iso=output_iso,
        catalogue=catalogue_path,
        translations=translation_path,
        font=font_path,
    )
    if output_iso.exists():
        raise FileExistsError(f"refusing to overwrite output ISO: {output_iso}")
    if input_iso.stat().st_size != SUPPORTED_ISO_SIZE:
        raise ValueError("unsupported input ISO size")
    input_digest = sha256_file(input_iso)
    if input_digest != SUPPORTED_ISO_SHA256:
        raise ValueError(f"unsupported input ISO SHA-256: {input_digest}")
    font_digest = sha256_file(font_path)
    if font_digest != FONT_SHA256:
        raise ValueError(f"unsupported NanumSquareNeo font SHA-256: {font_digest}")

    source, plan = load_patch_plan(catalogue_path, translation_path)
    grouped: dict[int, dict[int, tuple[dict[str, object], Translation]]] = defaultdict(dict)
    for occurrence, translation in plan.items():
        if occurrence.message_id in grouped[occurrence.archive_id]:
            raise AssertionError("duplicate message id after occurrence grouping")
        grouped[occurrence.archive_id][occurrence.message_id] = (
            source[occurrence], translation
        )
    if set(grouped) != set(BANK_STREAMS):
        raise ValueError("translation plan does not cover all 24 Hyda banks")

    index = read_index(input_iso)
    archives: list[tuple[int, int, bytes]] = []
    reports: list[dict[str, object]] = []
    for archive_id in sorted(grouped):
        start = index[archive_id] * SECTOR
        size = index[0x1800 + archive_id] * SECTOR
        if start <= 0 or size <= 0:
            raise ValueError(f"archive {archive_id} is absent from the hidden index")
        with input_iso.open("rb") as handle:
            handle.seek(start)
            original = handle.read(size)
        if len(original) != size:
            raise ValueError(f"short archive read for {archive_id}")
        rebuilt, report = patch_archive(
            original, archive_id, grouped[archive_id], font_path
        )
        if len(rebuilt) != len(original):
            raise AssertionError("in-place archive rebuild changed archive size")
        archives.append((start, size, rebuilt))
        reports.append(report)

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        dir=output_iso.parent,
        prefix=f".{output_iso.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_iso = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        shutil.copyfile(input_iso, temporary_iso)
        with temporary_iso.open("r+b") as output:
            for start, size, archive in archives:
                output.seek(start)
                output.write(archive)
                if output.tell() != start + size:
                    raise AssertionError("archive write length mismatch")
        if temporary_iso.stat().st_size != input_iso.stat().st_size:
            raise AssertionError("output ISO size changed")

        # Verify all 653 physical segments before the temporary file is
        # atomically promoted to the requested output path.
        verified_occurrences = 0
        for report in reports:
            archive_id = int(report["archive_id"])
            start = index[archive_id] * SECTOR
            size = index[0x1800 + archive_id] * SECTOR
            with temporary_iso.open("rb") as handle:
                handle.seek(start)
                archive = handle.read(size)
            if sha256(archive) != report["new_archive_sha256"]:
                raise AssertionError(f"output archive {archive_id} hash mismatch")
            parsed_rows = parse_pk1_table(archive)
            decoded, _ = decode_inner_slz(archive, parsed_rows[1][3])
            mclib = Mclib.parse(decoded)
            messages = {
                message_id: mclib.segments[offset]
                for message_id, offset in mclib.rows
            }
            for message_id, (_, translation) in grouped[archive_id].items():
                if message_id not in messages:
                    raise AssertionError("verified target message disappeared")
                if sha256(messages[message_id]) == translation.exact_sha256:
                    raise AssertionError("verified target message remains Japanese")
                verified_occurrences += 1
        if verified_occurrences != EXPECTED_OCCURRENCES:
            raise AssertionError(
                f"verified occurrence count {verified_occurrences} != "
                f"{EXPECTED_OCCURRENCES}"
            )
        if output_iso.exists():
            raise FileExistsError(f"refusing to overwrite output ISO: {output_iso}")
        os.replace(temporary_iso, output_iso)
    finally:
        temporary_iso.unlink(missing_ok=True)

    return {
        "schema_version": 1,
        "status": "static_verification_complete_runtime_unverified",
        "input_iso": str(input_iso),
        "output_iso": str(output_iso),
        "input_iso_sha256": input_digest,
        "output_iso_sha256": sha256_file(output_iso),
        "font": str(font_path),
        "font_sha256": font_digest,
        "translation_entries": EXPECTED_TRANSLATION_ENTRIES,
        "translated_occurrences": verified_occurrences,
        "archive_count": len(reports),
        "archives": reports,
        "emulator_tested": False,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_iso", type=Path)
    parser.add_argument("output_iso", type=Path)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument(
        "--font",
        type=Path,
        help="NanumSquareNeo-cBd.ttf; defaults to the input ISO directory",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    font = args.font or args.input_iso.with_name(FONT_FILENAME)
    try:
        reject_path_aliases(
            input_iso=args.input_iso,
            output_iso=args.output_iso,
            catalogue=args.catalogue,
            translations=args.translations,
            font=font,
            report=args.report,
        )
        result = patch_iso(
            args.input_iso,
            args.output_iso,
            args.catalogue,
            args.translations,
            font,
        )
    except (OSError, ValueError, AssertionError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

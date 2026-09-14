#!/usr/bin/env python3
"""Verify the 653-occurrence Hyda Korean dialogue ISO through a separate path.

The verifier deliberately does not import ``patch_hyda_dialogue``.  It shares
only the low-level index/SLZ/mclib library, then separately parses PK1 packages
and dialogue controls and decodes Korean speaker/body glyphs by matching the
patched atlas to fresh NanumSquareNeo rasterizations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from so3_repack import Mclib, decompress_slz_payload, read_index  # noqa: E402


ORIGINAL_ISO_SIZE = 4_689_854_464
ORIGINAL_ISO_SHA256 = "95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826"
EXPECTED_PATCHED_ISO_SHA256: str | None = (
    "7080D2226400C5B3747C7FE92F939E9DAD1EFAE580A81CAA2D371D885238E464"
)
NANUM_FONT_SHA256 = "4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767"
GRAY_LEVELS = 2
SECTOR = 0x800
INDEX_OFFSET = 0x200000
INDEX_BYTES = 0x1800 * 3 * 4
EXPECTED_TRANSLATIONS = 434
EXPECTED_OCCURRENCES = 653

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
    b"\x80\x80": 2,
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
    b"\x90\x80": 2,
    b"\x92\x80": 3,
    b"\x93\x80": 3,
    b"\x94\x80": 6,
    b"\x95\x80": 6,
    b"\x9c\x80": 5,
    b"\x9d\x80": 3,
    b"\x9e\x80": 6,
    b"\x9f\x80": 2,
}
ZERO_TERMINATED = {b"\x91\x80"}
REMOVED_VISIBLE_CONTROLS = {b"\x90\x80", b"\x91\x80", b"\x93\x80"}
NEWLINE = b"\x80\x80"
SPEAKER_DELIMITER = b"\x87\x80\x80\x80"
SPACE_CODE = 232
TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")
GLOBAL_CODE_MAP = {
    **{str(value): value + 1 for value in range(10)},
    **{chr(ord("A") + value): 14 + value for value in range(26)},
    **{chr(ord("a") + value): 40 + value for value in range(26)},
    "-": 11, ".": 12, "'": 13, ",": 258, " ": 232, "　": 233,
    "、": 235, "。": 237, "・": 239, "?": 241, "！": 243,
    "：": 259, "(": 263, "（": 264, "「": 272, "『": 273,
    "+": 278, "～": 283, "…": 284, "♪": 285,
}
GLOBAL_CHARACTER_MAP = {code: character for character, code in GLOBAL_CODE_MAP.items()}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


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


def logical_segment(data: bytes) -> bytes:
    require(bool(data) and data.endswith(b"\0"), "message segment lacks NUL")
    return data.rstrip(b"\0") + b"\0"


def normalize_text(value: object, field: str) -> str:
    require(isinstance(value, str), f"{field} must be a string")
    result = value.replace("\r\n", "\n").replace("\r", "\n")
    require("\0" not in result, f"{field} contains NUL")
    return result


def expand_tokens(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = TOKEN_PATTERN.sub(lambda match: match.group(1), text)
    require("{" not in text and "}" not in text, "unbalanced translation token")
    return text


@dataclass(frozen=True, order=True)
class Occurrence:
    archive_id: int
    stream_id: int
    message_id: int


@dataclass(frozen=True)
class ExpectedText:
    exact_sha256: str
    japanese: str | None
    korean: str
    speaker_korean: str | None


@dataclass(frozen=True)
class Token:
    kind: str
    raw: bytes
    code: int | None = None


def parse_occurrence(item: dict[str, object]) -> Occurrence:
    try:
        result = Occurrence(
            int(item["archive_id"]), int(item["stream_id"]), int(item["message_id"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError(f"invalid occurrence: {item!r}") from exc
    require(BANK_STREAMS.get(result.archive_id) == result.stream_id, "unverified bank")
    return result


def load_expectations(
    catalogue_path: Path, translation_path: Path
) -> tuple[dict[Occurrence, dict[str, object]], dict[Occurrence, ExpectedText]]:
    catalogue_doc = json.loads(catalogue_path.read_text(encoding="utf-8"))
    catalogue_rows = catalogue_doc.get("dialogues")
    compact = False
    if not isinstance(catalogue_rows, list):
        catalogue_rows = catalogue_doc.get("occurrences")
        compact = True
    require(isinstance(catalogue_rows, list), "catalogue rows missing")
    require(len(catalogue_rows) == EXPECTED_OCCURRENCES, "catalogue count mismatch")
    catalogue: dict[Occurrence, dict[str, object]] = {}
    for row in catalogue_rows:
        require(isinstance(row, dict), "catalogue row is not an object")
        occurrence = parse_occurrence(row)
        require(occurrence not in catalogue, "duplicate catalogue occurrence")
        raw_hex = row.get("raw_bytes_hex")
        if raw_hex is not None:
            raw = bytes.fromhex(str(raw_hex))
            require(sha256(raw) == str(row["exact_sha256"]).lower(), "catalogue hash mismatch")
        else:
            require(compact, "full catalogue raw bytes missing")
        line_count = row.get("source_line_count")
        if line_count is None and isinstance(row.get("japanese"), str):
            line_count = len(normalize_text(row["japanese"], "catalogue Japanese").split("\n"))
            row = {**row, "source_line_count": line_count}
        require(isinstance(line_count, int) and line_count > 0, "source line count invalid")
        catalogue[occurrence] = row

    translation_doc = json.loads(translation_path.read_text(encoding="utf-8"))
    entries = translation_doc.get("translations")
    require(isinstance(entries, list), "translations array missing")
    require(len(entries) == EXPECTED_TRANSLATIONS, "translation entry count mismatch")
    seen_hashes: set[str] = set()
    expected: dict[Occurrence, ExpectedText] = {}
    for entry in entries:
        require(isinstance(entry, dict), "translation entry is not an object")
        digest = str(entry.get("exact_sha256", "")).lower()
        require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "bad translation hash")
        require(digest not in seen_hashes, "duplicate translation hash")
        seen_hashes.add(digest)
        japanese_value = entry.get("japanese")
        japanese = (
            normalize_text(japanese_value, "japanese")
            if japanese_value is not None
            else None
        )
        korean = expand_tokens(normalize_text(entry.get("korean"), "korean"))
        require(bool(korean), "empty Korean translation")
        speaker_value = entry.get("speaker_korean")
        speaker = None if speaker_value is None else expand_tokens(
            normalize_text(speaker_value, "speaker_korean")
        )
        occurrences = entry.get("occurrences")
        require(isinstance(occurrences, list) and bool(occurrences), "empty occurrence list")
        item = ExpectedText(digest, japanese, korean, speaker)
        for raw_occurrence in occurrences:
            require(isinstance(raw_occurrence, dict), "occurrence is not an object")
            occurrence = parse_occurrence(raw_occurrence)
            require(occurrence not in expected, "occurrence translated twice")
            source = catalogue.get(occurrence)
            require(source is not None, "translation occurrence absent from catalogue")
            require(str(source["exact_sha256"]).lower() == digest, "occurrence hash mismatch")
            require(len(korean.split("\n")) == int(source["source_line_count"]),
                    "Korean source line count mismatch")
            if japanese is not None and source.get("japanese") is not None:
                require(normalize_text(source["japanese"], "source Japanese") == japanese,
                        "translation source text mismatch")
            mode = source["speaker"]["mode"]
            if mode == "implicit_or_continuation":
                require(speaker is None, "implicit speaker was translated")
            else:
                require(bool(speaker) and "\n" not in speaker, "speaker_korean missing")
            expected[occurrence] = item
    require(set(expected) == set(catalogue), "translation coverage is not exact")
    require(len(expected) == EXPECTED_OCCURRENCES, "translated occurrence count mismatch")
    return catalogue, expected


def parse_pk1(data: bytes | bytearray) -> list[tuple[bytes, int, int, int]]:
    require(len(data) >= 16 and u32(data, 0) == 0, "PK1 signature mismatch")
    count, header_size, reserved = struct.unpack_from("<III", data, 4)
    require(1 <= count <= 1000, "PK1 count invalid")
    require(header_size == 16 + count * 16 and reserved == 0, "PK1 header invalid")
    rows = [
        struct.unpack_from("<4sIII", data, 16 + index * 16)
        for index in range(count)
    ]
    for _, _, size, offset in rows:
        require(size > 0 and header_size <= offset and offset + size <= len(data),
                "PK1 record outside archive")
    return rows


def package_boundary(data: bytes, rows: Sequence[tuple[bytes, int, int, int]]) -> tuple[int, int]:
    end = max(offset + size for _, _, size, offset in rows)
    candidates: list[int] = []
    for offset in range((end + 3) & ~3, len(data) - 15, 4):
        zero, count, header_size, reserved = struct.unpack_from("<IIII", data, offset)
        if not (
            zero == 0
            and 1 <= count <= 1000
            and header_size == 16 + count * 16
            and reserved == 0
            and offset + header_size <= len(data)
        ):
            continue
        try:
            parse_pk1(data[offset:])
        except AssertionError:
            continue
        candidates.append(offset)
    boundary = min(candidates, default=len(data))
    require(not any(data[end:boundary]), "PK1 gap is not zero")
    return end, boundary


def decode_slz(data: bytes | bytearray, offset: int) -> tuple[bytes, dict[str, int]]:
    header = bytes(data[offset : offset + 16])
    require(len(header) == 16 and header[:3] == b"SLZ", "SLZ signature mismatch")
    mode, compressed, unpacked, next_rel = (
        header[3], u32(header, 4), u32(header, 8), u32(header, 12)
    )
    payload = bytes(data[offset + 16 : offset + 16 + compressed])
    require(len(payload) == compressed, "SLZ payload truncated")
    decoded = decompress_slz_payload(payload, mode, unpacked)
    return decoded, {
        "mode": mode,
        "compressed": compressed,
        "unpacked": unpacked,
        "next_rel": next_rel,
    }


def glyph_at(data: bytes, offset: int) -> tuple[int | None, int]:
    first = data[offset]
    if first == 0:
        return None, 0
    if first < 0x80:
        return first, 1
    if offset + 1 < len(data) and data[offset + 1] < 0x80:
        return (first & 0x7F) | (data[offset + 1] << 7), 2
    return None, 0


def conservative_local_codes(data: bytes, local_base: int, glyph_count: int) -> set[int]:
    """Find every possible local-font reference, including control operands.

    Scanning every byte boundary can over-protect a slot, but it cannot miss a
    slot merely because an unknown control embeds what looks like glyph data.
    """

    upper_bound = local_base + glyph_count
    protected: set[int] = set()
    for offset in range(len(data)):
        code, _ = glyph_at(data, offset)
        if code is not None and local_base <= code < upper_bound:
            protected.add(code)
    return protected


def tokenize(data: bytes, start: int) -> tuple[list[Token], int]:
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
        require(offset + 1 < len(data), "truncated dialogue control")
        opcode = data[offset : offset + 2]
        if opcode in ZERO_TERMINATED:
            end = data.find(b"\0", offset + 2)
            require(end >= 0, "unterminated dialogue control")
            raw = data[offset : end + 1]
            offset = end + 1
        else:
            size = CONTROL_SIZES.get(opcode)
            require(size is not None and offset + size <= len(data),
                    f"unsupported dialogue control {opcode.hex()}")
            raw = data[offset : offset + size]
            offset += size
        tokens.append(Token("control", raw))
    raise AssertionError("dialogue body lacks NUL")


def preserved_control_signature(tokens: Iterable[Token]) -> tuple[str, ...]:
    return tuple(
        token.raw.hex()
        for token in tokens
        if token.kind == "control" and token.raw[:2] not in REMOVED_VISIBLE_CONTROLS
    )


def patched_body_start(raw: bytes, source: dict[str, object]) -> tuple[int, int | None, int | None]:
    mode = source["speaker"]["mode"]
    if mode == "implicit_or_continuation":
        return int(source["evidence"]["body_start_offset"]), None, None
    field_start = int(source["evidence"]["speaker_field_start_offset"])
    delimiter = raw.find(SPEAKER_DELIMITER, field_start)
    require(delimiter >= field_start, "patched speaker/body delimiter missing")
    field_end = delimiter - 2 if raw[delimiter - 2 : delimiter] == b"\x89\x80" else delimiter
    return delimiter + len(SPEAKER_DELIMITER), field_start, field_end


def render_expected(
    text: str, font_path: Path, pixel_size: int
) -> dict[str, tuple[int, bytes]]:
    font = ImageFont.truetype(str(font_path), pixel_size)
    result: dict[str, tuple[int, bytes]] = {}
    for character in text:
        bbox = font.getbbox(character)
        require(bbox is not None, f"font cannot render {character!r}")
        advance = max(1, min(24, round(font.getlength(character))))
        image = Image.new("L", (24, 24), 0)
        draw = ImageDraw.Draw(image)
        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (24 - ink_width) // 2 - bbox[0])
        y = max(0, (24 - ink_height) // 2 - bbox[1])
        draw.text((x, y), character, font=font, fill=255)
        pixels = [
            round(value / 255 * (GRAY_LEVELS - 1)) * 15 // (GRAY_LEVELS - 1)
            for value in image.getdata()
        ]
        packed = bytearray()
        for index in range(0, len(pixels), 2):
            packed.append(pixels[index] | (pixels[index + 1] << 4))
        result[character] = (advance, bytes(packed))
    return result


def expected_bank_characters(
    plan: dict[int, tuple[dict[str, object], ExpectedText]]
) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for _, expected in plan.values():
        texts = ([expected.speaker_korean] if expected.speaker_korean else []) + [expected.korean]
        for text in texts:
            for character in text:
                if character == "\n" or character in GLOBAL_CODE_MAP or character in seen:
                    continue
                seen.add(character)
                result.append(character)
    return "".join(result)


def local_character_map(
    mclib: Mclib, expected_bitmaps: dict[str, tuple[int, bytes]]
) -> tuple[dict[int, str], dict[str, int]]:
    glyph_bytes = mclib.glyph_stride * mclib.glyph_height // 2
    signatures: dict[tuple[int, bytes], str] = {}
    for character, signature in expected_bitmaps.items():
        require(signature not in signatures, f"ambiguous rendered glyph bitmap for {character!r}")
        signatures[signature] = character
    code_to_character: dict[int, str] = {}
    character_hits: dict[str, list[int]] = defaultdict(list)
    for index in range(mclib.glyph_count):
        start = index * glyph_bytes
        signature = (
            mclib.widths[index],
            mclib.bitmaps[start : start + glyph_bytes],
        )
        character = signatures.get(signature)
        if character is not None:
            code = mclib.local_base + index
            code_to_character[code] = character
            character_hits[character].append(code)
    for character in expected_bitmaps:
        require(character_hits[character], f"Nanum glyph {character!r} absent from local atlas")
    return code_to_character, {
        character: min(codes) for character, codes in character_hits.items()
    }


def decode_visible(tokens: Iterable[Token], code_map: dict[int, str]) -> str:
    result: list[str] = []
    for token in tokens:
        if token.kind == "glyph":
            if token.code in GLOBAL_CHARACTER_MAP:
                result.append(GLOBAL_CHARACTER_MAP[token.code])
            else:
                require(token.code in code_map, f"unverified patched glyph code {token.code}")
                result.append(code_map[token.code])
        elif token.raw == NEWLINE:
            result.append("\n")
        else:
            require(token.raw[:2] not in REMOVED_VISIBLE_CONTROLS,
                    "Japanese visible-name/ruby control remains in patched body")
    return "".join(result)


def decode_speaker(raw: bytes, start: int, end: int, code_map: dict[int, str]) -> str:
    result: list[str] = []
    offset = start
    while offset < end:
        code, size = glyph_at(raw, offset)
        require(code is not None and size > 0, "patched speaker is not literal glyphs")
        if code in GLOBAL_CHARACTER_MAP:
            result.append(GLOBAL_CHARACTER_MAP[code])
        else:
            require(code in code_map, f"unverified patched speaker code {code}")
            result.append(code_map[code])
        offset += size
    return "".join(result)


def read_range(path: Path, start: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(size)
    require(len(data) == size, f"short read at 0x{start:X}")
    return data


def count_differences(left: bytes, right: bytes) -> int:
    require(len(left) == len(right), "diff blocks have unequal size")
    return sum(a != b for a, b in zip(left, right))


def verify_equal_range(
    original: Path, patched: Path, start: int, size: int, chunk_size: int = 8 * 1024 * 1024
) -> None:
    with original.open("rb") as left, patched.open("rb") as right:
        left.seek(start)
        right.seek(start)
        remaining = size
        while remaining:
            take = min(chunk_size, remaining)
            require(left.read(take) == right.read(take),
                    f"unexpected ISO difference outside allowed archives at 0x{start + size - remaining:X}")
            remaining -= take


def verify_diff_scope(
    original: Path,
    patched: Path,
    allowed_ranges: Sequence[tuple[int, int]],
    *,
    total_size: int = ORIGINAL_ISO_SIZE,
) -> int:
    cursor = 0
    difference_count = 0
    for start, size in sorted(allowed_ranges):
        require(start >= cursor, "overlapping allowed archive ranges")
        verify_equal_range(original, patched, cursor, start - cursor)
        left = read_range(original, start, size)
        right = read_range(patched, start, size)
        bank_differences = count_differences(left, right)
        require(bank_differences > 0, f"allowed archive at 0x{start:X} did not change")
        difference_count += bank_differences
        cursor = start + size
    verify_equal_range(original, patched, cursor, total_size - cursor)
    return difference_count


def verify_bank(
    archive_id: int,
    original: bytes,
    patched: bytes,
    source_rows: dict[int, tuple[dict[str, object], ExpectedText]],
    font_path: Path,
) -> dict[str, object]:
    original_rows = parse_pk1(original)
    patched_rows = parse_pk1(patched)
    require(len(original_rows) == len(patched_rows), "PK1 row count changed")
    require(len(original_rows) > 1, "scene PK1 row missing")
    require(original_rows[1][:2] == (b"DCMS", 0), "original row 1 is not scene DCMS")
    require(patched_rows[1][:2] == (b"DCMS", 0), "patched row 1 is not scene DCMS")
    original_end, original_boundary = package_boundary(original, original_rows)
    patched_end, patched_boundary = package_boundary(patched, patched_rows)
    require(original_boundary == patched_boundary, "later PK1 package boundary moved")
    require(original[original_boundary:] == patched[patched_boundary:],
            "later PK1 package/tail changed")

    for index, (old_row, new_row) in enumerate(zip(original_rows, patched_rows)):
        require(old_row[:2] == new_row[:2], f"PK1 identity changed at row {index}")
        if index == 1:
            continue
        old_size, old_offset = old_row[2], old_row[3]
        new_size, new_offset = new_row[2], new_row[3]
        require(old_size == new_size, f"non-target PK1 size changed at row {index}")
        require(original[old_offset : old_offset + old_size] == patched[new_offset : new_offset + new_size],
                f"non-target PK1 record {index} changed")

    original_decoded, original_slz = decode_slz(original, original_rows[1][3])
    patched_decoded, patched_slz = decode_slz(patched, patched_rows[1][3])
    require(original_slz["mode"] == patched_slz["mode"] == 2, "target SLZ mode changed")
    require(original_slz["next_rel"] == patched_slz["next_rel"] == 0, "target SLZ link changed")
    target_allocation = patched_rows[1][2]
    used = 16 + patched_slz["compressed"]
    require(used <= target_allocation, "patched SLZ exceeds PK1 allocation")
    target_offset = patched_rows[1][3]
    require(not any(patched[target_offset + used : target_offset + target_allocation]),
            "patched target allocation padding is not zero")
    require(not any(patched[patched_end:patched_boundary]), "patched PK1 gap is not zero")

    original_mclib = Mclib.parse(original_decoded)
    patched_mclib = Mclib.parse(patched_decoded)
    original_geometry = (
        original_mclib.glyph_width,
        original_mclib.glyph_height,
        original_mclib.glyph_stride,
    )
    patched_geometry = (
        patched_mclib.glyph_width,
        patched_mclib.glyph_height,
        patched_mclib.glyph_stride,
    )
    require(original_geometry == patched_geometry == (24, 24, 24),
            "local font geometry changed")
    require(original_mclib.local_base == patched_mclib.local_base,
            "local font base changed")
    require(patched_mclib.glyph_count >= original_mclib.glyph_count,
            "local glyph table shrank")
    old_ids = [message_id for message_id, _ in original_mclib.rows]
    new_ids = [message_id for message_id, _ in patched_mclib.rows]
    require(old_ids == new_ids, "mclib message id table changed")
    old_messages = {
        message_id: original_mclib.segments[offset]
        for message_id, offset in original_mclib.rows
    }
    new_messages = {
        message_id: patched_mclib.segments[offset]
        for message_id, offset in patched_mclib.rows
    }
    target_ids = set(source_rows)
    protected_local_codes: set[int] = set()
    for message_id in old_ids:
        if message_id not in target_ids:
            require(logical_segment(old_messages[message_id]) == logical_segment(new_messages[message_id]),
                    f"non-target message {message_id} bytecode changed")
            protected_local_codes.update(conservative_local_codes(
                old_messages[message_id],
                original_mclib.local_base,
                original_mclib.glyph_count,
            ))

    glyph_bytes = (original_mclib.glyph_stride * original_mclib.glyph_height + 1) // 2
    for code in sorted(protected_local_codes):
        glyph_index = code - original_mclib.local_base
        require(original_mclib.widths[glyph_index] == patched_mclib.widths[glyph_index],
                f"non-target local glyph width changed for code {code}")
        bitmap_start = glyph_index * glyph_bytes
        bitmap_end = bitmap_start + glyph_bytes
        require(
            original_mclib.bitmaps[bitmap_start:bitmap_end]
            == patched_mclib.bitmaps[bitmap_start:bitmap_end],
            f"non-target local glyph bitmap changed for code {code}",
        )

    characters = expected_bank_characters(source_rows)
    font_pixel_size = 22
    expected_bitmaps = render_expected(characters, font_path, font_pixel_size)
    code_map, representative_codes = local_character_map(patched_mclib, expected_bitmaps)
    verified = 0
    for message_id, (source, expected) in source_rows.items():
        old_raw = old_messages[message_id]
        new_raw = new_messages[message_id]
        require(sha256(old_raw) == expected.exact_sha256, "original target hash mismatch")
        if source.get("raw_bytes_hex") is not None:
            require(old_raw.hex() == source["raw_bytes_hex"], "catalogue target bytes mismatch")
        require(sha256(new_raw) != expected.exact_sha256, "target message did not change")

        old_body_start = int(source["evidence"]["body_start_offset"])
        old_tokens, _ = tokenize(old_raw, old_body_start)
        new_body_start, speaker_start, speaker_end = patched_body_start(new_raw, source)
        new_tokens, new_terminator = tokenize(new_raw, new_body_start)
        require(not any(new_raw[new_terminator + 1 :]), "non-zero patched message padding")
        require(preserved_control_signature(old_tokens) == preserved_control_signature(new_tokens),
                f"event/layout control signature changed for message {message_id}")
        decoded_body = decode_visible(new_tokens, code_map)
        source_line_count = int(source["source_line_count"])
        while (
            len(decoded_body.split("\n")) > source_line_count
            and decoded_body.endswith("\n")
        ):
            decoded_body = decoded_body[:-1]
        require(decoded_body == expected.korean,
                f"Korean body mismatch for archive {archive_id} message {message_id}")

        mode = source["speaker"]["mode"]
        if mode != "implicit_or_continuation":
            require(speaker_start is not None and speaker_end is not None,
                    "patched explicit speaker boundary missing")
            old_field_start = int(source["evidence"]["speaker_field_start_offset"])
            old_field_end = int(source["evidence"]["speaker_field_end_offset"])
            require(new_raw[:speaker_start] == old_raw[:old_field_start], "speaker prefix changed")
            require(new_raw[speaker_end:new_body_start] == old_raw[old_field_end:old_body_start],
                    "speaker/body delimiter controls changed")
            decoded_speaker = decode_speaker(new_raw, speaker_start, speaker_end, code_map)
            require(decoded_speaker == expected.speaker_korean,
                    f"Korean speaker mismatch for archive {archive_id} message {message_id}")
        verified += 1

    return {
        "archive_id": archive_id,
        "stream_id": BANK_STREAMS[archive_id],
        "verified_occurrences": verified,
        "old_archive_sha256": sha256(original),
        "new_archive_sha256": sha256(patched),
        "old_slz_compressed": original_slz["compressed"],
        "new_slz_compressed": patched_slz["compressed"],
        "old_record_size": original_rows[1][2],
        "new_record_size": patched_rows[1][2],
        "old_gap": original_boundary - original_end,
        "new_gap": patched_boundary - patched_end,
        "old_glyph_count": original_mclib.glyph_count,
        "new_glyph_count": patched_mclib.glyph_count,
        "verified_nanumsquare_characters": len(characters),
        "font_pixel_size": font_pixel_size,
        "font_gray_levels": GRAY_LEVELS,
        "representative_character_codes": representative_codes,
        "non_target_messages_verified": len(old_ids) - len(target_ids),
        "protected_non_target_local_glyphs": len(protected_local_codes),
        "event_control_signatures_verified": verified,
    }


def verify(
    original_iso: Path,
    patched_iso: Path,
    catalogue_path: Path,
    translation_path: Path,
    font_path: Path,
    expected_output_sha256: str | None,
) -> dict[str, object]:
    require(original_iso.resolve() != patched_iso.resolve(), "original and patched ISO alias")
    require(original_iso.stat().st_size == ORIGINAL_ISO_SIZE, "original ISO size mismatch")
    require(patched_iso.stat().st_size == ORIGINAL_ISO_SIZE, "patched ISO size mismatch")
    original_hash = sha256_file(original_iso)
    require(original_hash == ORIGINAL_ISO_SHA256, "original ISO SHA-256 mismatch")
    patched_hash = sha256_file(patched_iso)
    pinned = expected_output_sha256 or EXPECTED_PATCHED_ISO_SHA256
    if pinned is not None:
        require(bool(re.fullmatch(r"[0-9A-Fa-f]{64}", pinned)), "bad expected output hash")
        require(patched_hash == pinned.upper(), "patched ISO SHA-256 mismatch")
    require(sha256_file(font_path) == NANUM_FONT_SHA256, "Nanum font SHA-256 mismatch")

    require(read_range(original_iso, INDEX_OFFSET, INDEX_BYTES)
            == read_range(patched_iso, INDEX_OFFSET, INDEX_BYTES), "encoded hidden index changed")
    original_index = read_index(original_iso)
    patched_index = read_index(patched_iso)
    require(original_index == patched_index, "decoded 6,144-entry index changed")

    catalogue, expected = load_expectations(catalogue_path, translation_path)
    grouped: dict[int, dict[int, tuple[dict[str, object], ExpectedText]]] = defaultdict(dict)
    for occurrence, text in expected.items():
        grouped[occurrence.archive_id][occurrence.message_id] = (catalogue[occurrence], text)
    require(set(grouped) == set(BANK_STREAMS), "expected banks are incomplete")

    ranges = [
        (original_index[archive_id] * SECTOR, original_index[0x1800 + archive_id] * SECTOR)
        for archive_id in sorted(BANK_STREAMS)
    ]
    difference_count = verify_diff_scope(original_iso, patched_iso, ranges)
    reports: list[dict[str, object]] = []
    verified = 0
    for archive_id, (start, size) in zip(sorted(BANK_STREAMS), ranges):
        original_archive = read_range(original_iso, start, size)
        patched_archive = read_range(patched_iso, start, size)
        report = verify_bank(
            archive_id,
            original_archive,
            patched_archive,
            grouped[archive_id],
            font_path,
        )
        reports.append(report)
        verified += int(report["verified_occurrences"])
    require(verified == EXPECTED_OCCURRENCES, "verified occurrence total mismatch")
    return {
        "schema_version": 1,
        "status": "static_verification_complete_runtime_unverified",
        "original_iso": str(original_iso),
        "patched_iso": str(patched_iso),
        "original_iso_sha256": original_hash,
        "patched_iso_sha256": patched_hash,
        "patched_hash_pinned": pinned is not None,
        "font_sha256": NANUM_FONT_SHA256,
        "encoded_index_unchanged": True,
        "decoded_index_entries_verified": 6144,
        "allowed_archive_count": len(BANK_STREAMS),
        "differences_outside_allowed_archives": 0,
        "differing_bytes_inside_allowed_archives": difference_count,
        "translation_entries": EXPECTED_TRANSLATIONS,
        "verified_occurrences": verified,
        "archives": reports,
        "emulator_tested": False,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_iso", type=Path)
    parser.add_argument("patched_iso", type=Path)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--expected-output-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            args.original_iso,
            args.patched_iso,
            args.catalogue,
            args.translations,
            args.font,
            args.expected_output_sha256,
        )
    except (OSError, ValueError, AssertionError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        report_key = os.path.normcase(str(args.report.resolve(strict=False)))
        protected = {
            os.path.normcase(str(path.resolve(strict=False)))
            for path in (args.original_iso, args.patched_iso, args.catalogue,
                         args.translations, args.font)
        }
        if report_key in protected:
            parser.error("report path aliases a protected input")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

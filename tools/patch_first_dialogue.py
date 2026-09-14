#!/usr/bin/env python3
"""Patch SO3 DC Disc 1's first conversation and extend its global font.

The patch is deliberately transactional and size preserving at the ISO level:

* append fourteen Korean glyphs to the global 24 px mclib;
* move the following DTT member inside archive 8 and repair its ZLS links;
* move the scene mclib's local-code base from 301 to 307 without changing any
  existing local bitmap;
* replace only message 5 while retaining its presentation control sequence;
* relocate later records only inside archive 1220's first PK1 package if the
  recompressed scene member grows.

Every source signature, boundary, decompression round trip, and unchanged
member is checked before a new ISO is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "work" / "repack_engine"))

from so3_repack import (  # noqa: E402
    Mclib,
    compress_slz_mode2,
    decompress_slz_payload,
    encode_glyph_code,
    render_glyphs,
)


GLOBAL_ARCHIVE_OFFSET = 0x394800
GLOBAL_ARCHIVE_SIZE = 0xF000
GLOBAL_WRAPPER_REL = 0x8580
GLOBAL_MEMBER_REL = GLOBAL_WRAPPER_REL + 0x10
GLOBAL_OLD_DTT_WRAPPER_REL = 0xE580
GLOBAL_OLD_END_REL = 0xEC00
GLOBAL_NEW_DTT_WRAPPER_REL = 0xE900
GLOBAL_NEW_END_REL = 0xEF80

TARGET_ARCHIVE_OFFSET = 0x4B369800
TARGET_ARCHIVE_SIZE = 0x25C800
TARGET_RECORD_INDEX = 1
TARGET_MEMBER_REL = 0x1188
TARGET_MESSAGE_ID = 5
TARGET_SECOND_PACKAGE_REL = 0xFD000

GLOBAL_MCLIB_SHA256 = "8F91FE6C630BF7890E2934D3B302911188C52DDE5732AA70E3ED40EEA325A3BC"
TARGET_MCLIB_SHA256 = "591C3C4FB746F618BB7DCBB4C3920B6CBCBEB913E436F5CC5FD39EA7441E91D5"
GLOBAL_ARCHIVE_SHA256 = "DF061A34F42E77776B72ABF922232CA6A5B69F84CEC55108F943763015F70374"
TARGET_ARCHIVE_SHA256 = "5466CDB5CEFBC1A1F190354CED2487CBEF18F5EECC30252607EED5FB0DCF5D5A"
SUPPORTED_ISO_SIZE = 4_689_854_464
SUPPORTED_ISO_SHA256 = "95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826"
FONT_GRAY_LEVELS = 4

SPEAKER = "소피아"
LINE_1 = "페이트, 봐."
LINE_2 = "이 호텔은…"
LINE_3 = "104호가 없어."
LINE_4 = "왜?"
DISPLAY_LINES = (SPEAKER, "「" + LINE_1, LINE_2, LINE_3, LINE_4)

# Existing global glyph codes.  The global atlas has local base 1, so a code
# maps to bitmap index code-1.
CODE_SPACE = 232       # verified blank glyph, advance 8
CODE_COMMA = 258       # ,
CODE_PERIOD = 12       # .
CODE_QUESTION = 241    # ?
CODE_OPEN_QUOTE = 272  # 「
CODE_ELLIPSIS = 284    # …
DIGIT_CODES = {"0": 1, "1": 2, "4": 5}
# The target mclib's original 1/0/4 glyphs after its local base moves +6.
TARGET_DIGIT_CODES = {"1": 312, "0": 313, "4": 318}


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def p32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & -boundary


def read_exact(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(size)
    if len(data) != size:
        raise ValueError(f"short read at 0x{offset:X}: {len(data)} != {size}")
    return data


def decode_inner_slz(archive: bytes | bytearray, member_rel: int) -> tuple[bytes, dict[str, int]]:
    header = bytes(archive[member_rel : member_rel + 16])
    if len(header) != 16 or header[:3] != b"SLZ":
        raise ValueError(f"SLZ header missing at archive+0x{member_rel:X}")
    mode = header[3]
    compressed, unpacked, next_rel = struct.unpack_from("<III", header, 4)
    payload = bytes(archive[member_rel + 16 : member_rel + 16 + compressed])
    if len(payload) != compressed:
        raise ValueError("truncated SLZ payload")
    decoded = decompress_slz_payload(payload, mode, unpacked)
    return decoded, {
        "mode": mode,
        "compressed": compressed,
        "unpacked": unpacked,
        "next_rel": next_rel,
    }


def hangul_characters() -> str:
    joined = SPEAKER + LINE_1 + LINE_2 + LINE_3 + LINE_4
    return "".join(dict.fromkeys(c for c in joined if "가" <= c <= "힣"))


def extend_global_font(
    original_archive: bytes,
    font_path: Path,
    gray_levels: int = FONT_GRAY_LEVELS,
) -> tuple[bytes, bytes, dict[str, int], dict[str, int]]:
    if sha256(original_archive) != GLOBAL_ARCHIVE_SHA256:
        raise ValueError("global archive SHA-256 mismatch")
    archive = bytearray(original_archive)
    if archive[GLOBAL_WRAPPER_REL : GLOBAL_WRAPPER_REL + 4] != b"ZLS\0":
        raise ValueError("global ZLS wrapper signature mismatch")
    if archive[GLOBAL_OLD_DTT_WRAPPER_REL : GLOBAL_OLD_DTT_WRAPPER_REL + 4] != b"ZLS\0":
        raise ValueError("DTT ZLS wrapper signature mismatch")
    if archive[GLOBAL_OLD_END_REL : GLOBAL_OLD_END_REL + 4] != b"DNE\0":
        raise ValueError("global archive end marker mismatch")

    decoded, old_slz = decode_inner_slz(archive, GLOBAL_MEMBER_REL)
    if old_slz != {"mode": 2, "compressed": 24496, "unpacked": 84608, "next_rel": 0}:
        raise ValueError(f"unexpected global SLZ header: {old_slz}")
    if sha256(decoded) != GLOBAL_MCLIB_SHA256:
        raise ValueError("global mclib SHA-256 mismatch")
    old_member_end = GLOBAL_MEMBER_REL + 16 + old_slz["compressed"]
    if any(archive[old_member_end:GLOBAL_OLD_DTT_WRAPPER_REL]):
        raise ValueError("global/DTT inter-member padding is not zero")
    if any(archive[GLOBAL_OLD_END_REL + 16 :]):
        raise ValueError("global archive tail padding is not zero")

    words = list(struct.unpack_from("<13I", decoded, 0x10))
    width_start, bitmap_start = words[2], words[3]
    old_count, height, stride = words[4], words[8], words[9]
    if (width_start, bitmap_start, old_count, height, stride, words[10], words[12]) != (
        0x80, 0x200, 292, 24, 24, 1, len(decoded)
    ):
        raise ValueError("unexpected global mclib geometry")

    characters = hangul_characters()
    if len(characters) != 14:
        raise AssertionError(f"translation no longer has 14 unique Hangul glyphs: {characters}")
    # Coverage quantization keeps the 22 px outlines legible while allowing
    # the extended font to fit the archive's fixed allocation.
    glyphs = render_glyphs(
        characters, font_path, 24, 24, 24, gray_levels=gray_levels
    )
    new_count = old_count + len(glyphs)
    if width_start + new_count > bitmap_start:
        raise ValueError("global width table has no room for appended glyphs")
    glyph_bytes = stride * height // 2
    bitmap_end = bitmap_start + new_count * glyph_bytes
    new_size = align(bitmap_end, 0x80)
    rebuilt = bytearray(new_size)
    rebuilt[: len(decoded)] = decoded
    p32(rebuilt, 0x20, new_count)
    p32(rebuilt, 0x40, new_size)
    code_map: dict[str, int] = {}
    for index, (character, (width, bitmap)) in enumerate(zip(characters, glyphs)):
        code = old_count + 1 + index
        code_map[character] = code
        rebuilt[width_start + code - 1] = width
        start = bitmap_start + (code - 1) * glyph_bytes
        rebuilt[start : start + glyph_bytes] = bitmap

    compressed = compress_slz_mode2(bytes(rebuilt))
    if decompress_slz_payload(compressed, 2, len(rebuilt)) != rebuilt:
        raise AssertionError("global font SLZ round trip failed")

    wrapper_delta = GLOBAL_NEW_DTT_WRAPPER_REL - GLOBAL_WRAPPER_REL
    max_payload = wrapper_delta - 0x20
    if len(compressed) > max_payload:
        raise ValueError(
            f"global font payload {len(compressed)} exceeds relocated allocation {max_payload}"
        )

    old_dtt_block = bytes(archive[GLOBAL_OLD_DTT_WRAPPER_REL : GLOBAL_OLD_END_REL])
    old_end_marker = bytes(archive[GLOBAL_OLD_END_REL : GLOBAL_OLD_END_REL + 16])
    if len(old_dtt_block) != 0x680 or len(old_end_marker) != 16:
        raise AssertionError("unexpected DTT/end block lengths")

    # Clear the old tail, then rebuild the two final linked members.
    archive[GLOBAL_WRAPPER_REL:] = b"\0" * (len(archive) - GLOBAL_WRAPPER_REL)
    wrapper = bytearray(original_archive[GLOBAL_WRAPPER_REL : GLOBAL_WRAPPER_REL + 16])
    p32(wrapper, 4, align(16 + len(compressed), 4))
    p32(wrapper, 12, wrapper_delta)
    archive[GLOBAL_WRAPPER_REL : GLOBAL_WRAPPER_REL + 16] = wrapper
    inner_header = b"SLZ\x02" + struct.pack("<III", len(compressed), len(rebuilt), 0)
    inner_start = GLOBAL_MEMBER_REL
    archive[inner_start : inner_start + 16] = inner_header
    archive[inner_start + 16 : inner_start + 16 + len(compressed)] = compressed

    dtt_block = bytearray(old_dtt_block)
    p32(dtt_block, 8, wrapper_delta)
    archive[GLOBAL_NEW_DTT_WRAPPER_REL : GLOBAL_NEW_END_REL] = dtt_block
    archive[GLOBAL_NEW_END_REL : GLOBAL_NEW_END_REL + 16] = old_end_marker

    verified, new_slz = decode_inner_slz(archive, GLOBAL_MEMBER_REL)
    if verified != rebuilt:
        raise AssertionError("rebuilt global member verification failed")
    old_dtt_decoded, old_dtt_slz = decode_inner_slz(
        original_archive, GLOBAL_OLD_DTT_WRAPPER_REL + 0x10
    )
    new_dtt_decoded, new_dtt_slz = decode_inner_slz(
        archive, GLOBAL_NEW_DTT_WRAPPER_REL + 0x10
    )
    if old_dtt_decoded != new_dtt_decoded or old_dtt_slz != new_dtt_slz:
        raise AssertionError("relocated DTT member changed")

    return bytes(archive), bytes(rebuilt), code_map, {
        "old_glyph_count": old_count,
        "new_glyph_count": new_count,
        "old_decoded": len(decoded),
        "new_decoded": len(rebuilt),
        "old_compressed": old_slz["compressed"],
        "new_compressed": new_slz["compressed"],
        "payload_allocation": max_payload,
        "old_dtt_wrapper_rel": GLOBAL_OLD_DTT_WRAPPER_REL,
        "new_dtt_wrapper_rel": GLOBAL_NEW_DTT_WRAPPER_REL,
        "old_end_rel": GLOBAL_OLD_END_REL,
        "new_end_rel": GLOBAL_NEW_END_REL,
    }


def encode_codes(codes: list[int]) -> bytes:
    return b"".join(encode_glyph_code(code) for code in codes)


def encode_korean(text: str, code_map: dict[str, int]) -> bytes:
    codes: list[int] = []
    for character in text:
        if character in code_map:
            codes.append(code_map[character])
        elif character == " ":
            codes.append(CODE_SPACE)
        elif character in DIGIT_CODES:
            codes.append(DIGIT_CODES[character])
        else:
            raise ValueError(f"no glyph mapping for {character!r}")
    return encode_codes(codes)


def build_target_message(code_map: dict[str, int]) -> bytes:
    # Exact original control ordering, with only the two 93 80 name calls
    # literalized and the visible glyph runs translated.
    parts = [
        bytes.fromhex("8a800000803f888006868000000000"),
        encode_korean(SPEAKER, code_map),
        bytes.fromhex("878080808a800000803f888007"),
        encode_codes([CODE_OPEN_QUOTE]),
        bytes.fromhex("888005"),
        encode_korean("페이트", code_map),
        bytes.fromhex("888007"),
        encode_codes([CODE_COMMA, CODE_SPACE]),
        encode_korean("봐", code_map),
        encode_codes([CODE_PERIOD]),
        bytes.fromhex("84808180"),
        encode_korean("이 호텔은", code_map),
        encode_codes([CODE_ELLIPSIS]),
        bytes.fromhex("80808580cdcc4c3e"),
        encode_codes([TARGET_DIGIT_CODES[c] for c in "104"]),
        encode_korean("호가 없어", code_map),
        encode_codes([CODE_PERIOD]),
        bytes.fromhex("80808580cdcccc3e"),
        encode_korean("왜", code_map),
        encode_codes([CODE_QUESTION]),
        bytes.fromhex("8480818000"),
    ]
    return b"".join(parts)


def patch_target_mclib(decoded: bytes, code_map: dict[str, int]) -> tuple[bytes, dict[str, int | str]]:
    if sha256(decoded) != TARGET_MCLIB_SHA256:
        raise ValueError("target scene mclib SHA-256 mismatch")
    mclib = Mclib.parse(decoded)
    if (mclib.local_base, mclib.glyph_count, mclib.mapping_count) != (301, 72, 14):
        raise ValueError("unexpected target mclib code geometry")
    new_local_base = max(code_map.values()) + 1
    if new_local_base != 307:
        raise AssertionError(f"unexpected new local base {new_local_base}")
    shift = new_local_base - mclib.local_base

    rebuilt = bytearray(decoded)
    seen_codes: set[int] = set()
    remapped = 0
    pos = mclib.text_start
    while pos + 1 < mclib.width_start:
        first, second = rebuilt[pos], rebuilt[pos + 1]
        if first >= 0x80 and second < 0x80:
            code = (first & 0x7F) | (second << 7)
            if mclib.local_base <= code < mclib.local_base + mclib.glyph_count:
                encoded = encode_glyph_code(code + shift)
                if len(encoded) != 2:
                    raise AssertionError("local code remap changed operand width")
                rebuilt[pos : pos + 2] = encoded
                seen_codes.add(code)
                remapped += 1
                pos += 2
                continue
        pos += 1
    if remapped != 108 or seen_codes != set(range(301, 373)):
        raise ValueError(
            f"local operand liveness mismatch: occurrences={remapped}, unique={len(seen_codes)}"
        )
    p32(rebuilt, 0x38, new_local_base)

    matches = [off for message_id, off in mclib.rows if message_id == TARGET_MESSAGE_ID]
    if len(matches) != 1:
        raise ValueError("target message ID was not unique")
    target_offset = matches[0]
    old_segment = mclib.segments[target_offset]
    replacement = build_target_message(code_map)
    if len(replacement) > len(old_segment):
        raise ValueError(
            f"translated message {len(replacement)} exceeds original segment {len(old_segment)}"
        )
    start = mclib.text_start + target_offset
    rebuilt[start : start + len(old_segment)] = replacement + b"\0" * (
        len(old_segment) - len(replacement)
    )

    checked = Mclib.parse(bytes(rebuilt))
    if checked.local_base != new_local_base or checked.glyph_count != mclib.glyph_count:
        raise AssertionError("target mclib reparse failed")
    if checked.widths != mclib.widths or checked.bitmaps != mclib.bitmaps:
        raise AssertionError("target local font data changed")
    return bytes(rebuilt), {
        "old_local_base": mclib.local_base,
        "new_local_base": new_local_base,
        "local_code_shift": shift,
        "remapped_local_operands": remapped,
        "old_message_bytes": len(old_segment),
        "new_message_bytes": len(replacement),
        "old_message_hex": old_segment.hex(),
        "new_message_hex": replacement.hex(),
    }


def parse_pk1_table(archive: bytes | bytearray) -> list[tuple[bytes, int, int, int]]:
    if u32(archive, 0) != 0:
        raise ValueError("PK1 leading word mismatch")
    count, header_size, reserved = struct.unpack_from("<III", archive, 4)
    if header_size != 0x10 + count * 16 or reserved != 0:
        raise ValueError("PK1 header geometry mismatch")
    return [
        (
            bytes(archive[0x10 + index * 16 : 0x14 + index * 16]),
            u32(archive, 0x14 + index * 16),
            u32(archive, 0x18 + index * 16),
            u32(archive, 0x1C + index * 16),
        )
        for index in range(count)
    ]


def patch_target_archive(
    original_archive: bytes, code_map: dict[str, int]
) -> tuple[bytes, bytes, dict[str, int | str]]:
    if sha256(original_archive) != TARGET_ARCHIVE_SHA256:
        raise ValueError("target archive SHA-256 mismatch")
    rows = parse_pk1_table(original_archive)
    tag, record_id, old_record_size, record_offset = rows[TARGET_RECORD_INDEX]
    if (tag, record_id, old_record_size, record_offset) != (b"DCMS", 0, 0x28BC, TARGET_MEMBER_REL):
        raise ValueError("target PK1 record mismatch")
    decoded, old_slz = decode_inner_slz(original_archive, record_offset)
    if old_slz != {"mode": 2, "compressed": 10411, "unpacked": 22528, "next_rel": 0}:
        raise ValueError(f"unexpected target SLZ header: {old_slz}")
    rebuilt, details = patch_target_mclib(decoded, code_map)
    compressed = compress_slz_mode2(rebuilt)
    if decompress_slz_payload(compressed, 2, len(rebuilt)) != rebuilt:
        raise AssertionError("target scene SLZ round trip failed")
    new_record = b"SLZ\x02" + struct.pack("<III", len(compressed), len(rebuilt), 0) + compressed
    new_record_size = align(len(new_record), 4)
    new_record += b"\0" * (new_record_size - len(new_record))
    delta = new_record_size - old_record_size

    first_package_end = max(offset + size for _, _, size, offset in rows)
    if first_package_end != 0xFCE84:
        raise ValueError(f"unexpected first PK1 end 0x{first_package_end:X}")
    gap = TARGET_SECOND_PACKAGE_REL - first_package_end
    if delta > gap:
        raise ValueError(f"target record growth {delta} exceeds first-package gap {gap}")
    if any(original_archive[first_package_end:TARGET_SECOND_PACKAGE_REL]):
        raise ValueError("first/second PK1 gap is not zero padding")

    old_suffix_start = record_offset + old_record_size
    suffix = original_archive[old_suffix_start:first_package_end]
    archive = bytearray(original_archive)
    archive[record_offset : record_offset + new_record_size] = new_record
    new_suffix_start = record_offset + new_record_size
    archive[new_suffix_start : new_suffix_start + len(suffix)] = suffix
    new_first_end = first_package_end + delta
    archive[new_first_end:TARGET_SECOND_PACKAGE_REL] = b"\0" * (
        TARGET_SECOND_PACKAGE_REL - new_first_end
    )
    p32(archive, 0x18 + TARGET_RECORD_INDEX * 16, new_record_size)
    for index in range(TARGET_RECORD_INDEX + 1, len(rows)):
        offset_field = 0x1C + index * 16
        p32(archive, offset_field, u32(archive, offset_field) + delta)

    new_rows = parse_pk1_table(archive)
    for index, (old_row, new_row) in enumerate(zip(rows, new_rows)):
        old_tag, old_id, old_size, old_offset = old_row
        new_tag, new_id, new_size, new_offset = new_row
        if (old_tag, old_id) != (new_tag, new_id):
            raise AssertionError(f"PK1 identity changed for row {index}")
        if index == TARGET_RECORD_INDEX:
            if (new_size, new_offset) != (new_record_size, old_offset):
                raise AssertionError("target PK1 record metadata mismatch")
        elif index > TARGET_RECORD_INDEX:
            if (new_size, new_offset) != (old_size, old_offset + delta):
                raise AssertionError(f"PK1 relocation mismatch for row {index}")
            if bytes(archive[new_offset : new_offset + new_size]) != original_archive[
                old_offset : old_offset + old_size
            ]:
                raise AssertionError(f"PK1 record {index} changed during relocation")

    verified, new_slz = decode_inner_slz(archive, record_offset)
    if verified != rebuilt:
        raise AssertionError("target scene verification failed")
    details.update(
        {
            "old_decoded": len(decoded),
            "new_decoded": len(rebuilt),
            "old_compressed": old_slz["compressed"],
            "new_compressed": new_slz["compressed"],
            "old_record_size": old_record_size,
            "new_record_size": new_record_size,
            "record_growth": delta,
            "first_package_gap": gap,
            "remaining_first_package_gap": gap - delta,
        }
    )
    return bytes(archive), rebuilt, details


def global_width_and_bitmap(global_mclib: bytes, code: int) -> tuple[int, bytes]:
    words = struct.unpack_from("<13I", global_mclib, 0x10)
    width_start, bitmap_start, count = words[2], words[3], words[4]
    height, stride = words[8], words[9]
    if not 1 <= code <= count:
        raise ValueError(f"global glyph code {code} outside 1..{count}")
    index = code - 1
    glyph_bytes = stride * height // 2
    start = bitmap_start + index * glyph_bytes
    return global_mclib[width_start + index], global_mclib[start : start + glyph_bytes]


def preview_width_and_bitmap(
    global_mclib: bytes, target_mclib: bytes, code: int
) -> tuple[int, bytes]:
    local = Mclib.parse(target_mclib)
    if code < local.local_base:
        return global_width_and_bitmap(global_mclib, code)
    index = code - local.local_base
    if not 0 <= index < local.glyph_count:
        raise ValueError(f"local preview glyph code {code} is outside the target atlas")
    glyph_bytes = local.glyph_stride * local.glyph_height // 2
    start = index * glyph_bytes
    return local.widths[index], local.bitmaps[start : start + glyph_bytes]


def preview_line_codes(code_map: dict[str, int]) -> list[list[int]]:
    return [
        [code_map[c] for c in SPEAKER],
        [CODE_OPEN_QUOTE]
        + [code_map[c] for c in "페이트"]
        + [CODE_COMMA, CODE_SPACE, code_map["봐"], CODE_PERIOD],
        [code_map["이"], CODE_SPACE, code_map["호"], code_map["텔"], code_map["은"], CODE_ELLIPSIS],
        [TARGET_DIGIT_CODES["1"], TARGET_DIGIT_CODES["0"], TARGET_DIGIT_CODES["4"], code_map["호"],
         code_map["가"], CODE_SPACE, code_map["없"], code_map["어"], CODE_PERIOD],
        [code_map["왜"], CODE_QUESTION],
    ]


def write_preview(
    global_mclib: bytes, target_mclib: bytes, code_map: dict[str, int], output: Path
) -> None:
    line_codes = preview_line_codes(code_map)
    widths = []
    for line in line_codes:
        advances = [
            preview_width_and_bitmap(global_mclib, target_mclib, code)[0]
            for code in line
        ]
        # The last glyph can draw beyond its advance. Keep its complete 24 px
        # bitmap instead of cropping the preview at the logical text width.
        widths.append(sum(advances[:-1]) + 24)
    canvas = Image.new("L", (max(widths), len(line_codes) * 28), 0)
    for row, codes in enumerate(line_codes):
        cursor = 0
        for code in codes:
            width, bitmap = preview_width_and_bitmap(global_mclib, target_mclib, code)
            pixels: list[int] = []
            for value in bitmap:
                pixels.extend(((value & 0x0F) * 17, (value >> 4) * 17))
            glyph = Image.new("L", (24, 24))
            glyph.putdata(pixels[: 24 * 24])
            canvas.paste(255, (cursor, row * 28), glyph)
            cursor += width
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.resize((canvas.width * 4, canvas.height * 4), Image.Resampling.NEAREST).save(output)


def verify_written_archive(path: Path, offset: int, expected: bytes) -> None:
    actual = read_exact(path, offset, len(expected))
    if actual != expected:
        raise AssertionError(f"written archive mismatch at ISO offset 0x{offset:X}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_iso", type=Path)
    parser.add_argument("output_iso", type=Path, nargs="?")
    parser.add_argument("--font", type=Path, default=Path(r"C:\Windows\Fonts\malgun.ttf"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.input_iso.is_file():
        parser.error(f"input ISO does not exist: {args.input_iso}")
    if not args.font.is_file():
        parser.error(f"font does not exist: {args.font}")
    if not args.dry_run and args.output_iso is None:
        parser.error("output_iso is required unless --dry-run is used")
    if args.output_iso is not None and args.input_iso.resolve() == args.output_iso.resolve():
        parser.error("input and output ISO must differ")
    if args.output_iso is not None and args.output_iso.exists():
        parser.error(f"refusing to overwrite existing output: {args.output_iso}")
    if args.input_iso.stat().st_size != SUPPORTED_ISO_SIZE:
        parser.error(
            f"unsupported ISO size: {args.input_iso.stat().st_size} != {SUPPORTED_ISO_SIZE}"
        )
    input_sha256 = sha256_file(args.input_iso)
    if input_sha256 != SUPPORTED_ISO_SHA256:
        parser.error(
            "unsupported ISO SHA-256: "
            f"{input_sha256} != {SUPPORTED_ISO_SHA256}"
        )

    original_global = read_exact(args.input_iso, GLOBAL_ARCHIVE_OFFSET, GLOBAL_ARCHIVE_SIZE)
    original_target = read_exact(args.input_iso, TARGET_ARCHIVE_OFFSET, TARGET_ARCHIVE_SIZE)
    patched_global, global_mclib, code_map, global_details = extend_global_font(
        original_global, args.font
    )
    patched_target, target_mclib, target_details = patch_target_archive(
        original_target, code_map
    )

    if args.preview:
        write_preview(global_mclib, target_mclib, code_map, args.preview)

    report: dict[str, object] = {
        "input_iso": str(args.input_iso),
        "output_iso": str(args.output_iso) if args.output_iso else None,
        "translation": {
            "speaker": SPEAKER,
            "lines": [LINE_1, LINE_2, LINE_3, LINE_4],
            "display_lines": list(DISPLAY_LINES),
        },
        "font": str(args.font),
        "font_sha256": sha256_file(args.font),
        "font_gray_levels": FONT_GRAY_LEVELS,
        "hangul_characters": hangul_characters(),
        "hangul_code_map": code_map,
        "global_archive": {
            "iso_offset": GLOBAL_ARCHIVE_OFFSET,
            "size": GLOBAL_ARCHIVE_SIZE,
            "sha256": sha256(patched_global),
            "mclib_sha256": sha256(global_mclib),
            **global_details,
        },
        "target_archive": {
            "archive_id": 1220,
            "iso_offset": TARGET_ARCHIVE_OFFSET,
            "size": TARGET_ARCHIVE_SIZE,
            "member_iso_offset": TARGET_ARCHIVE_OFFSET + TARGET_MEMBER_REL,
            "message_id": TARGET_MESSAGE_ID,
            "sha256": sha256(patched_target),
            "mclib_sha256": sha256(target_mclib),
            **target_details,
        },
        "dry_run": args.dry_run,
    }

    if not args.dry_run:
        assert args.output_iso is not None
        args.output_iso.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.input_iso, args.output_iso)
        with args.output_iso.open("r+b") as output:
            output.seek(GLOBAL_ARCHIVE_OFFSET)
            output.write(patched_global)
            output.seek(TARGET_ARCHIVE_OFFSET)
            output.write(patched_target)
        if args.output_iso.stat().st_size != args.input_iso.stat().st_size:
            raise AssertionError("output ISO size changed")
        verify_written_archive(args.output_iso, GLOBAL_ARCHIVE_OFFSET, patched_global)
        verify_written_archive(args.output_iso, TARGET_ARCHIVE_OFFSET, patched_target)
        report["output_size"] = args.output_iso.stat().st_size
        report["output_iso_sha256"] = sha256_file(args.output_iso)

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a NanumSquareNeo SO3 Disc 1 verification patch.

The output includes the Korean first-dialogue patch and replaces thirteen kanji
bitmaps in the immediately following hotel scene with their Korean readings:

    当ホテル... / 一切の人工生物が使用...
    당ホテル... / 일절の인공생물が사용...

Only the global font archive and archive 1220 are written. The message
bytecode, local glyph count, and every unrelated glyph bitmap remain intact.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path

from PIL import Image


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import patch_first_dialogue as first  # noqa: E402
from so3_repack import (  # noqa: E402
    Mclib,
    compress_slz_mode2,
    decompress_slz_payload,
    render_glyphs,
)


FONT_FILENAME = "NanumSquareNeo-cBd.ttf"
FONT_SHA256 = "4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767"
FONT_GRAY_LEVELS = 3
BASE_TARGET_MCLIB_SHA256 = (
    "DFDBDC2BA6E9E91A756930A6A70A99C78A350FC143B4F432D9222970DC535768"
)
BASE_TARGET_ARCHIVE_SHA256 = (
    "FC3D44EEDD2FC618E1CAD1104366ECE563720A60B1660BCDBE6F40C85A7D45BE"
)

# Bitmap indices are unchanged when the first-dialogue patch moves the local
# code base 301 -> 307. The message operands therefore become old_code + 6.
READING_ROWS = (
    ("号", "호", 8, 309, 315),
    ("室", "실", 9, 310, 316),
    ("無", "무", 12, 313, 319),
    ("何", "하", 33, 334, 340),
    ("当", "당", 39, 340, 346),
    ("一", "일", 40, 341, 347),
    ("切", "절", 41, 342, 348),
    ("人", "인", 42, 343, 349),
    ("工", "공", 43, 344, 350),
    ("生", "생", 44, 345, 351),
    ("物", "물", 45, 346, 352),
    ("使", "사", 46, 347, 353),
    ("用", "용", 47, 348, 354),
)


def _path_alias_key(path: Path) -> str:
    """Return a stable key for existing or not-yet-created paths."""

    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot normalize path {path}: {exc}") from exc
    return os.path.normcase(str(resolved))


def _paths_alias(left: Path, right: Path) -> bool:
    if _path_alias_key(left) == _path_alias_key(right):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def reject_path_aliases(**named_paths: Path | None) -> None:
    """Reject any two supplied artifact paths that resolve to the same file."""

    active = [(name, path) for name, path in named_paths.items() if path is not None]
    for index, (left_name, left) in enumerate(active):
        for right_name, right in active[index + 1 :]:
            if _paths_alias(left, right):
                raise ValueError(
                    f"path collision: {right_name} aliases {left_name}: {right}"
                )


def validate_artifact_paths(
    input_iso: Path,
    font: Path,
    output_iso: Path | None,
    preview: Path | None,
    report: Path | None,
) -> None:
    """Protect source files and keep every writable artifact distinct."""

    reject_path_aliases(
        input_iso=input_iso,
        font=font,
        output_iso=output_iso,
        preview=preview,
        report=report,
    )


def validate_reading_rows(rows=READING_ROWS) -> None:
    """Reject malformed or ambiguous local-glyph replacement mappings."""

    indices: set[int] = set()
    original_codes: set[int] = set()
    patched_codes: set[int] = set()
    for kanji, reading, index, original_code, patched_code in rows:
        if len(kanji) != 1 or len(reading) != 1:
            raise ValueError("each kanji and Korean reading must be one Unicode character")
        if not 0 <= index < 72:
            raise ValueError(f"local glyph index out of range: {index}")
        if original_code != 301 + index:
            raise ValueError(f"invalid original code for local glyph index {index}")
        if patched_code != 307 + index:
            raise ValueError(f"invalid patched code for local glyph index {index}")
        if index in indices:
            raise ValueError(f"duplicate local glyph index: {index}")
        if original_code in original_codes:
            raise ValueError(f"duplicate original glyph code: {original_code}")
        if patched_code in patched_codes:
            raise ValueError(f"duplicate patched glyph code: {patched_code}")
        indices.add(index)
        original_codes.add(original_code)
        patched_codes.add(patched_code)

# Actual post-remap operands for message 14. Kana and punctuation remain in
# the game's global font; only codes 346..354 select the replaced local slots.
PREVIEW_LINES = (
    (346, 187, 176, 198, 101, 226, 196, 159, 222, 157, 177, 220, 157, 174, 98, 102),
    (347, 348, 101, 349, 350, 351, 352, 122, 353, 354, 87, 118, 95, 81, 116, 107, 90, 121, 237),
)


def replace_reading_glyphs(
    decoded: bytes, font_path: Path
) -> tuple[bytes, dict[str, object]]:
    validate_reading_rows()
    if first.sha256(decoded) != BASE_TARGET_MCLIB_SHA256:
        raise ValueError("first-dialogue target mclib SHA-256 mismatch")
    parsed = Mclib.parse(decoded)
    if (parsed.local_base, parsed.glyph_count, parsed.glyph_width, parsed.glyph_height) != (
        307,
        72,
        24,
        24,
    ):
        raise ValueError("unexpected target local-font geometry")

    readings = "".join(row[1] for row in READING_ROWS)
    rendered = render_glyphs(
        readings,
        font_path,
        24,
        24,
        24,
        gray_levels=FONT_GRAY_LEVELS,
    )
    glyph_bytes = parsed.glyph_stride * parsed.glyph_height // 2
    rebuilt = bytearray(decoded)
    changed_slots: list[dict[str, object]] = []
    for (kanji, reading, index, old_code, new_code), (width, bitmap) in zip(
        READING_ROWS, rendered
    ):
        if len(bitmap) != glyph_bytes:
            raise AssertionError("rendered glyph byte length mismatch")
        old_bitmap_start = parsed.bitmap_start + index * glyph_bytes
        old_bitmap = bytes(rebuilt[old_bitmap_start : old_bitmap_start + glyph_bytes])
        old_width = rebuilt[parsed.width_start + index]
        rebuilt[old_bitmap_start : old_bitmap_start + glyph_bytes] = bitmap
        changed_slots.append(
            {
                "kanji": kanji,
                "reading": reading,
                "local_index": index,
                "original_code": old_code,
                "patched_code": new_code,
                "old_width": old_width,
                "new_width": old_width,
                "rendered_width": width,
                "old_bitmap_sha256": first.sha256(old_bitmap),
                "new_bitmap_sha256": first.sha256(bitmap),
            }
        )

    checked = Mclib.parse(bytes(rebuilt))
    if checked.local_base != parsed.local_base or checked.glyph_count != parsed.glyph_count:
        raise AssertionError("local font geometry changed")
    if checked.widths != parsed.widths:
        raise AssertionError("local glyph advances changed")
    changed_indices = {row[2] for row in READING_ROWS}
    for index in range(parsed.glyph_count):
        start = index * glyph_bytes
        end = start + glyph_bytes
        if index in changed_indices:
            if checked.bitmaps[start:end] == parsed.bitmaps[start:end]:
                raise AssertionError(f"local glyph {index} did not change")
        else:
            if checked.bitmaps[start:end] != parsed.bitmaps[start:end]:
                raise AssertionError(f"unrelated local glyph {index} changed")
            if checked.widths[index] != parsed.widths[index]:
                raise AssertionError(f"unrelated local width {index} changed")
    if bytes(rebuilt[parsed.text_start : parsed.width_start]) != decoded[
        parsed.text_start : parsed.width_start
    ]:
        raise AssertionError("message bytecode changed during bitmap replacement")
    return bytes(rebuilt), {
        "gray_levels": FONT_GRAY_LEVELS,
        "changed_glyph_count": len(changed_slots),
        "changed_slots": changed_slots,
    }


def repack_target_archive(
    base_archive: bytes, font_path: Path
) -> tuple[bytes, bytes, dict[str, object]]:
    if first.sha256(base_archive) != BASE_TARGET_ARCHIVE_SHA256:
        raise ValueError("first-dialogue target archive SHA-256 mismatch")
    rows = first.parse_pk1_table(base_archive)
    tag, record_id, old_record_size, record_offset = rows[first.TARGET_RECORD_INDEX]
    if (tag, record_id, old_record_size, record_offset) != (
        b"DCMS",
        0,
        0x28D0,
        first.TARGET_MEMBER_REL,
    ):
        raise ValueError("unexpected first-dialogue target record")

    decoded, old_slz = first.decode_inner_slz(base_archive, record_offset)
    if old_slz != {"mode": 2, "compressed": 10429, "unpacked": 22528, "next_rel": 0}:
        raise ValueError(f"unexpected first-dialogue target SLZ: {old_slz}")
    rebuilt, details = replace_reading_glyphs(decoded, font_path)
    compressed = compress_slz_mode2(rebuilt)
    if decompress_slz_payload(compressed, 2, len(rebuilt)) != rebuilt:
        raise AssertionError("reading-patched target SLZ round trip failed")

    new_record = b"SLZ\x02" + struct.pack("<III", len(compressed), len(rebuilt), 0) + compressed
    used_record_size = first.align(len(new_record), 4)
    new_record += b"\0" * (used_record_size - len(new_record))
    if used_record_size > old_record_size:
        raise ValueError(
            f"reading target needs {used_record_size} bytes, "
            f"but its fixed record allocation is {old_record_size} bytes"
        )

    old_first_end = max(offset + size for _, _, size, offset in rows)
    if old_first_end != 0xFCE98:
        raise ValueError(f"unexpected patched first PK1 end 0x{old_first_end:X}")
    gap = first.TARGET_SECOND_PACKAGE_REL - old_first_end
    if any(base_archive[old_first_end : first.TARGET_SECOND_PACKAGE_REL]):
        raise ValueError("first/second PK1 padding is not zero")

    # Keep the PK1 table, every record offset, and every following record byte
    # identical. The recompressed payload is smaller than the existing target
    # allocation, so only the target record needs to be replaced and zero-filled.
    archive = bytearray(base_archive)
    padded_record = new_record + b"\0" * (old_record_size - used_record_size)
    archive[record_offset : record_offset + old_record_size] = padded_record

    new_rows = first.parse_pk1_table(archive)
    if new_rows != rows:
        raise AssertionError("PK1 table changed during fixed-allocation replacement")
    for index, (old_row, new_row) in enumerate(zip(rows, new_rows)):
        old_tag, old_id, old_size, old_offset = old_row
        new_tag, new_id, new_size, new_offset = new_row
        if (new_tag, new_id, new_size, new_offset) != old_row:
            raise AssertionError(f"PK1 metadata changed for row {index}")
        if index != first.TARGET_RECORD_INDEX:
            if bytes(archive[new_offset : new_offset + new_size]) != base_archive[
                old_offset : old_offset + old_size
            ]:
                raise AssertionError(f"unrelated PK1 record {index} changed")
    if bytes(archive[first.TARGET_SECOND_PACKAGE_REL :]) != base_archive[
        first.TARGET_SECOND_PACKAGE_REL :
    ]:
        raise AssertionError("second PK1 package changed")

    verified, new_slz = first.decode_inner_slz(archive, record_offset)
    if verified != rebuilt:
        raise AssertionError("reading-patched target member verification failed")
    details.update(
        {
            "old_compressed": old_slz["compressed"],
            "new_compressed": new_slz["compressed"],
            "old_record_size": old_record_size,
            "new_record_size": old_record_size,
            "used_record_size": used_record_size,
            "unused_record_bytes": old_record_size - used_record_size,
            "record_growth": 0,
            "old_package_gap": gap,
            "remaining_package_gap": gap,
            "mclib_sha256": first.sha256(rebuilt),
            "archive_sha256": first.sha256(archive),
        }
    )
    return bytes(archive), rebuilt, details


def write_preview(global_mclib: bytes, target_mclib: bytes, output: Path) -> None:
    widths: list[int] = []
    for line in PREVIEW_LINES:
        advances = [
            first.preview_width_and_bitmap(global_mclib, target_mclib, code)[0]
            for code in line
        ]
        widths.append(sum(advances[:-1]) + 24)
    canvas = Image.new("L", (max(widths), len(PREVIEW_LINES) * 28), 0)
    for row, line in enumerate(PREVIEW_LINES):
        cursor = 0
        for code in line:
            width, bitmap = first.preview_width_and_bitmap(global_mclib, target_mclib, code)
            pixels: list[int] = []
            for value in bitmap:
                pixels.extend(((value & 0x0F) * 17, (value >> 4) * 17))
            glyph = Image.new("L", (24, 24))
            glyph.putdata(pixels[: 24 * 24])
            canvas.paste(255, (cursor, row * 28), glyph)
            cursor += width
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.resize((canvas.width * 4, canvas.height * 4), Image.Resampling.NEAREST).save(output)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_iso", type=Path)
    parser.add_argument("output_iso", type=Path, nargs="?")
    parser.add_argument(
        "--font",
        type=Path,
        help=(
            "NanumSquareNeo-cBd.ttf; defaults to the input ISO directory"
        ),
    )
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    validate_reading_rows()
    font = (
        args.font
        if args.font is not None
        else args.input_iso.with_name(FONT_FILENAME)
    )
    try:
        validate_artifact_paths(
            args.input_iso, font, args.output_iso, args.preview, args.report
        )
    except ValueError as exc:
        parser.error(str(exc))

    if not args.input_iso.is_file():
        parser.error(f"input ISO does not exist: {args.input_iso}")
    if args.input_iso.stat().st_size != first.SUPPORTED_ISO_SIZE:
        parser.error("unsupported input ISO size")
    input_sha256 = first.sha256_file(args.input_iso)
    if input_sha256 != first.SUPPORTED_ISO_SHA256:
        parser.error(f"unsupported input ISO SHA-256: {input_sha256}")
    if not font.is_file():
        parser.error(f"font does not exist: {font}")
    font_sha256 = first.sha256_file(font)
    if font_sha256 != FONT_SHA256:
        parser.error(f"unexpected NanumSquareNeo-cBd.ttf SHA-256: {font_sha256}")
    if not args.dry_run and args.output_iso is None:
        parser.error("output_iso is required unless --dry-run is used")
    if args.output_iso is not None and args.output_iso.exists():
        parser.error(f"refusing to overwrite output: {args.output_iso}")

    original_global = first.read_exact(
        args.input_iso, first.GLOBAL_ARCHIVE_OFFSET, first.GLOBAL_ARCHIVE_SIZE
    )
    original_target = first.read_exact(
        args.input_iso, first.TARGET_ARCHIVE_OFFSET, first.TARGET_ARCHIVE_SIZE
    )
    patched_global, global_mclib, code_map, global_details = first.extend_global_font(
        original_global, font, gray_levels=FONT_GRAY_LEVELS
    )
    base_target, _, base_target_details = first.patch_target_archive(
        original_target, code_map
    )
    patched_target, target_mclib, reading_details = repack_target_archive(
        base_target, font
    )
    if args.preview:
        write_preview(global_mclib, target_mclib, args.preview)

    report: dict[str, object] = {
        "input_iso": str(args.input_iso),
        "output_iso": str(args.output_iso) if args.output_iso else None,
        "input_iso_sha256": input_sha256,
        "font": str(font),
        "font_sha256": font_sha256,
        "font_gray_levels": FONT_GRAY_LEVELS,
        "first_dialogue": list(first.DISPLAY_LINES),
        "reading_preview": [
            "당ホテルのプライベートビーチには",
            "일절の인공생물が사용されておりません。",
        ],
        "reading_source": "Unicode Unihan kHangul; explicit early-scene mapping",
        "global_archive": {
            "sha256": first.sha256(patched_global),
            "mclib_sha256": first.sha256(global_mclib),
            **global_details,
        },
        "target_archive": {
            "sha256": first.sha256(patched_target),
            "base_first_dialogue_record_growth": base_target_details["record_growth"],
            **reading_details,
        },
        "dry_run": args.dry_run,
    }

    if not args.dry_run:
        assert args.output_iso is not None
        args.output_iso.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.input_iso, args.output_iso)
        with args.output_iso.open("r+b") as output:
            output.seek(first.GLOBAL_ARCHIVE_OFFSET)
            output.write(patched_global)
            output.seek(first.TARGET_ARCHIVE_OFFSET)
            output.write(patched_target)
        if args.output_iso.stat().st_size != args.input_iso.stat().st_size:
            raise AssertionError("output ISO size changed")
        first.verify_written_archive(
            args.output_iso, first.GLOBAL_ARCHIVE_OFFSET, patched_global
        )
        first.verify_written_archive(
            args.output_iso, first.TARGET_ARCHIVE_OFFSET, patched_target
        )
        report["output_iso_sha256"] = first.sha256_file(args.output_iso)
        report["output_size"] = args.output_iso.stat().st_size

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

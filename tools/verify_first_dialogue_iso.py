#!/usr/bin/env python3
"""Verify a first-dialogue ISO without private extraction manifests."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import patch_first_dialogue as patch  # noqa: E402
from so3_repack import read_index  # noqa: E402


EXPECTED_TARGET_MCLIB_SHA256 = (
    "DFDBDC2BA6E9E91A756930A6A70A99C78A350FC143B4F432D9222970DC535768"
)


def verify_target_records(
    original_archive: bytes, patched_archive: bytes
) -> dict[str, int]:
    """Check every record in archive 1220's first PK1 package."""
    old_rows = patch.parse_pk1_table(original_archive)
    new_rows = patch.parse_pk1_table(patched_archive)
    if len(old_rows) != 12 or len(new_rows) != len(old_rows):
        raise AssertionError("archive 1220 PK1 record count changed")

    old_target_size = old_rows[patch.TARGET_RECORD_INDEX][2]
    new_target_size = new_rows[patch.TARGET_RECORD_INDEX][2]
    delta = new_target_size - old_target_size
    if delta <= 0:
        raise AssertionError("target record did not grow")

    changed = unchanged = 0
    for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
        old_tag, old_id, old_size, old_offset = old_row
        new_tag, new_id, new_size, new_offset = new_row
        if (new_tag, new_id) != (old_tag, old_id):
            raise AssertionError(f"PK1 record {index} identity changed")
        expected_offset = old_offset + (delta if index > patch.TARGET_RECORD_INDEX else 0)
        expected_size = new_target_size if index == patch.TARGET_RECORD_INDEX else old_size
        if (new_size, new_offset) != (expected_size, expected_offset):
            raise AssertionError(f"PK1 record {index} metadata mismatch")

        old_record = original_archive[old_offset : old_offset + old_size]
        new_record = patched_archive[new_offset : new_offset + new_size]
        if index == patch.TARGET_RECORD_INDEX:
            if old_record == new_record:
                raise AssertionError("target PK1 record did not change")
            changed += 1
        else:
            if old_record != new_record:
                raise AssertionError(f"PK1 record {index} changed unexpectedly")
            unchanged += 1

    if original_archive[patch.TARGET_SECOND_PACKAGE_REL :] != patched_archive[
        patch.TARGET_SECOND_PACKAGE_REL :
    ]:
        raise AssertionError("archive 1220's second PK1 package changed")

    return {
        "checked": len(old_rows),
        "unchanged": unchanged,
        "changed": changed,
        "target_record_growth": delta,
    }


def verify_global_roots(
    original_archive: bytes, patched_archive: bytes
) -> dict[str, int | str]:
    checked = 0
    global_header: dict[str, int] | None = None
    for old_rel, new_rel, should_change in (
        (0x10, 0x10, False),
        (0x3D90, 0x3D90, False),
        (patch.GLOBAL_MEMBER_REL, patch.GLOBAL_MEMBER_REL, True),
        (
            patch.GLOBAL_OLD_DTT_WRAPPER_REL + 0x10,
            patch.GLOBAL_NEW_DTT_WRAPPER_REL + 0x10,
            False,
        ),
    ):
        old_decoded, old_header = patch.decode_inner_slz(original_archive, old_rel)
        new_decoded, new_header = patch.decode_inner_slz(patched_archive, new_rel)
        if should_change:
            if old_decoded == new_decoded:
                raise AssertionError("global font did not change")
            global_header = new_header
        elif old_header != new_header or old_decoded != new_decoded:
            raise AssertionError(f"archive 8 root at 0x{old_rel:X} changed")
        checked += 1

    if global_header is None:
        raise AssertionError("global font header was not checked")

    # Validate the complete outer ZLS chain, not only its inner payloads.
    if patched_archive[:16] != original_archive[:16] or patched_archive[
        0x3D80 : 0x3D90
    ] != original_archive[0x3D80 : 0x3D90]:
        raise AssertionError("unchanged archive 8 wrapper metadata changed")
    expected_wrappers = (
        (0x0000, (b"ZLS\0", 0x3D3C, 0x0000, 0x3D80)),
        (0x3D80, (b"ZLS\0", 0x47A0, 0x3D80, 0x4800)),
        (
            patch.GLOBAL_WRAPPER_REL,
            (
                b"ZLS\0",
                patch.align(16 + global_header["compressed"], 4),
                0x4800,
                0x6380,
            ),
        ),
        (
            patch.GLOBAL_NEW_DTT_WRAPPER_REL,
            (b"ZLS\0", 0x0610, 0x6380, 0x0680),
        ),
    )
    for offset, expected in expected_wrappers:
        actual = struct.unpack_from("<4sIII", patched_archive, offset)
        if actual != expected:
            raise AssertionError(
                f"archive 8 wrapper at 0x{offset:X}: {actual!r} != {expected!r}"
            )
        _, size, _, next_rel = actual
        padding = patched_archive[offset + 16 + size : offset + next_rel]
        if any(padding):
            raise AssertionError(f"archive 8 padding after 0x{offset:X} is nonzero")
    end_marker = struct.unpack_from(
        "<4sIII", patched_archive, patch.GLOBAL_NEW_END_REL
    )
    if end_marker != (b"DNE\0", 0, 0x0680, 0):
        raise AssertionError(f"archive 8 end marker mismatch: {end_marker!r}")
    if any(patched_archive[patch.GLOBAL_NEW_END_REL + 16 :]):
        raise AssertionError("archive 8 tail padding is nonzero")

    global_mclib, _ = patch.decode_inner_slz(
        patched_archive, patch.GLOBAL_MEMBER_REL
    )
    words = struct.unpack_from("<13I", global_mclib, 0x10)
    if (words[4], words[8], words[9], words[12]) != (306, 24, 24, len(global_mclib)):
        raise AssertionError("patched global font geometry mismatch")
    return {
        "checked": checked,
        "mclib_sha256": patch.sha256(global_mclib),
        "glyph_count": words[4],
    }


def full_diff(original: Path, patched: Path) -> dict[str, object]:
    regions = (
        (
            patch.GLOBAL_ARCHIVE_OFFSET,
            patch.GLOBAL_ARCHIVE_OFFSET + patch.GLOBAL_ARCHIVE_SIZE,
            "global_font_archive",
        ),
        (
            patch.TARGET_ARCHIVE_OFFSET,
            patch.TARGET_ARCHIVE_OFFSET + patch.TARGET_ARCHIVE_SIZE,
            "dialogue_archive_1220",
        ),
    )
    totals = {name: 0 for _, _, name in regions}
    changed = outside = 0
    first = last = None
    position = 0
    with original.open("rb") as left, patched.open("rb") as right:
        while True:
            a = left.read(8 * 1024 * 1024)
            b = right.read(8 * 1024 * 1024)
            if not a and not b:
                break
            if len(a) != len(b):
                raise AssertionError("ISO sizes diverged during full diff")
            if a != b:
                for index, (old, new) in enumerate(zip(a, b)):
                    if old == new:
                        continue
                    offset = position + index
                    changed += 1
                    first = offset if first is None else first
                    last = offset
                    name = next(
                        (name for start, end, name in regions if start <= offset < end),
                        None,
                    )
                    if name is None:
                        outside += 1
                    else:
                        totals[name] += 1
            position += len(a)
    if outside:
        raise AssertionError(f"{outside} bytes changed outside authorized archives")
    return {
        "bytes_scanned": position,
        "changed_bytes": changed,
        "changed_outside_authorized_archives": outside,
        "first_changed_offset": first,
        "last_changed_offset": last,
        "changed_by_archive": totals,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_iso", type=Path)
    parser.add_argument("patched_iso", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--full-diff", action="store_true")
    args = parser.parse_args()

    if args.original_iso.stat().st_size != args.patched_iso.stat().st_size:
        raise AssertionError("patched ISO size differs from original")
    original_sha256 = patch.sha256_file(args.original_iso)
    if original_sha256 != patch.SUPPORTED_ISO_SHA256:
        raise AssertionError(f"unsupported original ISO SHA-256: {original_sha256}")
    if read_index(args.original_iso) != read_index(args.patched_iso):
        raise AssertionError("hidden archive index changed")

    old_global = patch.read_exact(
        args.original_iso, patch.GLOBAL_ARCHIVE_OFFSET, patch.GLOBAL_ARCHIVE_SIZE
    )
    new_global = patch.read_exact(
        args.patched_iso, patch.GLOBAL_ARCHIVE_OFFSET, patch.GLOBAL_ARCHIVE_SIZE
    )
    old_target = patch.read_exact(
        args.original_iso, patch.TARGET_ARCHIVE_OFFSET, patch.TARGET_ARCHIVE_SIZE
    )
    new_target = patch.read_exact(
        args.patched_iso, patch.TARGET_ARCHIVE_OFFSET, patch.TARGET_ARCHIVE_SIZE
    )

    global_roots = verify_global_roots(old_global, new_global)
    target_records = verify_target_records(old_target, new_target)
    target_decoded, target_header = patch.decode_inner_slz(
        new_target, patch.TARGET_MEMBER_REL
    )
    target_mclib = patch.Mclib.parse(target_decoded)
    target_sha256 = patch.sha256(target_decoded)
    if target_sha256 != EXPECTED_TARGET_MCLIB_SHA256:
        raise AssertionError(f"patched target mclib hash mismatch: {target_sha256}")
    if target_mclib.local_base != 307:
        raise AssertionError("patched scene local base is not 307")

    result: dict[str, object] = {
        "size": args.patched_iso.stat().st_size,
        "original_iso_sha256": original_sha256,
        "patched_iso_sha256": patch.sha256_file(args.patched_iso),
        "hidden_index_unchanged": True,
        "archive_8_roots": global_roots,
        "archive_1220_records": target_records,
        "target_slz": target_header,
        "target_mclib_sha256": target_sha256,
        "target_local_base": target_mclib.local_base,
    }
    if args.full_diff:
        result["full_diff"] = full_diff(args.original_iso, args.patched_iso)

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

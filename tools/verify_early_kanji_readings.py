#!/usr/bin/env python3
"""Read-only verifier for the SO3 early kanji-reading ISO patch."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import patch_first_dialogue as first  # noqa: E402
import patch_early_kanji_readings as reading_patch  # noqa: E402
from so3_repack import (  # noqa: E402
    Mclib,
    compress_slz_mode2,
    decompress_slz_payload,
    read_index,
)


EXPECTED_OUTPUT_ISO_SHA256 = (
    "FD298A889002FF3AC23B43CF8433B1BE50EECC15ACE81E61DD48B759DF800F9B"
)
EXPECTED_GLOBAL_ARCHIVE_SHA256 = (
    "0C9BB9000FE13B7A0BD5D08057B76BDED341ED9DB0C276BC90C8A1949327692B"
)
EXPECTED_GLOBAL_MCLIB_SHA256 = (
    "843E25AAB01EDECF08FFE267BD9DD85F4816416E67C716B292784E0119F3895D"
)
EXPECTED_BASE_TARGET_ARCHIVE_SHA256 = (
    "FC3D44EEDD2FC618E1CAD1104366ECE563720A60B1660BCDBE6F40C85A7D45BE"
)
EXPECTED_BASE_TARGET_MCLIB_SHA256 = (
    "DFDBDC2BA6E9E91A756930A6A70A99C78A350FC143B4F432D9222970DC535768"
)
EXPECTED_TARGET_ARCHIVE_SHA256 = (
    "58ACAC705237C5FF23C36B6D646BE55C0D71632CDBB60E0443F618D6847659EE"
)
EXPECTED_TARGET_MCLIB_SHA256 = (
    "37A167F230D9CE019A9846FC2C4468C1488132D365070EFA361945561B7F4718"
)

# The verifier deliberately owns this table instead of importing it from the
# patcher.  That makes a changed patch specification fail verification rather
# than silently changing the verification target with it.
READING_SLOTS = (
    ("号", "호", 8, 309, 315, "7D2653EBD67F3EBD8E834C02C46BBAB9086B11381A91A11142F3DC339C64ECA6", "78B27B84EA753754069E2EC75C335E3300CCC40D2A8A3778F8D5B6373675D781"),
    ("室", "실", 9, 310, 316, "B53A26DA58D94B2C17FDF8441176DC4D049B5F478968A18A4A45A3A52D07729F", "EE455E60E5620319F5417870A040413D0EFA8E2D5AAFFAFC420AB322DC0D9DE6"),
    ("無", "무", 12, 313, 319, "E7916D3124BF53ADFDD1A0719F378AE19DAEC6C585D7018C099623C64148B4C3", "13952987F0A7FF1913F255EA4A89E446724ACC4957F5D6BB47827FB3D0996684"),
    ("何", "하", 33, 334, 340, "B0A110B670164EDE0300CE2C250EC2CBBABBE745CA6565ECD17D8B26B1193E4B", "D02253320FACDE359905640ACBA2942B414BB4BECBC37A30D96BEC59CA1A038A"),
    ("当", "당", 39, 340, 346, "773FB54D1262D662A89723656695B2BE61713D537807FE093898D850263E7A3F", "D777BAA97DA5E15FA964463F192593959BBC9E0D0DED5485392874C98FAF7838"),
    ("一", "일", 40, 341, 347, "074783F723843C0BC64D55CA579463A005EEA7AED76348F0931D3C38F4E52266", "2ABDD5B9A781D9455B44788B82213609119277806210A26406A6E7FB2072C72F"),
    ("切", "절", 41, 342, 348, "02EC1365DDF19F56F3A162531DF45B5F8F30F3EED026DB1F92076DF852924C5E", "28CD7E51D1CF2587E63741287488558914BAC6C9B98071EB9516DE68040DB172"),
    ("人", "인", 42, 343, 349, "411BE09018EA712059AFA2A9B2A60971433645F565791630508A5BDFC2141A77", "49DC672D63418CDB47929355B57370D0FAB2BCD32D77C01D8211C306187A22F3"),
    ("工", "공", 43, 344, 350, "E6B648EBE388AB2935F24F2954D5994A3CBD252A1EF5450577438120BCF027A7", "33327E274ECF66D5EE4F19849EF5FF77037333560D2D50C7166181A203D539C6"),
    ("生", "생", 44, 345, 351, "A96F29E24002E316B68DA2D02677D39FC215EA439564F6EC39949688ECA932B9", "DB67D5D6EF59DD1422C63F4A0037FBE8D7CF3747228166832EBBE58B173EF8F8"),
    ("物", "물", 45, 346, 352, "592CDBDCDC197EA3810B499D04B5879F71F8AB1E0591CA987C843CCEF4CD2FCF", "DA23E6D46F4083060230F95C7BEA56FD25B4F60D95803AA76C525B4F94151AEE"),
    ("使", "사", 46, 347, 353, "FE2DCF2FCEF242872A26C3BDF5C228431A0B5F15EAC4F9A85EBE2D922FC291EB", "01D985BE7C27FCC44B27C4B2F5FCF3FB72AF0690D7D064DE2330DA086CC27CA1"),
    ("用", "용", 47, 348, 354, "1C78301C6B5ADEE4C099B22F2CF908416B7FBCA87862B02A75DC62AC6BFFB88E", "8723AEC96A44BF1AC419F46F7061857D19648835ECCA27EC29494F6BC02FC4B0"),
)


def validate_artifact_paths(
    original_iso: Path, patched_iso: Path, report: Path | None
) -> None:
    """Keep a verifier report from aliasing either protected ISO."""

    reading_patch.reject_path_aliases(
        original_iso=original_iso,
        patched_iso=patched_iso,
        report=report,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def full_iso_diff(original: Path, patched: Path) -> dict[str, object]:
    """Hash both ISOs and prove every byte difference is in two fixed ranges."""

    regions = (
        (
            first.GLOBAL_ARCHIVE_OFFSET,
            first.GLOBAL_ARCHIVE_OFFSET + first.GLOBAL_ARCHIVE_SIZE,
            "archive_8_global_font",
        ),
        (
            first.TARGET_ARCHIVE_OFFSET,
            first.TARGET_ARCHIVE_OFFSET + first.TARGET_ARCHIVE_SIZE,
            "archive_1220_dialogue",
        ),
    )
    stats: dict[str, dict[str, int | None]] = {
        name: {"changed_bytes": 0, "first_changed": None, "last_changed": None, "run_count": 0}
        for _, _, name in regions
    }
    left_hash = hashlib.sha256()
    right_hash = hashlib.sha256()
    position = 0
    changed_bytes = 0
    outside = 0
    open_run_name: str | None = None
    previous_changed = -2

    with original.open("rb") as left, patched.open("rb") as right:
        while True:
            old_chunk = left.read(8 * 1024 * 1024)
            new_chunk = right.read(8 * 1024 * 1024)
            if not old_chunk and not new_chunk:
                break
            require(len(old_chunk) == len(new_chunk), "ISO sizes diverged during full diff")
            left_hash.update(old_chunk)
            right_hash.update(new_chunk)
            if old_chunk != new_chunk:
                for relative, (old, new) in enumerate(zip(old_chunk, new_chunk)):
                    if old == new:
                        continue
                    offset = position + relative
                    changed_bytes += 1
                    name = next(
                        (label for start, end, label in regions if start <= offset < end),
                        None,
                    )
                    if name is None:
                        outside += 1
                        open_run_name = None
                    else:
                        region = stats[name]
                        region["changed_bytes"] = int(region["changed_bytes"] or 0) + 1
                        if region["first_changed"] is None:
                            region["first_changed"] = offset
                        region["last_changed"] = offset
                        if offset != previous_changed + 1 or open_run_name != name:
                            region["run_count"] = int(region["run_count"] or 0) + 1
                        open_run_name = name
                    previous_changed = offset
            position += len(old_chunk)

    original_sha = left_hash.hexdigest().upper()
    patched_sha = right_hash.hexdigest().upper()
    require(position == first.SUPPORTED_ISO_SIZE, "unexpected number of ISO bytes scanned")
    require(original_sha == first.SUPPORTED_ISO_SHA256, f"original ISO SHA-256 mismatch: {original_sha}")
    require(patched_sha == EXPECTED_OUTPUT_ISO_SHA256, f"patched ISO SHA-256 mismatch: {patched_sha}")
    require(outside == 0, f"{outside} bytes changed outside the two authorized archives")
    require(changed_bytes > 0, "patched ISO is byte-identical to the original")
    for _, _, name in regions:
        require(int(stats[name]["changed_bytes"] or 0) > 0, f"{name} did not change")

    return {
        "bytes_scanned": position,
        "original_iso_sha256": original_sha,
        "patched_iso_sha256": patched_sha,
        "changed_bytes": changed_bytes,
        "changed_outside_authorized_ranges": outside,
        "authorized_ranges": [
            {"name": name, "start": start, "end_exclusive": end, "size": end - start}
            for start, end, name in regions
        ],
        "observed_changes": stats,
    }


def verify_global_archive(original: bytes, patched: bytes) -> dict[str, object]:
    require(first.sha256(original) == first.GLOBAL_ARCHIVE_SHA256, "original global archive hash mismatch")
    archive_sha = first.sha256(patched)
    require(archive_sha == EXPECTED_GLOBAL_ARCHIVE_SHA256, f"patched global archive hash mismatch: {archive_sha}")

    global_mclib, header = first.decode_inner_slz(patched, first.GLOBAL_MEMBER_REL)
    require(
        header == {"mode": 2, "compressed": 25383, "unpacked": 88704, "next_rel": 0},
        f"patched global SLZ header mismatch: {header}",
    )
    mclib_sha = first.sha256(global_mclib)
    require(mclib_sha == EXPECTED_GLOBAL_MCLIB_SHA256, f"patched global mclib hash mismatch: {mclib_sha}")
    width_start = first.u32(global_mclib, 0x18)
    bitmap_start = first.u32(global_mclib, 0x1C)
    glyph_count = first.u32(global_mclib, 0x20)
    glyph_height = first.u32(global_mclib, 0x30)
    glyph_stride = first.u32(global_mclib, 0x34)
    local_base = first.u32(global_mclib, 0x38)
    declared_size = first.u32(global_mclib, 0x40)
    require(
        (
            width_start,
            bitmap_start,
            glyph_count,
            glyph_height,
            glyph_stride,
            local_base,
            declared_size,
        )
        == (0x80, 0x200, 306, 24, 24, 1, len(global_mclib)),
        "patched global mclib geometry mismatch",
    )

    unchanged_roots = 0
    for old_rel, new_rel in (
        (0x10, 0x10),
        (0x3D90, 0x3D90),
        (first.GLOBAL_OLD_DTT_WRAPPER_REL + 0x10, first.GLOBAL_NEW_DTT_WRAPPER_REL + 0x10),
    ):
        old_decoded, old_header = first.decode_inner_slz(original, old_rel)
        new_decoded, new_header = first.decode_inner_slz(patched, new_rel)
        require(old_decoded == new_decoded and old_header == new_header, f"archive 8 root 0x{old_rel:X} changed")
        unchanged_roots += 1

    compressed = compress_slz_mode2(global_mclib)
    require(len(compressed) == header["compressed"], "global mclib recompression length mismatch")
    require(
        decompress_slz_payload(compressed, 2, len(global_mclib)) == global_mclib,
        "global mclib SLZ round trip failed",
    )
    return {
        "archive_sha256": archive_sha,
        "mclib_sha256": mclib_sha,
        "slz": header,
        "glyph_count": glyph_count,
        "unchanged_other_roots": unchanged_roots,
    }


def reconstruct_first_dialogue_base(original_target: bytes) -> tuple[bytes, bytes]:
    characters = first.hangul_characters()
    require(characters == "소피아페이트봐호텔은가없어왜", "first-dialogue glyph order changed")
    code_map = dict(zip(characters, range(293, 307)))
    base_archive, base_mclib, _ = first.patch_target_archive(original_target, code_map)
    base_archive_sha = first.sha256(base_archive)
    base_mclib_sha = first.sha256(base_mclib)
    require(
        base_archive_sha == EXPECTED_BASE_TARGET_ARCHIVE_SHA256,
        f"base first-dialogue archive hash mismatch: {base_archive_sha}",
    )
    require(
        base_mclib_sha == EXPECTED_BASE_TARGET_MCLIB_SHA256,
        f"base first-dialogue mclib hash mismatch: {base_mclib_sha}",
    )
    return base_archive, base_mclib


def verify_bitmap_substitutions(base_mclib: bytes, patched_mclib: bytes) -> dict[str, object]:
    before = Mclib.parse(base_mclib)
    after = Mclib.parse(patched_mclib)
    require(len(base_mclib) == len(patched_mclib), "target mclib decoded size changed")
    require(
        (before.local_base, before.glyph_count, before.glyph_width, before.glyph_height)
        == (307, 72, 24, 24),
        "base target mclib geometry mismatch",
    )
    require(
        (after.local_base, after.glyph_count, after.glyph_width, after.glyph_height)
        == (307, 72, 24, 24),
        "patched target mclib geometry mismatch",
    )
    require(before.rows == after.rows, "message row table changed")
    require(before.segments == after.segments, "message bytecode segments changed")
    require(before.widths == after.widths, "local glyph width table changed")
    require(
        base_mclib[before.text_start : before.width_start]
        == patched_mclib[after.text_start : after.width_start],
        "message bytecode area changed",
    )

    glyph_bytes = after.glyph_stride * after.glyph_height // 2
    require(glyph_bytes == 288, "unexpected local glyph bitmap size")
    changed_indices = {row[2] for row in READING_SLOTS}
    require(len(changed_indices) == 13, "reading slot table does not contain 13 unique indices")
    allowed_offsets: set[int] = set()
    verified_slots: list[dict[str, object]] = []

    for kanji, reading, index, old_code, new_code, old_hash, new_hash in READING_SLOTS:
        require(old_code == 301 + index, f"bad original code for {kanji}")
        require(new_code == 307 + index, f"bad patched code for {kanji}")
        start = index * glyph_bytes
        end = start + glyph_bytes
        old_bitmap = before.bitmaps[start:end]
        new_bitmap = after.bitmaps[start:end]
        require(old_bitmap != new_bitmap, f"slot {index} ({kanji}) did not change")
        require(first.sha256(old_bitmap) == old_hash, f"slot {index} original bitmap hash mismatch")
        require(first.sha256(new_bitmap) == new_hash, f"slot {index} patched bitmap hash mismatch")
        absolute = after.bitmap_start + start
        allowed_offsets.update(range(absolute, absolute + glyph_bytes))
        verified_slots.append(
            {
                "kanji": kanji,
                "reading": reading,
                "local_index": index,
                "original_code": old_code,
                "patched_code": new_code,
                "width": after.widths[index],
                "old_bitmap_sha256": old_hash,
                "new_bitmap_sha256": new_hash,
            }
        )

    for index in range(after.glyph_count):
        start = index * glyph_bytes
        end = start + glyph_bytes
        if index not in changed_indices:
            require(
                before.bitmaps[start:end] == after.bitmaps[start:end],
                f"unrelated local bitmap {index} changed",
            )

    actual_offsets = {
        offset
        for offset, (old, new) in enumerate(zip(base_mclib, patched_mclib))
        if old != new
    }
    require(actual_offsets, "target mclib is unchanged")
    require(actual_offsets <= allowed_offsets, "target mclib changed outside the 13 bitmap slots")
    return {
        "changed_glyph_count": len(verified_slots),
        "glyph_bitmap_bytes": glyph_bytes,
        "changed_mclib_bytes": len(actual_offsets),
        "width_table_unchanged": True,
        "message_bytecode_unchanged": True,
        "slots": verified_slots,
    }


def verify_target_archive(
    original: bytes, patched: bytes
) -> dict[str, object]:
    require(first.sha256(original) == first.TARGET_ARCHIVE_SHA256, "original target archive hash mismatch")
    base_archive, base_mclib = reconstruct_first_dialogue_base(original)

    archive_sha = first.sha256(patched)
    require(archive_sha == EXPECTED_TARGET_ARCHIVE_SHA256, f"patched target archive hash mismatch: {archive_sha}")
    base_rows = first.parse_pk1_table(base_archive)
    patched_rows = first.parse_pk1_table(patched)
    require(patched_rows == base_rows, "PK1 table differs from the base first-dialogue archive")
    header_size = 0x10 + len(base_rows) * 16
    require(patched[:header_size] == base_archive[:header_size], "raw PK1 table bytes changed")

    unchanged_records = 0
    for index, row in enumerate(base_rows):
        _, _, size, offset = row
        if index == first.TARGET_RECORD_INDEX:
            continue
        require(
            patched[offset : offset + size] == base_archive[offset : offset + size],
            f"non-target PK1 record {index} changed",
        )
        unchanged_records += 1

    require(
        patched[first.TARGET_SECOND_PACKAGE_REL :]
        == base_archive[first.TARGET_SECOND_PACKAGE_REL :],
        "archive 1220 second PK1 package or tail changed",
    )
    first_package_end = max(offset + size for _, _, size, offset in patched_rows)
    require(first_package_end == 0xFCE98, "unexpected first PK1 package end")
    require(first.TARGET_SECOND_PACKAGE_REL - first_package_end == 360, "unexpected PK1 package gap")
    require(not any(patched[first_package_end : first.TARGET_SECOND_PACKAGE_REL]), "PK1 package gap is not zero")

    _, _, allocation, target_offset = patched_rows[first.TARGET_RECORD_INDEX]
    require((allocation, target_offset) == (0x28D0, first.TARGET_MEMBER_REL), "target PK1 allocation changed")
    patched_mclib, slz = first.decode_inner_slz(patched, target_offset)
    require(
        slz == {"mode": 2, "compressed": 9859, "unpacked": 22528, "next_rel": 0},
        f"target SLZ header mismatch: {slz}",
    )
    mclib_sha = first.sha256(patched_mclib)
    require(mclib_sha == EXPECTED_TARGET_MCLIB_SHA256, f"target mclib hash mismatch: {mclib_sha}")

    payload_start = target_offset + 16
    archived_payload = patched[payload_start : payload_start + slz["compressed"]]
    require(len(archived_payload) == slz["compressed"], "target SLZ payload is truncated")
    require(
        decompress_slz_payload(archived_payload, slz["mode"], slz["unpacked"])
        == patched_mclib,
        "target SLZ decompression round trip failed",
    )
    recompressed = compress_slz_mode2(patched_mclib)
    require(recompressed == archived_payload, "target SLZ payload is not the canonical recompression")
    used_record_size = first.align(16 + len(archived_payload), 4)
    require(used_record_size == 9876, "target SLZ used record size mismatch")
    require(
        not any(patched[target_offset + used_record_size : target_offset + allocation]),
        "target record unused allocation is not zero-filled",
    )

    bitmap_details = verify_bitmap_substitutions(base_mclib, patched_mclib)
    return {
        "archive_sha256": archive_sha,
        "mclib_sha256": mclib_sha,
        "base_first_dialogue_archive_sha256": first.sha256(base_archive),
        "base_first_dialogue_mclib_sha256": first.sha256(base_mclib),
        "pk1_row_count": len(patched_rows),
        "pk1_table_unchanged": True,
        "unchanged_non_target_records": unchanged_records,
        "second_package_unchanged": True,
        "slz": slz,
        "slz_round_trip": True,
        "record_allocation": allocation,
        "used_record_size": used_record_size,
        "unused_record_bytes": allocation - used_record_size,
        "package_gap": 360,
        "bitmap_substitutions": bitmap_details,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_iso", type=Path)
    parser.add_argument("patched_iso", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        validate_artifact_paths(args.original_iso, args.patched_iso, args.report)
    except ValueError as exc:
        parser.error(str(exc))

    require(args.original_iso.is_file(), f"original ISO does not exist: {args.original_iso}")
    require(args.patched_iso.is_file(), f"patched ISO does not exist: {args.patched_iso}")
    original_size = args.original_iso.stat().st_size
    patched_size = args.patched_iso.stat().st_size
    require(original_size == first.SUPPORTED_ISO_SIZE, f"unsupported original ISO size: {original_size}")
    require(patched_size == original_size, "patched ISO size differs from the original")
    require(read_index(args.original_iso) == read_index(args.patched_iso), "hidden archive index changed")

    original_global = first.read_exact(
        args.original_iso, first.GLOBAL_ARCHIVE_OFFSET, first.GLOBAL_ARCHIVE_SIZE
    )
    patched_global = first.read_exact(
        args.patched_iso, first.GLOBAL_ARCHIVE_OFFSET, first.GLOBAL_ARCHIVE_SIZE
    )
    original_target = first.read_exact(
        args.original_iso, first.TARGET_ARCHIVE_OFFSET, first.TARGET_ARCHIVE_SIZE
    )
    patched_target = first.read_exact(
        args.patched_iso, first.TARGET_ARCHIVE_OFFSET, first.TARGET_ARCHIVE_SIZE
    )

    result = {
        "verified": True,
        "original_iso": str(args.original_iso),
        "patched_iso": str(args.patched_iso),
        "size": patched_size,
        "hidden_archive_index_unchanged": True,
        "global_archive": verify_global_archive(original_global, patched_global),
        "target_archive": verify_target_archive(original_target, patched_target),
        "full_iso_diff": full_iso_diff(args.original_iso, args.patched_iso),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

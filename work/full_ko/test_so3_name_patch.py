#!/usr/bin/env python3
"""pytest suite for so3_name_patch (task D11-2).

Covers: converter-simulation round-trips, the mode-3 (SLZ16) codec, the
global-font rebuild invariants, the 0002.sle decrypt/patch/rebuild/re-encrypt
round-trip, the fallback-latin build, and a full end-to-end run on a COPY of
the original ISO including a negative (corrupted-table) verification test.
"""

from __future__ import annotations

import random
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import so3_name_patch as snp  # noqa: E402
from so3_repack import decompress_slz_payload  # noqa: E402

ORIG_ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")

needs_iso = pytest.mark.skipif(not ORIG_ISO.exists(), reason="original ISO missing")
needs_refs = pytest.mark.skipif(
    not (snp.REF_FONT.exists() and snp.REF_0068.exists()
         and snp.REF_1069.exists() and snp.REF_SLE.exists()),
    reason="workspace reference files missing",
)
needs_font = pytest.mark.skipif(not snp.DEFAULT_FONT.exists(), reason="Nanum font missing")


# ---------------------------------------------------------------------------
# converter simulation and code constraints
# ---------------------------------------------------------------------------

def test_code_constraints():
    info = snp.validate_code_constraints()
    assert len(info["codes"]) == 21
    assert not set(info["codes"]) & snp.FORBIDDEN_GLOBAL_CODES
    assert all(147 <= c <= 202 for c in info["codes"])
    assert 157 not in info["codes"]


def test_converter_roundtrip_all_hangul_names():
    replaced = snp.replaced_slot_map()
    for cid, name in snp.HANGUL_NAMES.items():
        raw = snp.kana_spelling(name)
        assert len(raw) <= 7, f"id {cid} does not fit 7 bytes + NUL"
        codes = snp.simulate_converter(raw.ljust(8, b"\x00"))
        assert len(codes) == len(name), f"id {cid}: composition changed length"
        decoded = "".join(snp.code_to_char(c, replaced) for c in codes)
        assert decoded == name, f"id {cid}: {decoded!r} != {name!r}"


def test_converter_roundtrip_all_latin_names():
    for cid, name in snp.LATIN_NAMES.items():
        raw = snp.latin_spelling(name)
        assert len(raw) <= 7
        codes = snp.simulate_converter(raw.ljust(8, b"\x00"))
        decoded = "".join(snp.code_to_char(c, {}) for c in codes)
        assert decoded == name, f"id {cid}: {decoded!r} != {name!r}"


def test_converter_matches_documented_rules():
    # original table entries must decode to the original katakana readings
    assert snp.simulate_converter(bytes.fromhex("ccaab2c400")) == [185, 151, 159, 177]
    # dakuten composition: ho+dakuten -> bo (0xCE + 0x11 = 0xDF = 223)
    assert snp.simulate_converter(bytes((0xCE, 0xDE, 0x00))) == [223]
    # ka+dakuten -> ga (0xB6 + 0x16 = 0xCC = 204)
    assert snp.simulate_converter(bytes((0xB6, 0xDE, 0x00))) == [204]
    # u+dakuten -> vu (203); handakuten: ha+0xDF -> pa (0xCA + 0x16 = 0xE0)
    assert snp.simulate_converter(bytes((0xB3, 0xDE, 0x00))) == [203]
    assert snp.simulate_converter(bytes((0xCA, 0xDF, 0x00))) == [224]
    # long-vowel dash and ASCII classes
    assert snp.simulate_converter(b"\xb0A z9\x00") == [157, 14, 0xF1, 65, 10]


# ---------------------------------------------------------------------------
# mode-3 codec
# ---------------------------------------------------------------------------

def test_mode3_roundtrip_synthetic():
    rng = random.Random(0x1069)
    cases = [
        b"",
        b"AB",
        b"ABAB" * 300,
        b"\x00" * 40000,
        rng.randbytes(4096),
        bytes(rng.randrange(4) for _ in range(20000)),
        b"\x00" * 66 + b"XY" + b"\x00" * 200,
        (b"WORDPAIR" + rng.randbytes(8190)) * 2,   # window-edge distances
        b"\x12\x34" * 17 + b"\x56\x78" * 3,        # max match length
    ]
    for i, data in enumerate(cases):
        if len(data) % 2:
            data += b"\x00"
        payload = snp.compress_slz_mode3_optimal(data)
        assert decompress_slz_payload(payload, 3, len(data)) == data, f"case {i}"


def test_mode3_rejects_odd_length():
    with pytest.raises(ValueError):
        snp.compress_slz_mode3_optimal(b"\x00")


def test_sle_cipher_roundtrip():
    rng = random.Random(2)
    blob = rng.randbytes(70000)
    assert snp.decrypt_sle_payload(snp.encrypt_sle_payload(blob)) == blob
    # reference vector: decrypting the real member 0 payload yields SLZ data
    if snp.REF_SLE.exists():
        sle = snp.REF_SLE.read_bytes()
        plain = snp.decrypt_sle_payload(sle[16 : 16 + 73138])
        image = decompress_slz_payload(plain, 3, 164480)
        assert snp.sha256(image) == snp.SHA_SLE_M0


# ---------------------------------------------------------------------------
# global font rebuild
# ---------------------------------------------------------------------------

@needs_refs
@needs_font
def test_font_rebuild_invariants():
    original = snp.REF_FONT.read_bytes()
    glyphs = snp.render_hangul_glyphs(snp.DEFAULT_FONT)
    assert len(glyphs) == 21
    patched = snp.build_font_image(original, glyphs)
    assert len(patched) == len(original)
    assert patched[:0x80] == original[:0x80], "header changed"

    changed_widths = [s for s in range(292) if patched[0x80 + s] != original[0x80 + s]]
    changed_bitmaps = [
        s for s in range(292)
        if patched[0x200 + s * 288 : 0x200 + (s + 1) * 288]
        != original[0x200 + s * 288 : 0x200 + (s + 1) * 288]
    ]
    assert set(changed_bitmaps) == set(glyphs), "wrong bitmap slots changed"
    assert set(changed_widths) <= set(glyphs), "a width outside the plan changed"
    for slot, (_syl, advance, bitmap) in glyphs.items():
        assert 1 <= advance <= 24
        assert patched[0x80 + slot] == advance
        assert patched[0x200 + slot * 288 : 0x200 + (slot + 1) * 288] == bitmap
        # 2 gray levels only
        assert {n for b in bitmap for n in (b & 15, b >> 4)} <= {0, 15}

    payload = snp.compress_slz_mode2_optimal(patched)
    assert decompress_slz_payload(payload, 2, len(patched)) == patched
    allocation = snp.FONT_MEMBER["boundary"] - snp.FONT_MEMBER["iso_offset"] - 16
    assert len(payload) <= allocation, (len(payload), allocation)


# ---------------------------------------------------------------------------
# table image builders
# ---------------------------------------------------------------------------

@needs_refs
def test_table_images_0068_and_1069():
    rows = snp.name_rows(False)
    replaced = snp.replaced_slot_map()

    img68 = snp.REF_0068.read_bytes()
    patched = snp.build_table_image(img68, snp.T0068_OFFSET, "forward", rows,
                                    snp._ORIG_ROWS)
    diff = snp._diff_outside(patched, img68,
                             [(snp.T0068_OFFSET, snp.T0068_OFFSET + 80)])
    assert diff == 0
    decoded = snp.decode_table(patched, snp.T0068_OFFSET, "forward", replaced)
    assert decoded == snp.HANGUL_NAMES

    img69 = snp.REF_1069.read_bytes()
    # the original pool must show the id9 dev leftover (izaaku), which we fix
    raw9 = img69[snp.T1069_OFFSET + 8 : snp.T1069_OFFSET + 16]
    assert raw9 == snp._ORIG_ROWS_1069[9].ljust(8, b"\x00")
    patched = snp.build_table_image(img69, snp.T1069_OFFSET, "reverse", rows,
                                    snp._ORIG_ROWS_1069)
    diff = snp._diff_outside(patched, img69,
                             [(snp.T1069_OFFSET, snp.T1069_OFFSET + 80)])
    assert diff == 0
    decoded = snp.decode_table(patched, snp.T1069_OFFSET, "reverse", replaced)
    assert decoded == snp.HANGUL_NAMES


@needs_refs
def test_table_image_rejects_wrong_original():
    rows = snp.name_rows(False)
    img68 = bytearray(snp.REF_0068.read_bytes())
    img68[snp.T0068_OFFSET] ^= 0xFF
    with pytest.raises(snp.NamePatchError):
        snp.build_table_image(bytes(img68), snp.T0068_OFFSET, "forward", rows,
                              snp._ORIG_ROWS)


# ---------------------------------------------------------------------------
# 0002.sle rebuild round-trip (decrypt -> patch -> rebuild -> re-encrypt)
# ---------------------------------------------------------------------------

@needs_iso
@needs_refs
@needs_font
def test_build_plans_fit_and_sle_roundtrip():
    with ORIG_ISO.open("rb") as handle:
        plans = snp.build_plans(handle, False, snp.DEFAULT_FONT)
    by_name = {p.name: p for p in plans}
    assert set(by_name) == {
        "font_global_1_72", "s000061_archive_68", "s001679_archive_1069", "0002_sle",
    }
    for plan in plans:
        assert plan.new_compressed <= plan.allocation, plan.fit_report()

    sle_plan = by_name["0002_sle"]
    new_sle = sle_plan.write_bytes
    orig_sle = snp.REF_SLE.read_bytes()
    assert len(new_sle) == len(orig_sle) == snp.SLE_EXTENT["size"]
    # member 0 (header + payload) byte-identical
    assert new_sle[:73156] == orig_sle[:73156]
    # decrypt(re-encrypted) == modified plaintext == decompressible image
    members = snp.parse_sle(new_sle)
    m1 = members[1]
    assert (m1["mode"], m1["unpacked"], m1["next_rel"]) == (3, 1186048, 0)
    plain = snp.decrypt_sle_payload(
        new_sle[m1["offset"] + 16 : m1["offset"] + 16 + m1["compressed"]]
    )
    image = decompress_slz_payload(plain, 3, m1["unpacked"])
    assert image == sle_plan.image
    # decompressed image: identical outside the table + the two literals
    ref_m1 = decompress_slz_payload(
        snp.decrypt_sle_payload(orig_sle[73172 : 73172 + 571646]), 3, 1186048
    )
    spans = [(snp.SLE_TABLE_OFFSET, snp.SLE_TABLE_OFFSET + 80)]
    spans += [(off, off + 8) for off in snp.SLE_LITERALS.values()]
    assert snp._diff_outside(image, ref_m1, spans) == 0
    # tables decode to the exact Hangul strings
    replaced = snp.replaced_slot_map()
    assert snp.decode_table(image, snp.SLE_TABLE_OFFSET, "reverse", replaced) \
        == snp.HANGUL_NAMES


@needs_iso
@needs_refs
def test_build_plans_fallback_latin():
    with ORIG_ISO.open("rb") as handle:
        plans = snp.build_plans(handle, True, snp.DEFAULT_FONT)
    names = {p.name for p in plans}
    assert "font_global_1_72" not in names, "fallback must not touch the font"
    by_name = {p.name: p for p in plans}
    for plan in plans:
        assert plan.new_compressed <= plan.allocation, plan.fit_report()
    img = by_name["s000061_archive_68"].image
    assert snp.decode_table(img, snp.T0068_OFFSET, "forward", {}) == snp.LATIN_NAMES


# ---------------------------------------------------------------------------
# end-to-end on a copy of the original ISO
# ---------------------------------------------------------------------------

def _masked_extents() -> list[tuple[int, int]]:
    spans = [
        (snp.FONT_MEMBER["iso_offset"], snp.FONT_MEMBER["boundary"]),
        (snp.S0068_MEMBER["iso_offset"], snp.S0068_MEMBER["boundary"]),
        (snp.S1069_MEMBER["iso_offset"], snp.S1069_MEMBER["boundary"]),
        (snp.SLE_EXTENT["iso_offset"],
         snp.SLE_EXTENT["iso_offset"] + snp.SLE_EXTENT["size"]),
    ]
    return sorted(spans)


def _compare_outside_extents(a: Path, b: Path, spans: list[tuple[int, int]],
                             chunk: int = 16 * 1024 * 1024) -> int:
    diffs = 0
    with a.open("rb") as fa, b.open("rb") as fb:
        offset = 0
        while True:
            ba = fa.read(chunk)
            bb = fb.read(chunk)
            assert len(ba) == len(bb)
            if not ba:
                break
            if ba != bb:
                ma, mb = bytearray(ba), bytearray(bb)
                for start, end in spans:
                    lo = max(start - offset, 0)
                    hi = min(end - offset, len(ma))
                    if lo < hi:
                        ma[lo:hi] = mb[lo:hi]
                diffs += sum(1 for x, y in zip(ma, mb) if x != y)
            offset += len(ba)
    return diffs


@needs_iso
@needs_refs
@needs_font
def test_end_to_end_on_iso_copy():
    work = Path(tempfile.gettempdir()) / "so3_name_patch_e2e.iso"
    report_path = Path(tempfile.gettempdir()) / "so3_name_patch_e2e_report.json"
    shutil.copyfile(ORIG_ISO, work)
    try:
        report = snp.apply_name_patch(work, work, report_path)
        assert report["plan"] == "kana_slot_repurpose"
        assert all(m["fits"] for m in report["members"])
        assert report["verification"]["ok"] is True

        # independent verification pass on the patched copy
        verification = snp.verify_name_patch(work)
        assert verification["ok"] is True
        for label, entry in verification["tables"].items():
            assert entry["decoded"] == snp.HANGUL_NAMES, label
            assert entry["outside_diff"] == 0, label

        # nothing outside the four extents may differ from the original
        assert _compare_outside_extents(ORIG_ISO, work, _masked_extents()) == 0

        # negative test: corrupt one byte of the 0068 table (as a valid SLZ
        # member, so only the semantic verification can catch it)
        with work.open("rb") as handle:
            image, _, next_rel = snp._extract_member(
                handle, {**snp.S0068_MEMBER, "mode": 2}
            )
        broken = bytearray(image)
        broken[snp.T0068_OFFSET] ^= 0x01
        payload = snp.compress_slz_mode2_optimal(bytes(broken))
        header = snp._slz_header(b"SLZ", 2, len(payload), len(broken), next_rel)
        with work.open("r+b") as handle:
            handle.seek(snp.S0068_MEMBER["iso_offset"])
            handle.write(header + payload)
        with pytest.raises(snp.NamePatchError):
            snp.verify_name_patch(work)

        # applying on an already-patched ISO must fail closed
        with pytest.raises(snp.NamePatchError):
            snp.apply_name_patch(work, work)
    finally:
        work.unlink(missing_ok=True)

#!/usr/bin/env python3
"""Acceptance tests for so3_full_patch (T1 identity, T2 Hyda regression,
T3 Hangul capacity stress).  Run with pytest from work/full_ko.

T2 builds a real ISO on D:\\ps2 (TEST_*.iso, deleted after verification) and
invokes the shipped independent verifier as a subprocess.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import so3_full_patch as fp
from so3_repack import Mclib, decompress_slz_payload, read_index

WS = fp.WS
ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
FONT = fp.DEFAULT_FONT_PATH
OUT_DIR = HERE / "patcher_test_out"
OUT_DIR.mkdir(exist_ok=True)

pytestmark = pytest.mark.skipif(not ISO.exists(), reason="original ISO not present")

TABLE = fp.default_control_table()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def read_stream_from_iso(row: dict[str, str]) -> bytes:
    iso_offset = int(row["iso_offset"])
    mode = int(row["mode"])
    comp = int(row["compressed"])
    unpacked = int(row["unpacked"])
    with ISO.open("rb") as handle:
        handle.seek(iso_offset + 16)
        payload = handle.read(comp)
    return decompress_slz_payload(payload, mode, unpacked)


def expected_identity_bytes(plan: fp.SegmentPlan) -> bytes:
    """Original bytes minus dropped tokens (ruby 9080/9180, name 9380)."""
    body = bytearray()
    for p_i, lines in enumerate(plan.pages):
        if p_i:
            body += b"\x81\x80"
        for l_i, line in enumerate(lines):
            if l_i:
                body += b"\x80\x80"
            for token in line.tokens:
                if token.kind == "control" and token.fam in fp.DEFAULT_DROP:
                    continue
                body += token.raw
    return plan.prefix_raw + plan.speaker_field_raw + plan.delim_raw + bytes(body) + b"\0"


def control_signature(seg: bytes) -> tuple[str, ...]:
    tokens, _ = fp.tokenize_segment(seg, TABLE)
    return tuple(t.raw.hex() for t in tokens
                 if t.kind == "control" and t.fam not in fp.DEFAULT_DROP)


def identity_translations(parsed: Mclib) -> tuple[dict[int, dict], dict[str, int]]:
    """PUA translations for every uniquely-addressable message."""
    code_to_char, char_to_code = fp.identity_code_maps(parsed)
    counts = defaultdict(int)
    for mid, _ in parsed.rows:
        counts[mid] += 1
    translations: dict[int, dict] = {}
    for mid, offset in parsed.rows:
        if counts[mid] != 1:
            continue
        seg = parsed.segments[offset]
        plan = fp.analyze_segment(seg, TABLE)
        body, speaker, unknown = fp.struct_to_text(plan, code_to_char)
        keep = (plan.speaker_field_tokens is not None
                and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
        # PUA maps cover every glyph code; the only tolerated unknowns are
        # non-glyph speaker-field tokens, which are kept verbatim.
        assert unknown == 0 or keep, f"PUA map must cover all codes (msg {mid})"
        assert "〓" not in body, f"PUA body decode incomplete (msg {mid})"
        translations[mid] = {
            "korean": body,
            "speaker_korean": None if keep else speaker,
            "keep_speaker": keep,
        }
    return translations, char_to_code


HANGUL_500 = [chr(0xAC00 + 28 * i) for i in range(399)] + \
             [chr(0xAC00 + 28 * i + 4) for i in range(101)]


def pseudo_hangul(ch: str) -> str:
    digest = hashlib.md5(ch.encode("utf-8")).digest()
    return HANGUL_500[int.from_bytes(digest[:4], "little") % len(HANGUL_500)]


def pseudo_translate_text(text: str) -> str:
    """JP chars -> deterministic Hangul; markers/newlines preserved;
    non-global exotic chars also -> Hangul (renderability)."""
    out: list[str] = []
    idx = 0
    for match in fp.MARKER_RE.finditer(text):
        out.append(_pseudo_chars(text[idx:match.start()]))
        out.append(match.group(0))
        idx = match.end()
    out.append(_pseudo_chars(text[idx:]))
    return "".join(out)


def _pseudo_chars(chunk: str) -> str:
    result = []
    for ch in chunk:
        if ch == "\n" or ch in fp.GLOBAL_CODE_MAP:
            result.append(ch)
        elif fp._JP_RE.match(ch) or not (" " <= ch <= "~"):
            result.append(pseudo_hangul(ch))
        else:
            result.append(ch)
    return "".join(result)


# ---------------------------------------------------------------------------
# T1 container selection
# ---------------------------------------------------------------------------

REQUIRED_STREAMS = [
    (77, 70), (85, 157),                     # menu roots
    (3454, 60027), (3454, 60028), (3454, 60029), (3454, 60030),  # battle DB
    (1775, 28069), (6068, 61963),            # IC / item flat
    (38, 19), (38, 20),                      # system (incl. local_base 1)
    (1204, 5829), (1245, 7017), (1222, 6258),  # hyda event banks
]
FEATURE_FAMS = (0x81, 0x88, 0x92, 0x9C, 0xA3, 0xA1)


def _select_t1_containers() -> list[dict]:
    cache = OUT_DIR / "t1_selection.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    rows = []
    seen = set()
    with fp.CONTAINER_CATALOG_PATH.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["file_sha256"] in seen:
                continue
            seen.add(row["file_sha256"])
            if row["glyph_width"] != "24":
                continue
            rows.append(row)
    by_stream = {(int(r["archive_id"]), int(r["stream_id"])): r for r in rows}

    def features_of(row: dict) -> tuple[set[int], bool, int]:
        data = Path(row["path"]).read_bytes()
        parsed = Mclib.parse(data)
        fams: set[int] = set()
        speaker = False
        for _, offset in parsed.rows:
            try:
                plan = fp.analyze_segment(parsed.segments[offset], TABLE)
            except fp.SegmentError:
                continue
            if plan.speaker_field_tokens is not None:
                speaker = True
            if len(plan.pages) > 1:
                fams.add(0x81)  # page separator (consumed by the splitter)
            for lines in plan.pages:
                if len(lines) > 1:
                    fams.add(0x80)
                for line in lines:
                    for token in line.tokens:
                        if token.kind == "control":
                            fams.add(token.fam)
        return fams, speaker, parsed.glyph_count

    selected: list[dict] = []
    have_shas: set[str] = set()

    def add(row: dict, why: str) -> None:
        if row["file_sha256"] in have_shas:
            return
        have_shas.add(row["file_sha256"])
        selected.append({
            "archive_id": int(row["archive_id"]),
            "stream_id": int(row["stream_id"]),
            "path": row["path"],
            "file_sha256": row["file_sha256"],
            "glyph_count": int(row["glyph_count"]),
            "why": why,
        })

    for key in REQUIRED_STREAMS:
        assert key in by_stream, f"required stream {key} missing from catalog"
        add(by_stream[key], "required")

    need = set(FEATURE_FAMS)
    glyph0 = 0
    for row in rows:
        if not need and glyph0 >= 2:
            break
        interesting = False
        if int(row["glyph_count"]) == 0 and glyph0 < 2 and int(row["valid_messages"]) > 0:
            interesting = True
        if need and int(row["valid_messages"]) > 0:
            interesting = True
        if not interesting:
            continue
        fams, speaker, gcount = features_of(row)
        hit = need & fams
        if hit:
            add(row, "features:" + ",".join(f"{f:02x}" for f in sorted(hit)))
            need -= hit
        elif gcount == 0 and glyph0 < 2:
            add(row, "glyph0")
            glyph0 += 1
    # fill to >= 32 with an even spread
    step = max(1, len(rows) // 40)
    for row in rows[::step]:
        if len(selected) >= 34:
            break
        if int(row["valid_messages"]) > 0:
            add(row, "spread")
    cache.write_text(json.dumps(selected, ensure_ascii=False, indent=1), encoding="utf-8")
    return selected


# ---------------------------------------------------------------------------
# T1: identity re-encode
# ---------------------------------------------------------------------------

def test_t1_identity_pua_byte_strict():
    """Full container rebuild with PUA identity text: every re-encoded message
    must be byte-identical to the original minus dropped tokens."""
    containers = _select_t1_containers()
    assert len(containers) >= 30, f"only {len(containers)} T1 containers selected"
    manifest = fp.load_stream_manifest()
    report = []
    total_msgs = 0
    features_seen: set[int] = set()
    for item in containers:
        data = Path(item["path"]).read_bytes()
        parsed = Mclib.parse(data)
        translations, char_to_code = identity_translations(parsed)
        assert translations, f"{item['archive_id']}:{item['stream_id']} has no usable messages"
        rebuilt, crep = fp.rebuild_container(
            data, translations, table=TABLE, identity_code_map=char_to_code)
        checked = Mclib.parse(rebuilt)
        remap = {old: new for (mid, old), (mid2, new) in zip(parsed.rows, checked.rows)}
        for mid, old_offset in parsed.rows:
            if mid not in translations:
                continue
            plan = fp.analyze_segment(parsed.segments[old_offset], TABLE)
            if len(plan.pages) > 1:
                features_seen.add(0x81)
            for lines in plan.pages:
                for line in lines:
                    for token in line.tokens:
                        if token.kind == "control":
                            features_seen.add(token.fam)
            expected = expected_identity_bytes(plan)
            got = checked.segments[remap[old_offset]]
            assert fp.logical_segment(got) == fp.logical_segment(expected), \
                f"{item['archive_id']}:{item['stream_id']} msg {mid} identity mismatch"
        total_msgs += len(translations)
        # full SLZ round-trip + fit report
        comp = fp.compress_checked(rebuilt)
        old_comp = None
        key = (item["archive_id"], item["stream_id"])
        if key in manifest:
            old_comp = int(manifest[key]["compressed"])
        report.append({
            "archive": item["archive_id"], "stream": item["stream_id"],
            "messages": len(translations),
            "old_compressed": old_comp, "new_compressed": len(comp),
            "delta": None if old_comp is None else len(comp) - old_comp,
            "why": item["why"],
        })
    (OUT_DIR / "t1_identity_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    # required feature coverage across the suite
    for fam in (0x81, 0x88):
        assert fam in features_seen, f"feature {fam:02x} not covered by T1 set"
    assert total_msgs > 3000, f"T1 exercised only {total_msgs} messages"


def test_t1_identity_realtext():
    """Real-text identity: decode with the container's own atlas mapping,
    re-encode, and require decode-back equality + control signatures."""
    containers = _select_t1_containers()
    bitmap_map = fp.load_bitmap_unicode_map()
    total = skipped = 0
    for item in containers:
        data = Path(item["path"]).read_bytes()
        parsed = Mclib.parse(data)
        code_to_char = fp.container_code_to_char(parsed, bitmap_map)
        char_to_code: dict[str, int] = {}
        for code, ch in sorted(code_to_char.items()):
            char_to_code.setdefault(ch, code)
        counts = defaultdict(int)
        for mid, _ in parsed.rows:
            counts[mid] += 1
        translations: dict[int, dict] = {}
        originals: dict[int, bytes] = {}
        for mid, offset in parsed.rows:
            if counts[mid] != 1:
                continue
            seg = parsed.segments[offset]
            try:
                plan = fp.analyze_segment(seg, TABLE)
            except fp.SegmentError:
                skipped += 1
                continue
            body, speaker, unknown = fp.struct_to_text(plan, code_to_char)
            keep = (plan.speaker_field_tokens is not None
                    and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
            if unknown and not keep:
                skipped += 1
                continue
            if "〓" in body or (speaker is not None and not keep and "〓" in speaker):
                skipped += 1
                continue
            translations[mid] = {
                "korean": body,
                "speaker_korean": None if keep else speaker,
                "keep_speaker": keep,
            }
            originals[mid] = seg
        if not translations:
            continue
        rebuilt, crep = fp.rebuild_container(
            data, translations, table=TABLE, identity_code_map=char_to_code)
        checked = Mclib.parse(rebuilt)
        remap = dict(checked.rows)
        offsets = dict(parsed.rows)
        for mid in translations:
            new_seg = checked.segments[remap[mid]]
            assert control_signature(originals[mid]) == control_signature(new_seg), \
                f"{item['archive_id']}:{item['stream_id']} msg {mid} signature changed"
        total += len(translations)
    assert total > 2000, f"real-text identity exercised only {total} messages"


def test_codec_error_paths():
    """Marker/structure violations must be hard errors."""
    data = Path(_select_t1_containers()[0]["path"]).read_bytes()
    parsed = Mclib.parse(data)
    code_to_char, char_to_code = fp.identity_code_maps(parsed)
    mid, offset = next((m, o) for m, o in parsed.rows)
    seg = parsed.segments[offset]
    plan = fp.analyze_segment(seg, TABLE)
    body, speaker, _ = fp.struct_to_text(plan, code_to_char)
    keep = (plan.speaker_field_tokens is not None
            and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
    kwargs = dict(speaker_korean=None if keep else speaker, keep_speaker=keep)
    # spurious positional marker
    with pytest.raises(fp.SegmentError):
        fp.encode_translated_segment(
            seg, body + f"⟦{len(plan.positional_tokens) + 1}⟧", char_to_code, TABLE, **kwargs)
    # extra page
    with pytest.raises(fp.SegmentError):
        fp.encode_translated_segment(
            seg, body + "⟦P⟧x" * (len(plan.pages) + 1), char_to_code, TABLE, **kwargs)
    # unknown character
    with pytest.raises(fp.SegmentError):
        fp.encode_translated_segment(seg, body + "￿", char_to_code, TABLE, **kwargs)
    # unbalanced bracket
    with pytest.raises(fp.SegmentError):
        fp.encode_translated_segment(seg, body + "⟦", char_to_code, TABLE, **kwargs)


# ---------------------------------------------------------------------------
# forced strategy-B / FAIL coverage (chain next_rel rewrite, PACK offsets)
# ---------------------------------------------------------------------------

def _padded_compressor(target_payload: int):
    """Valid mode-2 stream padded to an exact payload size with trailing bytes
    the decoder never reads; forces controlled growth to exercise reflow
    paths (test-only)."""
    def compress(data: bytes) -> bytes:
        comp = fp.compress_checked(data)
        assert len(comp) <= target_payload, "forced test target too small"
        return comp + b"\0" * (target_payload - len(comp))
    return compress


def _read_archive(archive_id: int) -> bytes:
    index = read_index(ISO)
    start = index[archive_id] * fp.SECTOR
    size = index[0x1800 + archive_id] * fp.SECTOR
    with ISO.open("rb") as handle:
        handle.seek(start)
        return handle.read(size)


def _identity_targets_for(data: bytes, address: tuple[int, int, int]) -> tuple[dict, dict]:
    packages = fp.parse_archive_layout(data)
    p, r, c = address
    member = packages[p].rows[r].members[c]
    decoded = decompress_slz_payload(
        data[member.offset + 16:member.offset + 16 + member.comp],
        member.mode, member.unpacked)
    parsed = Mclib.parse(decoded)
    translations, char_to_code = identity_translations(parsed)
    return translations, char_to_code


def test_forced_chain_reflow_archive38():
    """Top-level SLZ chain: grow member 0 -> members shift, next_rel rewritten."""
    data = _read_archive(38)
    translations, char_to_code = _identity_targets_for(data, (0, 0, 0))
    packages = fp.parse_archive_layout(data)
    member = packages[0].rows[0].members[0]
    gap = packages[0].boundary - packages[0].extent
    assert gap > 200, "archive 38 tail gap unexpectedly small"
    new_data, report = fp.rebuild_archive(
        data, {(0, 0, 0): translations}, table=TABLE,
        identity_code_map=char_to_code, archive_id=38,
        compressor=_padded_compressor(member.span - 16 + 8))
    assert report["packages"][0]["strategy"] == "B"
    new_packages = fp.parse_archive_layout(new_data)
    old_members = packages[0].rows[0].members
    new_members = new_packages[0].rows[0].members
    assert len(new_members) == len(old_members)
    assert new_members[0].next_rel != old_members[0].next_rel
    for c in (1, 2):
        old_m, new_m = old_members[c], new_members[c]
        old_end = old_m.offset + (old_m.span if c != 2 else 16 + old_m.comp)
        new_end = new_m.offset + (new_m.span if c != 2 else 16 + new_m.comp)
        assert data[old_m.offset:old_end] == new_data[new_m.offset:new_end]


def test_forced_pack_reflow_archive3454():
    """PACK table: grow record 1 -> later records shift, offsets rewritten."""
    data = _read_archive(3454)
    packages = fp.parse_archive_layout(data)
    gap = packages[0].boundary - packages[0].extent
    translations, char_to_code = _identity_targets_for(data, (0, 1, 0))
    member = packages[0].rows[1].members[0]
    row_alloc = packages[0].rows[1].end - member.offset
    if gap < 0x100:
        pytest.skip(f"archive 3454 tail gap {gap} too small to force B")
    new_data, report = fp.rebuild_archive(
        data, {(0, 1, 0): translations}, table=TABLE,
        identity_code_map=char_to_code, archive_id=3454,
        compressor=_padded_compressor(row_alloc - 16 + 8))
    assert report["packages"][0]["strategy"] == "B"
    new_packages = fp.parse_archive_layout(new_data)
    old_rows = packages[0].rows
    new_rows = new_packages[0].rows
    assert new_rows[2].offset > old_rows[2].offset
    assert [row.aux for row in new_rows] == [row.aux for row in old_rows]
    for r in range(2, len(old_rows)):
        old_c = data[old_rows[r].offset:old_rows[r].end]
        new_c = new_data[new_rows[r].offset:new_rows[r].end]
        assert new_c[:len(old_c)] == old_c, f"pack record {r} content changed"


def test_forced_multi_target_joint_reflow():
    """Two records of the SAME PK1 package resized simultaneously; the
    internal verifier proves non-targets are byte-identical but shifted."""
    data = _read_archive(1204)
    packages = fp.parse_archive_layout(data)
    picks = []
    for r in (1, 2):
        member = packages[0].rows[r].members[0]
        decoded = decompress_slz_payload(
            data[member.offset + 16:member.offset + 16 + member.comp],
            member.mode, member.unpacked)
        picks.append((r, member, Mclib.parse(decoded)))
    targets = {}
    for r, member, parsed in picks:
        translations, _ = identity_translations(parsed)
        targets[(0, r, 0)] = translations
    # one shared PUA map covers both containers (bijective over all codes)
    big = max((parsed for _, _, parsed in picks),
              key=lambda m: m.local_base + m.glyph_count)
    _, char_to_code = fp.identity_code_maps(big)
    sizes = [packages[0].rows[r].size - 16 + 8 for r, _, _ in picks]
    calls = {"n": 0}

    def compress(blob: bytes) -> bytes:
        target = sizes[calls["n"] % len(sizes)]
        calls["n"] += 1
        comp = fp.compress_checked(blob)
        assert len(comp) <= target
        return comp + b"\0" * (target - len(comp))

    new_data, report = fp.rebuild_archive(
        data, targets, table=TABLE, identity_code_map=char_to_code,
        archive_id=1204, compressor=compress)
    assert [p["strategy"] for p in report["packages"]] == ["B"]
    assert len(report["targets"]) == 2


def test_forced_fit_failure_diagnostics():
    """Overflowing the package gap must fail closed with precise diagnostics."""
    data = _read_archive(1245)
    translations, char_to_code = _identity_targets_for(data, (0, 1, 0))
    with pytest.raises(fp.FitError) as excinfo:
        fp.rebuild_archive(
            data, {(0, 1, 0): translations}, table=TABLE,
            identity_code_map=char_to_code, archive_id=1245,
            compressor=_padded_compressor(2 << 20))
    info = excinfo.value.info
    assert info["archive"] == 1245 and info["package"] == 0
    assert isinstance(info["needed"], int) and isinstance(info["available"], int)
    assert info["needed"] > info["available"]


def test_glyph0_section_creation():
    """glyph_count==0 container: real Hangul translation must create the
    width/bitmap sections and keep local_base."""
    bitmap_map = fp.load_bitmap_unicode_map()
    picked = None
    for item in _select_t1_containers():
        if item["glyph_count"] == 0:
            picked = item
            break
    assert picked is not None
    data = Path(picked["path"]).read_bytes()
    parsed = Mclib.parse(data)
    code_to_char = fp.container_code_to_char(parsed, bitmap_map)
    counts = defaultdict(int)
    for mid, _ in parsed.rows:
        counts[mid] += 1
    translations = {}
    for mid, offset in parsed.rows:
        if counts[mid] != 1:
            continue
        plan = fp.analyze_segment(parsed.segments[offset], TABLE)
        body, speaker, unknown = fp.struct_to_text(plan, code_to_char)
        if unknown or "〓" in body:
            continue
        keep = (plan.speaker_field_tokens is not None
                and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
        translations[mid] = {
            "korean": pseudo_translate_text(body),
            "speaker_korean": None if keep or speaker is None else pseudo_translate_text(speaker),
            "keep_speaker": keep,
        }
    assert translations, "glyph0 container had no translatable messages"
    rebuilt, report = fp.rebuild_container(
        data, translations, table=TABLE, font_path=FONT)
    checked = Mclib.parse(rebuilt)
    assert parsed.glyph_count == 0
    assert checked.glyph_count == report["appended_local_glyphs"] > 0
    assert checked.local_base == parsed.local_base
    assert len(checked.widths) == checked.glyph_count
    comp = fp.compress_checked(rebuilt)
    assert decompress_slz_payload(comp, 2, len(rebuilt)) == rebuilt


def test_nested_stream_rejected():
    manifest = fp.load_stream_manifest()
    nested_key = next(key for key, row in manifest.items() if int(row["depth"]) > 0)
    with pytest.raises(fp.PatchError) as excinfo:
        fp.build_plan(ISO, {nested_key: {0: {"korean": "x"}}}, manifest)
    assert "nested" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⟦G:sha8⟧ preserve-bitmap tokens + variant speaker pattern
# ---------------------------------------------------------------------------

def test_g_token_preserve_bitmaps():
    """(38,20) base-1 container: unlabeled special glyphs arrive as ⟦G:sha8⟧
    tokens; the rebuilt atlas must keep their bitmaps + advance widths, and
    every message must decode back to the same token sequence."""
    manifest = fp.load_stream_manifest()
    bitmap_map = fp.load_bitmap_unicode_map()
    row = manifest[(38, 20)]
    data = read_stream_from_iso(row)
    parsed = Mclib.parse(data)
    glyph_bytes = parsed.glyph_stride * parsed.glyph_height // 2
    code_to_char = fp.container_code_to_char(parsed, bitmap_map, unlabeled_g_tokens=True)

    translations: dict[int, dict] = {}
    g_tokens_seen: set[str] = set()
    for mid, offset in parsed.rows:
        seg = parsed.segments[offset]
        plan = fp.analyze_segment(seg, TABLE)
        body, speaker, unknown = fp.struct_to_text(plan, code_to_char)
        assert unknown == 0 and "〓" not in body, f"msg {mid} still has unknown glyphs"
        g_tokens_seen.update(m.lower() for m in fp.G_TOKEN_RE.findall(body))
        keep = (plan.speaker_field_tokens is not None
                and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
        translations[mid] = {
            "korean": pseudo_translate_text(body),
            "speaker_korean": None if keep or speaker is None else pseudo_translate_text(speaker),
            "keep_speaker": keep,
        }
    assert g_tokens_seen, "expected ⟦G:⟧ tokens in (38,20)"

    rebuilt, report = fp.rebuild_container(data, translations, table=TABLE, font_path=FONT)
    assert report["preserved_bitmap_glyphs"] == len(g_tokens_seen)
    checked = Mclib.parse(rebuilt)

    # resolve each token against the ORIGINAL atlas and verify preservation
    sha_to_index: dict[str, int] = {}
    for index in range(parsed.glyph_count):
        digest = hashlib.sha256(
            parsed.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]).hexdigest()
        sha_to_index.setdefault(digest[:8], index)
    referenced_codes: set[int] = set()
    for sha8 in sorted(g_tokens_seen):
        index = sha_to_index[sha8]
        assert (checked.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]
                == parsed.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]), \
            f"bitmap for ⟦G:{sha8}⟧ changed"
        assert checked.widths[index] == parsed.widths[index], f"width for ⟦G:{sha8}⟧ changed"
        referenced_codes.add(parsed.local_base + index)

    # decode-back with token map: token sequences must match the translations
    decode_map = dict(fp.GLOBAL_CODE_TO_CHAR) if parsed.local_base != 1 else {}
    inverse: dict[int, str] = {}
    for index in range(checked.glyph_count):
        digest = hashlib.sha256(
            checked.bitmaps[index * glyph_bytes:(index + 1) * glyph_bytes]).hexdigest()
        inverse[checked.local_base + index] = digest
    remap = dict(checked.rows)
    used_codes: set[int] = set()
    for mid, entry in translations.items():
        new_plan = fp.analyze_segment(checked.segments[remap[mid]], TABLE)
        for lines in new_plan.pages:
            for line in lines:
                for unit in line.units:
                    if unit.kind == "glyph":
                        used_codes.add(unit.code)
    assert referenced_codes <= used_codes, "preserved bitmap codes not referenced by output"
    # SLZ round-trip
    comp = fp.compress_checked(rebuilt)
    assert decompress_slz_payload(comp, 2, len(rebuilt)) == rebuilt

    # fail-closed: unknown prefix and ambiguity guard
    bad = dict(translations)
    first = next(iter(bad))
    bad[first] = {**bad[first], "korean": bad[first]["korean"] + "⟦G:00000000⟧"}
    if "00000000" not in sha_to_index:
        with pytest.raises(fp.ContainerError):
            fp.rebuild_container(data, bad, table=TABLE, font_path=FONT)


VARIANT_SPEAKER_MESSAGES = [(1070, 1722, 35), (1070, 1727, 35), (1921, 31233, 7)]


def test_variant_speaker_pattern_as_body_markers():
    """Inventory's 8780-less speaker pattern (8880+9380+8980 at line start):
    delivered as body markers with the literalized name between ⟦1⟧ and ⟦2⟧,
    no speaker_korean field."""
    manifest = fp.load_stream_manifest()
    verified = 0
    for archive_id, stream_id, mid in VARIANT_SPEAKER_MESSAGES:
        row = manifest[(archive_id, stream_id)]
        data = read_stream_from_iso(row)
        parsed = Mclib.parse(data)
        seg = parsed.segments[dict(parsed.rows)[mid]]
        plan = fp.analyze_segment(seg, TABLE)
        assert plan.speaker_field_tokens is None, "pattern must not be a speaker construct"
        line0 = plan.pages[0][0].tokens
        assert [t.fam for t in line0[:3]] == [0x88, 0x93, 0x89]

        code_to_char, char_to_code = fp.identity_code_maps(parsed)
        body, speaker, _ = fp.struct_to_text(plan, code_to_char)
        assert speaker is None
        assert body.startswith("⟦1⟧⟦2⟧"), body[:20]
        # literalize the name between the colour markers with existing glyphs
        name = code_to_char[parsed.local_base] + code_to_char[parsed.local_base + 1]
        korean = body.replace("⟦1⟧⟦2⟧", f"⟦1⟧{name}⟦2⟧", 1)
        out = fp.encode_translated_segment(seg, korean, char_to_code, TABLE)
        # 8880 raw + name glyph codes + 8980 raw at the start of the body
        expected_head = (line0[0].raw
                         + fp.encode_glyph_code(parsed.local_base)
                         + fp.encode_glyph_code(parsed.local_base + 1)
                         + line0[2].raw)
        assert out.startswith(expected_head), out[:16].hex()
        # decode-back reproduces the marked-up translation
        new_plan = fp.analyze_segment(out, TABLE)
        got, _, _ = fp.struct_to_text(new_plan, code_to_char)
        assert fp.parse_translated_text(got) == fp.parse_translated_text(korean)
        # fail-closed: supplying speaker_korean must error
        with pytest.raises(fp.SegmentError):
            fp.encode_translated_segment(seg, korean, char_to_code, TABLE,
                                         speaker_korean=name)
        verified += 1
    assert verified == 3


# ---------------------------------------------------------------------------
# T2: Hyda regression through the new pipeline
# ---------------------------------------------------------------------------

def test_t2_hyda_regression():
    from patch_hyda_dialogue import expand_name_tokens

    translations_path = WS / "publish/so3dc-korean-tools/translations/hyda_ko.json"
    manifest_path = WS / "publish/so3dc-korean-tools/translations/hyda_patch_manifest.json"
    document = json.loads(translations_path.read_text(encoding="utf-8"))
    catalogue = json.loads(manifest_path.read_text(encoding="utf-8"))
    speaker_modes = {
        (o["archive_id"], o["stream_id"], o["message_id"]): o["speaker"]["mode"]
        for o in catalogue["occurrences"]
    }

    stream_targets: dict[tuple[int, int], dict[int, dict]] = defaultdict(dict)
    occurrence_count = 0
    for entry in document["translations"]:
        korean = expand_name_tokens(entry["korean"].replace("\r\n", "\n"))
        speaker = entry.get("speaker_korean")
        if speaker is not None:
            speaker = expand_name_tokens(speaker)
        for occ in entry["occurrences"]:
            key = (occ["archive_id"], occ["stream_id"])
            mode = speaker_modes[(occ["archive_id"], occ["stream_id"], occ["message_id"])]
            stream_targets[key][occ["message_id"]] = {
                "korean": korean,
                "speaker_korean": None if mode == "implicit_or_continuation" else speaker,
            }
            occurrence_count += 1
    assert occurrence_count == 653
    assert len(stream_targets) == 24

    plan = fp.build_plan(ISO, dict(stream_targets))
    assert sum(len(v) for v in plan.values()) == 24
    for archive_id, addresses in plan.items():
        assert list(addresses) == [(0, 1, 0)], f"unexpected address in archive {archive_id}"

    out_iso = Path(r"D:\ps2\TEST_full_patch_hyda.iso")
    if out_iso.exists():
        out_iso.unlink()
    try:
        # Legacy adapter: marker-less translations -> every control (incl. the
        # colour pair and mid-message page breaks) is re-anchored, exactly the
        # shipped v0.4 semantics.
        result = fp.patch_archives(
            ISO, out_iso, plan, font_path=FONT,
            positional=frozenset(), structural_page=False)
        assert result["archives_failed"] == 0
        assert result["archives_ok"] == 24
        output_sha = result["output_iso_sha256"]
        report_path = OUT_DIR / "t2_verifier_report.json"
        proc = subprocess.run(
            [sys.executable,
             str(WS / "publish/so3dc-korean-tools/tools/verify_hyda_dialogue_iso.py"),
             str(ISO), str(out_iso),
             "--catalogue", str(manifest_path),
             "--translations", str(translations_path),
             "--font", str(FONT),
             "--expected-output-sha256", output_sha,
             "--report", str(report_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3600,
        )
        assert proc.returncode == 0, f"verifier failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        verifier = json.loads(report_path.read_text(encoding="utf-8"))
        assert verifier["verified_occurrences"] == 653
        assert verifier["differences_outside_allowed_archives"] == 0
        assert verifier["encoded_index_unchanged"] is True
        (OUT_DIR / "t2_patch_report.json").write_text(
            json.dumps(result, indent=1, default=str), encoding="utf-8")
    finally:
        if out_iso.exists():
            out_iso.unlink()


# ---------------------------------------------------------------------------
# T3: Hangul capacity stress (fit simulator)
# ---------------------------------------------------------------------------

T3_ARCHIVES = list(range(76, 136)) + [3454, 1775, 6068, 38]


def test_t3_capacity_stress():
    manifest = fp.load_stream_manifest()
    bitmap_map = fp.load_bitmap_unicode_map()

    stream_targets: dict[tuple[int, int], dict[int, dict]] = {}
    stats: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (archive_id, stream_id), row in sorted(manifest.items()):
        if archive_id not in T3_ARCHIVES:
            continue
        if int(row["depth"]) != 0 or not row["magic_text"].startswith("so3mclib"):
            continue
        data = read_stream_from_iso(row)
        parsed = Mclib.parse(data)
        if (parsed.glyph_width, parsed.glyph_height) != (24, 24):
            continue
        code_to_char = fp.container_code_to_char(parsed, bitmap_map)
        counts = defaultdict(int)
        for mid, _ in parsed.rows:
            counts[mid] += 1
        translations: dict[int, dict] = {}
        for mid, offset in parsed.rows:
            stats[archive_id]["messages"] += 1
            if counts[mid] != 1:
                stats[archive_id]["skipped_dup_id"] += 1
                continue
            seg = parsed.segments[offset]
            try:
                plan = fp.analyze_segment(seg, TABLE)
            except fp.SegmentError:
                stats[archive_id]["skipped_untokenizable"] += 1
                continue
            body, speaker, unknown = fp.struct_to_text(plan, code_to_char)
            keep = (plan.speaker_field_tokens is not None
                    and any(t.kind != "glyph" for t in plan.speaker_field_tokens))
            if "〓" in body or unknown:
                stats[archive_id]["skipped_unknown_glyph"] += 1
                continue
            joined = body + (speaker or "")
            if not fp._JP_RE.search(joined):
                stats[archive_id]["skipped_no_jp"] += 1
                continue
            translations[mid] = {
                "korean": pseudo_translate_text(body),
                "speaker_korean": None if keep or speaker is None else pseudo_translate_text(speaker),
                "keep_speaker": keep,
            }
            stats[archive_id]["translated"] += 1
        if translations:
            stream_targets[(archive_id, stream_id)] = translations

    assert stream_targets, "no T3 targets found"
    plan = fp.build_plan(ISO, stream_targets)
    result = fp.simulate(ISO, plan, font_path=FONT)

    verdicts: dict[int, dict] = {}
    for report in result["archives"]:
        archive_id = report["archive_id"]
        strategies = {p["strategy"] for p in report["packages"]}
        old_total = sum(t["old_compressed"] for t in report["targets"].values())
        new_total = sum(t["new_compressed"] for t in report["targets"].values())
        gaps = {p["package"]: (p["gap_consumed"], p["available_gap"]) for p in report["packages"]}
        verdicts[archive_id] = {
            "verdict": "B" if "B" in strategies else "A",
            "targets": len(report["targets"]),
            "old_compressed": old_total,
            "new_compressed": new_total,
            "gap_consumed_by_pkg": gaps,
            **{k: v for k, v in stats[archive_id].items()},
        }
    for failure in result["failures"]:
        archive_id = failure["archive"]
        verdicts[archive_id] = {
            "verdict": "FAIL",
            "error": failure.get("error"),
            "needed": failure.get("needed"),
            "available": failure.get("available"),
            **{k: v for k, v in stats[archive_id].items()},
        }
    planned = {aid for aid in plan}
    missing = planned - set(verdicts)
    assert not missing, f"no verdict for archives {sorted(missing)}"

    (OUT_DIR / "t3_capacity.json").write_text(
        json.dumps({"verdicts": {str(k): v for k, v in sorted(verdicts.items())},
                    "summary": {
                        "archives": len(verdicts),
                        "A": sum(1 for v in verdicts.values() if v["verdict"] == "A"),
                        "B": sum(1 for v in verdicts.values() if v["verdict"] == "B"),
                        "FAIL": sum(1 for v in verdicts.values() if v["verdict"] == "FAIL"),
                    }}, indent=1, default=str), encoding="utf-8")
    # This test is the go/no-go *signal*: it must produce a verdict for every
    # planned archive; capacity failures are reported, not asserted.
    assert len(verdicts) == len(planned)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-x"]))

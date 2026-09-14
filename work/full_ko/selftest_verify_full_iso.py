# -*- coding: utf-8 -*-
"""Self-test for verify_full_iso.py (fixture generator + positive/negative runs).

so3_full_patch.py is used HERE as the fixture generator only (per task rules);
the verifier itself never imports it.

Steps
  0. synthetic container test: a hand-built mclib with a variant-speaker
     message and an 8780-construct message is patched via so3_full_patch and
     checked with verify_full_iso.verify_container directly (positive) plus
     mutation checks (negative).
  1. fixture: pick translation units fully confined to archives {1204, 1206,
     3454} (Hyda PK1 banks incl. a glyph_count==0 container occurring 5x, and
     the 3454 PACK battle DB), pseudo-translate them deterministically
     (JP chars -> Hangul pool, names literalized, width-trimmed against the
     delivery budgets), emit fixture tr_out batches + a patcher plan.
  2. build the patched ISO with so3_full_patch (CLI).
  3. run verify_full_iso.py -> must exit 0.
  4. negative A: flip one byte inside a patched target member -> must fail
     with an archive/decode error (not merely diff-scope).
  5. negative B: flip one byte outside all planned extents -> must fail with
     a diff_scope error.  Both flips are restored afterwards.

Phases can be run standalone on an existing fixture ISO:
  --negatives-phase   negative A + B + restored verification only
  --name-phase        apply so3_name_patch in-place, then negative C (verify
                      without --name-patch must fail via diff_scope),
                      positive 2 (verify --name-patch must pass) and negative
                      D (corrupt the font member; --name-patch must fail)
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_full_iso as V  # noqa: E402

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
FULL_KO = WS / "work" / "full_ko"
FIXTURE = FULL_KO / "verify_fixture"
ORIGINAL_ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
PATCHED_ISO = Path(r"D:\ps2\so3_fullko_fixture.iso")
FONT = Path(r"D:\ps2\NanumSquareNeo-cBd.ttf")
TARGET_ARCHIVES = {1204, 1206, 3454}
MAX_PLAIN_UNITS = 60
HANGUL_POOL = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허고노도로모보"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def info(msg: str) -> None:
    print(f"[selftest] {msg}", flush=True)


# ---------------------------------------------------------------------------
# step 0: synthetic container (variant speaker + 8780 construct)
# ---------------------------------------------------------------------------

def build_synthetic_mclib(segments: list[tuple[int, bytes]]) -> bytes:
    table_start = 0x80
    mapping_count = len(segments)
    text_start = (table_start + mapping_count * 8 + 0x7F) & ~0x7F
    blob = bytearray()
    offsets = []
    for _, seg in segments:
        offsets.append(len(blob))
        blob += seg
    file_size = (text_start + len(blob) + 0x7F) & ~0x7F
    data = bytearray(file_size)
    data[:0x10] = b"so3mclib 1.75\x00\x00\x00"
    struct.pack_into("<I", data, 0x10, table_start)
    struct.pack_into("<I", data, 0x14, text_start)
    struct.pack_into("<I", data, 0x18, 0)   # width_start (glyph_count == 0)
    struct.pack_into("<I", data, 0x1C, 0)   # bitmap_start
    struct.pack_into("<I", data, 0x20, 0)   # glyph_count
    struct.pack_into("<I", data, 0x2C, 24)  # glyph_width
    struct.pack_into("<I", data, 0x30, 24)  # glyph_height
    struct.pack_into("<I", data, 0x34, 24)  # glyph_stride
    struct.pack_into("<I", data, 0x38, 301)  # local_base
    struct.pack_into("<I", data, 0x3C, mapping_count)
    struct.pack_into("<I", data, 0x40, file_size)
    for i, ((mid, _), off) in enumerate(zip(segments, offsets)):
        struct.pack_into("<II", data, table_start + i * 8, mid, off)
    data[text_start:text_start + len(blob)] = blob
    return bytes(data)


def synthetic_test(ctl: V.Controls, renderer: V.Renderer, global_widths: bytes) -> None:
    import so3_full_patch as P  # fixture generator only

    g = lambda code: bytes([code]) if code < 0x80 else bytes([(code & 0x7F) | 0x80, code >> 7])
    # msg 1: variant speaker (8880 06 | 9380 01 | 8980 | 8080) + body "AB" / page / "a"
    msg1 = (b"\x88\x80\x06" + b"\x93\x80\x01" + b"\x89\x80" + b"\x80\x80"
            + g(14) + g(15) + b"\x81\x80" + g(40) + b"\x00")
    # msg 2: 8780 construct: prefix 8880 06 | field "AB" | 8980 8780 8080 | body "b" + 8580 float
    msg2 = (b"\x88\x80\x06" + g(14) + g(15) + b"\x89\x80\x87\x80\x80\x80"
            + b"\x85\x80" + struct.pack("<f", 0.1) + g(41) + b"\x00")
    # msg 3: non-target
    msg3 = g(16) + g(17) + b"\x00"
    original = build_synthetic_mclib([(1, msg1), (2, msg2), (3, msg3)])

    translations = {
        1: {"korean": "⟦1⟧소피⟦2⟧\n가나⟦P⟧다"},         # variant -> body markers
        2: {"korean": "라마", "speaker_korean": "바사"},
    }
    patched, report = P.rebuild_container(original, translations, font_path=FONT)

    exp1 = V.ExpectedMessage(
        key="synth1", korean="가나\n⟦P⟧다", speaker_korean="소피",
        unit={"speaker_mode": "character_reference"}, budget_px=480,
        msg_sha=V.sha256(V.logical_segment(msg1)), container_sha="synth")
    exp1.msg_sha = V.sha256(msg1)
    exp1.body_pages = V.parse_expected_body(exp1.korean)
    exp1.speaker_units = V.parse_expected_inline(exp1.speaker_korean)
    exp2 = V.ExpectedMessage(
        key="synth2", korean="라마", speaker_korean="바사",
        unit={"speaker_mode": "literal_glyphs"}, budget_px=480,
        msg_sha=V.sha256(msg2), container_sha="synth")
    exp2.body_pages = V.parse_expected_body(exp2.korean)
    exp2.speaker_units = V.parse_expected_inline(exp2.speaker_korean)

    expects = {1: exp1, 2: exp2}
    result = V.verify_container("synthetic", original, patched, expects, None,
                                ctl, renderer, global_widths)
    if not result.ok:
        for e in result.errors:
            info(f"  synthetic error: {e}")
        raise SystemExit("synthetic positive test FAILED")
    if sorted(result.verified_message_ids) != [1, 2]:
        raise SystemExit("synthetic positive test verified wrong messages")
    info("synthetic positive test OK (variant speaker + 8780 construct)")

    # negative: swap the color operand of the variant 8880 in the patched blob
    mutated = patched.replace(b"\x88\x80\x06", b"\x88\x80\x07", 1)
    assert mutated != patched
    bad = V.verify_container("synthetic-neg", original, mutated, expects, None,
                             ctl, renderer, global_widths)
    if bad.ok or not any("positional control" in e[2] for e in bad.errors):
        raise SystemExit("synthetic negative test (operand mutation) NOT caught")
    info("synthetic negative test OK (mutated positional operand caught)")

    # negative: speaker text mutation
    exp2b = V.ExpectedMessage(
        key="synth2", korean="라마", speaker_korean="바마",
        unit={"speaker_mode": "literal_glyphs"}, budget_px=480,
        msg_sha=V.sha256(msg2), container_sha="synth")
    exp2b.body_pages = V.parse_expected_body(exp2b.korean)
    exp2b.speaker_units = V.parse_expected_inline(exp2b.speaker_korean)
    bad2 = V.verify_container("synthetic-neg2", original, patched,
                              {1: exp1, 2: exp2b}, None, ctl, renderer, global_widths)
    if bad2.ok or not any("speaker decode mismatch" in e[2]
                          or "unverified patched glyph" in e[2] for e in bad2.errors):
        raise SystemExit("synthetic negative test (speaker mismatch) NOT caught")
    info("synthetic negative test OK (speaker mismatch caught: "
         + bad2.errors[0][2] + ")")


# ---------------------------------------------------------------------------
# step 1: fixture selection + pseudo-translation
# ---------------------------------------------------------------------------

def pool_char(ch: str) -> str:
    digest = hashlib.md5(ch.encode("utf-8")).digest()
    return HANGUL_POOL[digest[0] % len(HANGUL_POOL)]


JP_RE = V.re.compile(r"[ぁ-ゟ゠-ヿ一-鿿]")
KEEP_JP = {"・"}


def transliterate(text: str) -> str:
    out = []
    idx = 0
    for match in V.TOKEN_RE.finditer(text):
        for ch in text[idx:match.start()]:
            out.append(_translit_char(ch))
        token = match.group(1)
        if token.startswith("이름:"):
            out.append(token[len("이름:"):])  # literalize the Korean name
        else:
            out.append(f"⟦{token}⟧")
        idx = match.end()
    for ch in text[idx:]:
        out.append(_translit_char(ch))
    return "".join(out)


def _translit_char(ch: str) -> str:
    if ch == "ヽ":
        return "、"
    if ch == "゜":
        return "。"
    if ch in KEEP_JP or not JP_RE.search(ch):
        return ch
    return pool_char(ch)


class WidthCalc:
    def __init__(self, global_widths: bytes, font_path: Path) -> None:
        from PIL import ImageFont
        self.gw = global_widths
        self.font = ImageFont.truetype(str(font_path), 22)
        self.cache: dict[str, int] = {}

    def px(self, ch: str) -> int:
        w = self.cache.get(ch)
        if w is None:
            code = V.GLOBAL_CODE_MAP.get(ch)
            if code is not None and 0 <= code - 1 < len(self.gw):
                w = self.gw[code - 1]
            else:
                w = max(1, min(24, round(self.font.getlength(ch))))
            self.cache[ch] = w
        return w

    def line_px(self, line: str) -> int:
        return sum(self.px(ch) for ch in V.TOKEN_RE.sub("", line))


def message_scale_bounds(seg: bytes, ctl: V.Controls) -> tuple[float, float]:
    """Conservative (max scale, max spacing) over a message's controls."""
    max_scale, max_spacing = 1.0, 0.0
    try:
        tokens, _ = V.tokenize(seg, ctl)
    except V.TokenError:
        return 4.0, 0.0
    for token in tokens:
        if token[0] != "c":
            continue
        fam, raw = token[1], token[2]
        if fam == V.SCALE_FAM and len(raw) == 6:
            value = struct.unpack_from("<f", raw, 2)[0]
            if 0.0 < value <= 4.0:
                max_scale = max(max_scale, value)
        elif fam == V.ADVANCE_FAM and len(raw) == 6:
            value = struct.unpack_from("<f", raw, 2)[0]
            if 0.0 <= value <= 128.0:
                max_spacing = max(max_spacing, value)
    return max_scale, max_spacing


def trim_line(line: str, budget: int, calc: WidthCalc, scale: float, spacing: float,
              marker_lookup: dict[int, str]) -> tuple[str, bool]:
    def measured(text: str) -> int:
        base = 0
        for ch in V.TOKEN_RE.sub("", text):
            base += int(round(calc.px(ch) * scale + spacing))
        allowance = 0
        for token in V.TOKEN_RE.findall(text):
            if token.isdigit():
                fam = marker_lookup.get(int(token), "")
                allowance += {"9280": 96, "a180": 96, "a280": 144, "a380": 144}.get(fam, 0)
            elif token.startswith("G:"):
                allowance += int(24 * scale)
        return int(base + allowance)

    while measured(line) > budget:
        # drop the last plain character (never a marker token)
        spans = [(m.start(), m.end()) for m in V.TOKEN_RE.finditer(line)]
        drop = None
        for pos in range(len(line) - 1, -1, -1):
            if any(s <= pos < e for s, e in spans):
                continue
            drop = pos
            break
        if drop is None:
            return line, False  # tokens only and still over budget
        line = line[:drop] + line[drop + 1:]
    return line, True


def build_fixture(ctl: V.Controls, global_widths: bytes):
    units = V.load_units(FULL_KO / "translation_units.jsonl")
    inventory = json.loads((FULL_KO / "inventory_containers.json").read_text(encoding="utf-8"))
    manifest = V.load_manifest(WS / "work" / "full_unpack" / "disc1" / "manifests" / "stream_manifest.csv")

    # batch membership (budgets are the delivery gate)
    key_batch: dict[str, tuple[str, int]] = {}
    for path in sorted((FULL_KO / "tr_batches").glob("batch_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for unit in doc.get("units", []):
            key = unit.get("key")
            if key and key not in key_batch and isinstance(unit.get("budget_px"), int):
                key_batch[key] = (doc["batch_id"], unit["budget_px"])

    key_arch: dict[str, set[int]] = defaultdict(set)
    key_containers: dict[str, list[tuple[dict, list[dict]]]] = defaultdict(list)
    for container in inventory["containers"]:
        archives = {r["archive"] for r in container["refs"]}
        messages_by_key: dict[str, list[dict]] = defaultdict(list)
        for message in container["messages"]:
            key = message.get("text_key")
            if key:
                key_arch[key] |= archives
                messages_by_key[key].append(message)
        for key, messages in messages_by_key.items():
            key_containers[key].append((container, messages))

    confined = [k for k in key_arch
                if key_arch[k] <= TARGET_ARCHIVES and k in units and k in key_batch]
    chosen: list[str] = []
    plains: list[str] = []
    for key in sorted(confined):
        unit = units[key]
        if unit.get("has_unknown_glyph"):
            continue
        if unit.get("speaker_mode") == "control_expression":
            continue
        if unit.get("jp_speaker") and V.TOKEN_RE.search(unit["jp_speaker"]) and \
                any(not t.startswith("이름:") for t in V.TOKEN_RE.findall(unit["jp_speaker"])):
            continue
        interesting = (unit.get("jp_speaker") or unit.get("markers")
                       or "⟦P⟧" in unit["jp_body"] or "⟦G:" in unit["jp_body"]
                       or unit.get("has_ruby"))
        if interesting:
            chosen.append(key)
        else:
            plains.append(key)
    chosen.extend(plains[:MAX_PLAIN_UNITS])
    info(f"fixture units chosen: {len(chosen)} "
         f"(interesting {len(chosen) - min(len(plains), MAX_PLAIN_UNITS)}, "
         f"plain {min(len(plains), MAX_PLAIN_UNITS)})")

    calc = WidthCalc(global_widths, FONT)
    container_cache: dict[str, object] = {}

    def container_segment(container: dict, mid: int) -> bytes | None:
        sha = container["file_sha256"]
        parsed = container_cache.get(sha)
        if parsed is None:
            from so3_repack import Mclib
            parsed = Mclib.parse(Path(container["path"]).read_bytes())
            container_cache[sha] = parsed
        offsets = [off for m, off in parsed.rows if m == mid]
        if len(offsets) != 1:
            return None
        return parsed.segments[offsets[0]]

    translations_out: dict[str, dict] = {}
    dropped: list[tuple[str, str]] = []
    for key in list(chosen):
        unit = units[key]
        # variant-instance / ambiguity screening on every occurrence
        variant = ambiguous = False
        scale, spacing = 1.0, 0.0
        for container, messages in key_containers[key]:
            for message in messages:
                seg = container_segment(container, message["id"])
                if seg is None:
                    ambiguous = True
                    continue
                try:
                    analysis = V.analyze_message(seg, ctl)
                except V.TokenError:
                    ambiguous = True
                    continue
                if analysis.field_tokens is None and analysis.variant_prefix is not None \
                        and unit.get("jp_speaker") is not None:
                    variant = True
                s, sp = message_scale_bounds(seg, ctl)
                scale, spacing = max(scale, s), max(spacing, sp)
        if variant or ambiguous:
            chosen.remove(key)
            dropped.append((key, "variant" if variant else "ambiguous"))
            continue

        budget = key_batch[key][1]
        marker_lookup = {m["n"]: m["family"] for m in (unit.get("markers") or [])}
        lines = []
        overflow = False
        for line in transliterate(unit["jp_body"]).split("\n"):
            trimmed, fits = trim_line(line, budget, calc, scale, spacing, marker_lookup)
            if not fits:
                overflow = True
            lines.append(trimmed)
        if overflow:
            chosen.remove(key)
            dropped.append((key, "budget_overflow"))
            continue
        korean = "\n".join(lines)
        speaker = None
        if unit.get("jp_speaker") is not None:
            speaker = transliterate(unit["jp_speaker"])
        translations_out[key] = {"korean": korean, "speaker_korean": speaker}
    info(f"fixture units after screening: {len(chosen)} (dropped {len(dropped)})")

    # tr_out fixture files
    tr_dir = FIXTURE / "tr_out"
    tr_dir.mkdir(parents=True, exist_ok=True)
    for old in tr_dir.glob("batch_*_ko.json"):
        old.unlink()
    by_batch: dict[str, list[dict]] = defaultdict(list)
    for key in chosen:
        batch_id = key_batch[key][0]
        entry = {"key": key, "korean": translations_out[key]["korean"]}
        entry["speaker_korean"] = translations_out[key]["speaker_korean"]
        by_batch[batch_id].append(entry)
    for batch_id, entries in sorted(by_batch.items()):
        (tr_dir / f"{batch_id}_ko.json").write_text(
            json.dumps({"batch_id": batch_id, "translations": entries},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    info(f"fixture tr_out: {len(by_batch)} batch files")

    # patcher plan (streams form).  The production plan keeps the inventory
    # "\n⟦P⟧" page convention verbatim (so3_full_patch's parser tolerates the
    # trailing newline; verify_full_iso.parse_expected_body requires it), so
    # the fixture plan uses the same convention -- no rewrite.
    streams: dict[str, dict[str, dict]] = defaultdict(dict)
    n_msgs = 0
    for key in chosen:
        entry = translations_out[key]
        korean_plan = entry["korean"]
        for container, messages in key_containers[key]:
            for ref in container["refs"]:
                a, s = ref["archive"], ref["stream"]
                if manifest.get((a, s), {}).get("depth", 1) != 0:
                    raise SystemExit(f"fixture stream {a}:{s} is nested")
                for message in messages:
                    plan_entry: dict = {"korean": korean_plan}
                    if entry["speaker_korean"] is not None:
                        plan_entry["speaker_korean"] = entry["speaker_korean"]
                    streams[f"{a}:{s}"][str(message["id"])] = plan_entry
                    n_msgs += 1
    plan_path = FIXTURE / "plan.json"
    plan_path.write_text(json.dumps({"streams": streams}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    info(f"plan: {len(streams)} streams, {n_msgs} message occurrences -> {plan_path}")
    (FIXTURE / "selection.json").write_text(json.dumps({
        "chosen": chosen, "dropped": dropped,
        "streams": sorted(streams), "messages": n_msgs,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# steps 2-5
# ---------------------------------------------------------------------------

def run(cmd: list[str], name: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    info(f"run {name}: {' '.join(str(c) for c in cmd[:6])} ...")
    started = time.time()
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    info(f"{name}: exit {proc.returncode} in {time.time() - started:.0f}s")
    return proc


def flip_byte(path: Path, offset: int) -> int:
    with path.open("r+b") as handle:
        handle.seek(offset)
        value = handle.read(1)[0]
        handle.seek(offset)
        handle.write(bytes([value ^ 0xFF]))
    return value


def restore_byte(path: Path, offset: int, value: int) -> None:
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(bytes([value]))


def name_phase() -> int:
    """Phase 2: apply so3_name_patch in-place on the fixture ISO, then
    negative C (verify without --name-patch must fail via diff_scope),
    positive 2 (verify with --name-patch must pass), and negative D
    (corrupt the patched font member payload; --name-patch must fail via
    the name_patch checks)."""
    import so3_name_patch as NP

    if not PATCHED_ISO.exists():
        raise SystemExit("fixture ISO missing; run the main phase first")
    plan_path = FIXTURE / "plan.json"

    info("applying so3_name_patch in-place on the fixture ISO")
    proc = run([sys.executable, str(FULL_KO / "so3_name_patch.py"),
                str(PATCHED_ISO), "--report", str(FIXTURE / "name_patch_report.json")],
               "so3_name_patch")
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit("name patch application failed")

    verifier = [sys.executable, str(FULL_KO / "verify_full_iso.py"),
                "--original", str(ORIGINAL_ISO), "--patched", str(PATCHED_ISO),
                "--plan", str(plan_path),
                "--tr-out-dir", str(FIXTURE / "tr_out"),
                "--allow-untranslated"]

    # negative C: name-patched ISO without --name-patch -> diff_scope failure
    proc = run(verifier + ["--skip-xdelta",
                           "--report", str(FIXTURE / "verify_report_negC.json")],
               "verify (negative C: name patch without --name-patch)")
    if proc.returncode == 0:
        raise SystemExit("negative C NOT caught")
    reportC = json.loads((FIXTURE / "verify_report_negC.json").read_text(encoding="utf-8"))
    checksC = {e["check"] for e in reportC["errors"]}
    info(f"negative C caught: error checks = {sorted(checksC)}")
    if "diff_scope" not in checksC:
        raise SystemExit("negative C did not trip diff_scope")

    # positive 2: with --name-patch (full checks incl. xdelta round-trip)
    proc = run(verifier + ["--name-patch",
                           "--report", str(FIXTURE / "verify_report_name.json")],
               "verify (positive 2: --name-patch)", timeout=7200)
    print(proc.stdout[-2000:])
    if proc.returncode != 0:
        print(proc.stderr[-3000:])
        raise SystemExit("POSITIVE --name-patch verification failed (should pass)")
    reportN = json.loads((FIXTURE / "verify_report_name.json").read_text(encoding="utf-8"))
    audit = reportN["checks"]["name_patch"]["independent_font_audit"]
    info(f"positive 2 PASSED (font audit: {audit})")

    # negative D: corrupt the patched global font member payload
    font_offset = NP.FONT_MEMBER["iso_offset"] + 16 + 100
    old = flip_byte(PATCHED_ISO, font_offset)
    try:
        proc = run(verifier + ["--name-patch", "--skip-xdelta",
                               "--report", str(FIXTURE / "verify_report_negD.json")],
                   "verify (negative D: corrupt font member)")
    finally:
        restore_byte(PATCHED_ISO, font_offset, old)
    if proc.returncode == 0:
        raise SystemExit("negative D NOT caught")
    reportD = json.loads((FIXTURE / "verify_report_negD.json").read_text(encoding="utf-8"))
    checksD = {e["check"] for e in reportD["errors"]}
    info(f"negative D caught: error checks = {sorted(checksD)}")
    if "name_patch" not in checksD:
        raise SystemExit("negative D did not trip the name_patch checks")

    info("NAME-PATCH PHASE COMPLETE: negative C + positive 2 + negative D OK")
    return 0


def negatives_phase(index, manifest, plan_path: Path) -> int:
    """Negative A (corrupt a target payload byte), negative B (corrupt a byte
    outside every planned extent), then a restored-ISO verification."""
    verifier = [sys.executable, str(FULL_KO / "verify_full_iso.py"),
                "--original", str(ORIGINAL_ISO), "--patched", str(PATCHED_ISO),
                "--plan", str(plan_path),
                "--tr-out-dir", str(FIXTURE / "tr_out"),
                "--allow-untranslated"]

    # locate a byte inside the patched target member (archive 1204, stream
    # 5829): resolve the (package,row,chain) address in the ORIGINAL layout,
    # then take the member at the same address in the PATCHED layout (robust
    # under strategy-B in-package shifts)
    archive_start = index[1204] * V.SECTOR
    archive_size = index[V.INDEX_ENTRIES + 1204] * V.SECTOR
    source_offset = manifest[(1204, 5829)]["source_offset"]
    with ORIGINAL_ISO.open("rb") as handle:
        original_archive = V.read_range(handle, archive_start, archive_size)
    with PATCHED_ISO.open("rb") as handle:
        patched_archive = V.read_range(handle, archive_start, archive_size)
    address = V.locate_member(V.parse_layout(original_archive), source_offset)
    assert address is not None, "target member not found in original layout"
    packages = V.parse_layout(patched_archive)
    member = packages[address[0]].rows[address[1]].members[address[2]]
    inside_offset = archive_start + member.offset + 16 + min(64, member.comp - 1)

    # negative A (inside a patched extent, inside the target payload)
    old = flip_byte(PATCHED_ISO, inside_offset)
    try:
        proc = run(verifier + ["--skip-xdelta",
                               "--report", str(FIXTURE / "verify_report_negA.json")],
                   "verify (negative A: corrupt target payload)")
    finally:
        restore_byte(PATCHED_ISO, inside_offset, old)
    if proc.returncode == 0:
        raise SystemExit("negative A NOT caught")
    reportA = json.loads((FIXTURE / "verify_report_negA.json").read_text(encoding="utf-8"))
    checksA = {e["check"] for e in reportA["errors"]}
    info(f"negative A caught: error checks = {sorted(checksA)}")
    if checksA <= {"diff_scope"}:
        raise SystemExit("negative A only tripped diff_scope; expected archive/decode")

    # negative B (outside all planned extents)
    extents = []
    for stream_key in json.loads(plan_path.read_text(encoding="utf-8"))["streams"]:
        archive_id = int(stream_key.split(":")[0])
        extents.append((index[archive_id] * V.SECTOR,
                        index[V.INDEX_ENTRIES + archive_id] * V.SECTOR))
    outside = 0x00500000
    while any(start <= outside < start + size for start, size in extents) or \
            V.INDEX_OFFSET <= outside < V.INDEX_OFFSET + V.INDEX_BYTES:
        outside += 0x100000
    old = flip_byte(PATCHED_ISO, outside)
    try:
        proc = run(verifier + ["--skip-xdelta",
                               "--report", str(FIXTURE / "verify_report_negB.json")],
                   "verify (negative B: corrupt outside extents)")
    finally:
        restore_byte(PATCHED_ISO, outside, old)
    if proc.returncode == 0:
        raise SystemExit("negative B NOT caught")
    reportB = json.loads((FIXTURE / "verify_report_negB.json").read_text(encoding="utf-8"))
    checksB = {e["check"] for e in reportB["errors"]}
    info(f"negative B caught: error checks = {sorted(checksB)}")
    if "diff_scope" not in checksB:
        raise SystemExit("negative B did not trip diff_scope")

    # confirm the restored ISO still verifies
    proc = run(verifier + ["--skip-xdelta",
                           "--report", str(FIXTURE / "verify_report_restored.json")],
               "verify (restored)")
    if proc.returncode != 0:
        raise SystemExit("restored ISO no longer verifies; flips not restored?")

    info("NEGATIVES PHASE COMPLETE: negative A + B caught, restored ISO verifies")
    return 0


def main() -> int:
    if "--name-phase" in sys.argv:
        return name_phase()
    if "--negatives-phase" in sys.argv:
        index = V.read_index(ORIGINAL_ISO)
        manifest = V.load_manifest(WS / "work" / "full_unpack" / "disc1" /
                                   "manifests" / "stream_manifest.csv")
        return negatives_phase(index, manifest, FIXTURE / "plan.json")
    FIXTURE.mkdir(parents=True, exist_ok=True)
    ctl = V.Controls(FULL_KO / "control_sizes_full.json")
    renderer = V.Renderer(FONT)
    index = V.read_index(ORIGINAL_ISO)
    manifest = V.load_manifest(WS / "work" / "full_unpack" / "disc1" /
                               "manifests" / "stream_manifest.csv")
    global_widths = V.load_global_widths(
        ORIGINAL_ISO, index, WS / "work" / "mclib_all_decode" / "container_catalog.csv",
        manifest)

    synthetic_test(ctl, renderer, global_widths)
    plan_path = build_fixture(ctl, global_widths)

    # step 2: build the fixture ISO
    if PATCHED_ISO.exists():
        PATCHED_ISO.unlink()
    proc = run([sys.executable, str(FULL_KO / "so3_full_patch.py"),
                str(ORIGINAL_ISO), str(PATCHED_ISO),
                "--plan", str(plan_path), "--font", str(FONT),
                "--report", str(FIXTURE / "patch_report.json")], "so3_full_patch")
    if proc.returncode != 0:
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
        raise SystemExit("fixture ISO build failed")

    verifier = [sys.executable, str(FULL_KO / "verify_full_iso.py"),
                "--original", str(ORIGINAL_ISO), "--patched", str(PATCHED_ISO),
                "--plan", str(plan_path),
                "--tr-out-dir", str(FIXTURE / "tr_out"),
                "--allow-untranslated"]

    # step 3: positive run (with xdelta round-trip)
    proc = run(verifier + ["--report", str(FIXTURE / "verify_report.json")],
               "verify (positive)", timeout=7200)
    print(proc.stdout[-2500:])
    if proc.returncode != 0:
        print(proc.stderr[-3000:])
        raise SystemExit("POSITIVE verification failed (should pass)")
    info("positive verification PASSED")

    # steps 4-6: negatives + restored verification
    negatives_phase(index, manifest, plan_path)
    info("SELF-TEST COMPLETE: positive + negative A/B all behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())

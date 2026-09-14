# -*- coding: utf-8 -*-
"""validate_translations.py — SO3 DC full-KO: validate translated batch outputs.

Inputs:
  --batches-dir  tr_batches\\batch_NNNN.json   (prepare_translation_batches.py output)
  --out-dir      tr_out\\batch_NNNN_ko.json    {batch_id, translations:
                                                [{key, speaker_korean|null, korean}]}
                 batch_id must echo the batch file's own string id ("batch_0024").
                 The numeric shorthand (24 / "24") is coerced with a
                 W batch_id_not_canonical; anything else is E unknown_batch_id and
                 the whole file goes unchecked.

Checks per unit (E = error, W = warning):
  1. coverage      E missing_translation / extra_key / duplicate_key; every unit key in
                   the batch must be translated exactly once, no extras.
  2. line struct   E line_count (korean line count != jp line_count);
                   E page_marker (⟦P⟧ not at the same line indices as jp_body, not at
                   line start, or duplicated on a line).
  3. markers       E marker_sequence (numbered ⟦n⟧ not exactly 1..N in ascending order,
                   duplicates/missing/extra); E unknown_token (any ⟦..⟧ token other
                   than ⟦P⟧/⟦n⟧); E name_token_residue (⟦이름:..⟧ left unliteralized).
  4. speaker       E missing_speaker / unexpected_speaker / speaker_multiline;
                   E speaker_token_missing (verbatim ⟦..#..⟧ token from jp_speaker
                   dropped); E name_token_residue in speaker.
  5. width         E width: per Korean line, px = Σ advance(ch) with ⟦..⟧ tokens
                   stripped first, + per-marker allowance for dynamic inserts on that
                   line (9280/a180 +96px, a280/a380 +144px, others +0 — runtime
                   substitution adds width the static text cannot show). Advance:
                   chars in GLOBAL_CODE_MAP -> ORIGINAL global 1.72 atlas width table;
                   all other chars -> round(Nanum 22px getlength) clamped [1, 24].
                   Compared against the unit budget_px (per-unit budget).
  6. glossary      W glossary_mismatch: batch glossary (+ core_names) jp term appears in
                   jp_body but mapped ko not found in korean (soft — inflection may
                   legitimately alter the form).
  7. residue       E jp_residue: kana/kanji remaining in korean/speaker after stripping
                   ⟦..⟧ tokens (・ U+30FB is allowed: legit typographic dot in the
                   global atlas); W unknown_glyph_char: 〓 remaining.

Outputs: machine-readable violations JSONL + JSON summary report.
Exit code 0 iff no errors (warnings OK).
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import struct
import sys
import time
from collections import Counter, defaultdict

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WS = os.environ.get(
    "SO3_WS",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
FULL_KO = os.path.dirname(os.path.abspath(__file__))
DEF_BATCHES = os.path.join(FULL_KO, "tr_batches")
DEF_OUT = os.path.join(FULL_KO, "tr_out")
DEF_REPORT = os.path.join(FULL_KO, "tr_validation_report.json")
DEF_CATALOG = os.path.join(WS, r"work\mclib_all_decode\container_catalog.csv")
DEF_FONT = os.environ.get("SO3_FONT", r"D:\ps2\NanumSquareNeo-cBd.ttf")
FONT_SIZE = 22
NANUM_MIN, NANUM_MAX = 1, 24

# Copied from publish\so3dc-korean-tools\tools\patch_hyda_dialogue.py GLOBAL_CODE_MAP
# (identified slots of the unchanged global 24px atlas; codes are 1-based glyph codes).
GLOBAL_CODE_MAP = {
    **{str(value): value + 1 for value in range(10)},
    **{chr(ord("A") + value): 14 + value for value in range(26)},
    **{chr(ord("a") + value): 40 + value for value in range(26)},
    "-": 11,
    ".": 12,
    "'": 13,
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

# px allowance per dynamic-insert marker family (runtime substitution adds width)
MARKER_ALLOWANCE_PX = {"9280": 96, "a180": 96, "a280": 144, "a380": 144}

TOKEN_RE = re.compile(r"⟦([^⟦⟧]*)⟧")  # ⟦...⟧
JP_RESIDUE_RE = re.compile(r"[ぁ-ゟ゠-ヿ一-鿿]")  # kana+kanji
ALLOWED_JP_CHARS = {"・"}  # U+30FB middle dot: allowed (global atlas slot 239)
PAGE = "⟦P⟧"
NAME_PREFIX = "이름:"


def load_global_widths(catalog_csv):
    """Embedded loader: find the global 1.72 container in container_catalog.csv and
    read its 1-byte-per-glyph advance width table straight from the mclib header."""
    with open(catalog_csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row["version"].strip().endswith("1.72"):
                continue
            path = row["path"]
            if not os.path.exists(path):
                continue
            data = open(path, "rb").read()
            if not data.startswith(b"so3mclib "):
                continue
            width_start = struct.unpack_from("<I", data, 0x18)[0]
            glyph_count = struct.unpack_from("<I", data, 0x20)[0]
            widths = data[width_start:width_start + glyph_count]
            if len(widths) >= 292:
                return widths
    raise SystemExit(f"global 1.72 width table not found via {catalog_csv}")


class WidthEngine:
    """Advance width per char: GLOBAL_CODE_MAP chars use the original global atlas
    width table; everything else uses Nanum 22px advances clamped to [1, 24]."""

    def __init__(self, global_widths, font_path):
        from PIL import ImageFont
        self.gw = global_widths
        self.font = ImageFont.truetype(font_path, FONT_SIZE)
        self.cache = {}

    def px(self, ch):
        w = self.cache.get(ch)
        if w is not None:
            return w
        code = GLOBAL_CODE_MAP.get(ch)
        if code is not None and 0 <= code - 1 < len(self.gw):
            w = self.gw[code - 1]
        else:
            w = int(round(self.font.getlength(ch)))
            w = max(NANUM_MIN, min(NANUM_MAX, w))
        self.cache[ch] = w
        return w

    def line_px(self, line):
        return sum(self.px(ch) for ch in TOKEN_RE.sub("", line))


class Reporter:
    def __init__(self):
        self.violations = []
        self.errors = 0
        self.warnings = 0

    def add(self, severity, vtype, batch_id, key, detail):
        self.violations.append({"severity": severity, "type": vtype,
                                "batch_id": batch_id, "key": key, "detail": detail})
        if severity == "error":
            self.errors += 1
        else:
            self.warnings += 1

    def err(self, vtype, batch_id, key, **detail):
        self.add("error", vtype, batch_id, key, detail)

    def warn(self, vtype, batch_id, key, **detail):
        self.add("warning", vtype, batch_id, key, detail)


def check_unit(rep, batch, unit, tr, weng):
    bid = batch["batch_id"]
    key = unit["key"]
    ko = tr.get("korean")
    if not isinstance(ko, str) or not ko.strip():
        rep.err("empty_korean", bid, key, got=repr(ko)[:80])
        return
    ko_lines = ko.split("\n")
    jp_lines = unit["jp_body"].split("\n")

    # 2. line structure
    if len(ko_lines) != unit["line_count"]:
        rep.err("line_count", bid, key,
                expected=unit["line_count"], actual=len(ko_lines))
    jp_p = [i for i, ln in enumerate(jp_lines) if PAGE in ln]
    ko_p = [i for i, ln in enumerate(ko_lines) if PAGE in ln]
    if jp_p != ko_p:
        rep.err("page_marker", bid, key, expected_line_indices=jp_p,
                actual_line_indices=ko_p)
    else:
        for i in ko_p:
            if not ko_lines[i].startswith(PAGE) or ko_lines[i].count(PAGE) != 1:
                rep.err("page_marker", bid, key, line_index=i,
                        line=ko_lines[i], reason="⟦P⟧ must appear once, at line start")

    # 3. markers
    fams = unit.get("markers") or []
    tokens = TOKEN_RE.findall(ko)
    nums = [int(t) for t in tokens if t.isdigit()]
    name_toks = [t for t in tokens if t.startswith(NAME_PREFIX)]
    unknown = [t for t in tokens
               if t != "P" and not t.isdigit() and not t.startswith(NAME_PREFIX)
               and not t.startswith("G:")]
    # 3b. preserve-bitmap glyph tokens ⟦G:sha8⟧: sequence must match jp_body exactly
    jp_g = [t for t in TOKEN_RE.findall(unit["jp_body"]) if t.startswith("G:")]
    ko_g = [t for t in tokens if t.startswith("G:")]
    if jp_g != ko_g:
        rep.err("glyph_token_mismatch", bid, key, expected=jp_g, actual=ko_g,
                reason="⟦G:sha8⟧ tokens must be preserved verbatim, same order")
    if name_toks:
        rep.err("name_token_residue", bid, key, tokens=name_toks,
                reason="⟦이름:..⟧ must be literalized in korean")
    if unknown:
        rep.err("unknown_token", bid, key, tokens=sorted(set(unknown)))
    if nums != list(range(1, len(fams) + 1)):
        rep.err("marker_sequence", bid, key,
                expected=list(range(1, len(fams) + 1)), actual=nums,
                reason="each ⟦n⟧ exactly once, ascending order")

    # 4. speaker
    jp_spk = unit.get("jp_speaker")
    spk = tr.get("speaker_korean")
    if jp_spk is None:
        if spk not in (None, ""):
            rep.err("unexpected_speaker", bid, key, speaker_korean=spk)
    else:
        if not isinstance(spk, str) or not spk.strip():
            rep.err("missing_speaker", bid, key, jp_speaker=jp_spk)
        else:
            if "\n" in spk:
                rep.err("speaker_multiline", bid, key, speaker_korean=spk)
            spk_names = [t for t in TOKEN_RE.findall(spk) if t.startswith(NAME_PREFIX)]
            if spk_names:
                rep.err("name_token_residue", bid, key, field="speaker",
                        tokens=spk_names)
            for t in TOKEN_RE.findall(jp_spk):
                if t.startswith(NAME_PREFIX):
                    continue  # must be literalized (checked above)
                if f"⟦{t}⟧" not in spk:  # dynamic e.g. ⟦문자열#..⟧: preserve verbatim
                    rep.err("speaker_token_missing", bid, key, token=f"⟦{t}⟧",
                            jp_speaker=jp_spk, speaker_korean=spk)
            spk_resid = sorted(set(JP_RESIDUE_RE.findall(TOKEN_RE.sub("", spk)))
                               - ALLOWED_JP_CHARS)
            if spk_resid:
                rep.err("jp_residue", bid, key, field="speaker", chars=spk_resid)

    # 5. width (vs per-unit budget). px = static Korean text (⟦⟧ tokens stripped);
    #    allow = conservative per-marker runtime-insert allowance.
    #    Policy: a line's STATIC text must fit the budget within one glyph of slack
    #    (NAME_TOL) -- this is the hard gate that forces verbose lines to be
    #    shortened. Two softer conditions are runtime-verify warnings, not errors:
    #      * width_minor: static text is over budget but within NAME_TOL. Happens
    #        for irreducible proper nouns wider in Korean than in Japanese
    #        (소피아, 아드레이・라즈버드) -- shortening would corrupt the name.
    #      * width_marker_pressure: static text fits, but static+insert-allowance
    #        exceeds budget. The overflow comes from runtime-substituted inserts
    #        (item names, numbers) whose real width is unknown at translation time,
    #        so it cannot be controlled here and must be checked in the emulator.
    NAME_TOL = 32  # ~1.4 glyphs: covers full character names (아드레이・라즈버드) whose
                   # 8 Hangul syllables are one glyph wider than the JP-derived budget.
    budget = unit["budget_px"]
    for i, ln in enumerate(ko_lines):
        px = weng.line_px(ln)
        allow = 0
        for t in TOKEN_RE.findall(ln):
            if t.isdigit():
                n = int(t)
                if 1 <= n <= len(fams):
                    allow += MARKER_ALLOWANCE_PX.get(fams[n - 1], 0)
            elif t.startswith("G:"):
                allow += 24  # preserved original glyph: conservative full cell
        total = px + allow
        if px > budget + NAME_TOL:
            rep.err("width", bid, key, line_index=i, actual_px=total,
                    text_px=px, marker_allowance_px=allow, budget_px=budget,
                    line=ln)
        elif px > budget:
            rep.warn("width_minor", bid, key, line_index=i, text_px=px,
                     budget_px=budget, over_px=px - budget, line=ln)
        elif total > budget:
            rep.warn("width_marker_pressure", bid, key, line_index=i,
                     text_px=px, marker_allowance_px=allow, budget_px=budget,
                     line=ln)

    # 6. glossary conformance (soft)
    gloss = dict(batch.get("glossary") or {})
    gloss.update(batch.get("core_names") or {})
    jp_all = unit["jp_body"] + ("\n" + jp_spk if jp_spk else "")
    ko_all = ko + ("\n" + spk if isinstance(spk, str) else "")
    for jp, koeq in gloss.items():
        if jp in jp_all and koeq not in ko_all:
            rep.warn("glossary_mismatch", bid, key, jp_term=jp, expected_ko=koeq)

    # 7. forbidden residue
    resid = sorted(set(JP_RESIDUE_RE.findall(TOKEN_RE.sub("", ko))) - ALLOWED_JP_CHARS)
    if resid:
        rep.err("jp_residue", bid, key, chars=resid)
    if "〓" in ko:
        rep.warn("unknown_glyph_char", bid, key,
                 count=ko.count("〓"), reason="〓 placeholder remaining")


def check_batch(rep, batch, out, weng):
    bid = batch["batch_id"]
    units = {u["key"]: u for u in batch["units"]}
    seen = set()
    trs = out.get("translations")
    if not isinstance(trs, list):
        rep.err("bad_output_shape", bid, None, reason="translations must be a list")
        return 0
    n_checked = 0
    for tr in trs:
        key = tr.get("key")
        if key not in units:
            rep.err("extra_key", bid, key, reason="key not in batch")
            continue
        if key in seen:
            rep.err("duplicate_key", bid, key)
            continue
        seen.add(key)
        check_unit(rep, batch, units[key], tr, weng)
        n_checked += 1
    for key in units:
        if key not in seen:
            rep.err("missing_translation", bid, key)
    return n_checked


OUT_NAME_RE = re.compile(r"^batch_(\d{4})_ko\.json$", re.IGNORECASE)


def normalize_batch_id(raw, batches, filename):
    """Map an output file's batch_id onto the canonical "batch_NNNN" key used by the
    batch files. Accepts the numeric shorthand (24 -> "batch_0024"), but only from a
    file named batch_0024_ko.json whose number agrees: glossary_batches\\ is a separate
    workflow with a colliding integer id namespace (its batch 1 is NOT tr_batches'
    batch_0001) and the same {batch_id, translations} shape, so an unanchored int
    would silently validate glossary output against the wrong batch. Its 3-digit names
    (batch_001_ko.json) fail this check. Returns None if no batch matches."""
    if isinstance(raw, str) and raw in batches:
        return raw
    if isinstance(raw, bool):  # bool subclasses int; never a batch id
        return None
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        n = int(raw.strip())
    else:
        return None
    m = OUT_NAME_RE.match(filename)
    if not m or int(m.group(1)) != n:
        return None
    cand = f"batch_{n:04d}"
    return cand if cand in batches else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batches-dir", default=DEF_BATCHES)
    ap.add_argument("--out-dir", default=DEF_OUT)
    ap.add_argument("--report", default=DEF_REPORT, help="JSON summary path")
    ap.add_argument("--violations", default=None,
                    help="violations JSONL path (default: <report>.violations.jsonl)")
    ap.add_argument("--catalog", default=DEF_CATALOG,
                    help="container_catalog.csv (global 1.72 width table lookup)")
    ap.add_argument("--font", default=DEF_FONT)
    ap.add_argument("--require-all", action="store_true",
                    help="treat batches without an output file as errors")
    args = ap.parse_args()
    violations_path = args.violations or (
        os.path.splitext(args.report)[0] + ".violations.jsonl")

    t0 = time.time()
    weng = WidthEngine(load_global_widths(args.catalog), args.font)

    batches = {}
    for path in sorted(glob.glob(os.path.join(args.batches_dir, "batch_*.json"))):
        b = json.load(open(path, encoding="utf-8"))
        batches[b["batch_id"]] = b
    if not batches:
        raise SystemExit(f"no batch files in {args.batches_dir}")

    rep = Reporter()
    checked_batches = []
    n_units_checked = 0
    out_files = sorted(glob.glob(os.path.join(args.out_dir, "batch_*_ko.json")))
    seen_bids = set()
    for path in out_files:
        try:
            out = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            rep.err("unreadable_output", os.path.basename(path), None, error=str(exc))
            continue
        raw_bid = out.get("batch_id")
        bid = normalize_batch_id(raw_bid, batches, os.path.basename(path))
        if bid is None:
            rep.err("unknown_batch_id", os.path.basename(path), None,
                    file=os.path.basename(path), got=repr(raw_bid),
                    reason='batch_id must be the batch file\'s own string id, '
                           'e.g. "batch_0024"')
            continue
        if bid != raw_bid:  # numeric shorthand (24 / "24") — check it, but flag it
            rep.warn("batch_id_not_canonical", bid, None,
                     file=os.path.basename(path), got=repr(raw_bid), normalized=bid,
                     reason='batch_id should be the string "%s", not %s'
                            % (bid, repr(raw_bid)))
        if bid in seen_bids:
            rep.err("duplicate_batch_output", bid, None, file=os.path.basename(path))
            continue
        seen_bids.add(bid)
        n_units_checked += check_batch(rep, batches[bid], out, weng)
        checked_batches.append(bid)

    not_translated = sorted(set(batches) - seen_bids)
    if args.require_all:
        for bid in not_translated:
            rep.err("missing_batch_output", bid, None)

    with open(violations_path, "w", encoding="utf-8", newline="\n") as f:
        for v in rep.violations:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    by_type = Counter()
    by_batch = defaultdict(Counter)
    for v in rep.violations:
        by_type[f"{v['severity']}:{v['type']}"] += 1
        by_batch[v["batch_id"] or "?"][v["severity"]] += 1
    summary = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "batches_defined": len(batches),
        "batch_outputs_checked": len(checked_batches),
        "batches_not_yet_translated": len(not_translated),
        "units_checked": n_units_checked,
        "errors": rep.errors,
        "warnings": rep.warnings,
        "violations_by_type": dict(sorted(by_type.items())),
        "violations_by_batch": {str(k): dict(v)
                                for k, v in sorted(by_batch.items(),
                                                   key=lambda kv: str(kv[0]))},
        "violations_file": violations_path,
        "exit_code": 0 if rep.errors == 0 else 1,
    }
    with open(args.report, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print(f"batches checked: {len(checked_batches)}/{len(batches)} "
          f"(not yet translated: {len(not_translated)}); units: {n_units_checked}")
    print(f"errors: {rep.errors}; warnings: {rep.warnings}")
    for k, v in sorted(by_type.items()):
        print(f"  {k}: {v}")
    print(f"report -> {args.report}")
    print(f"violations -> {violations_path}")
    print(f"elapsed {time.time() - t0:.1f}s")
    sys.exit(0 if rep.errors == 0 else 1)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""prepare_translation_batches.py — SO3 DC full-KO: per-batch translation work packages.

Inputs (WS\\work\\full_ko\\):
  translation_units.jsonl      20,578 units (build_inventory.py output)
  inventory_containers.json    container paths + refs (sha12 -> path lookup)
  glossary                     --glossary (default glossary_full.json, fallback
                               glossary_merged.json). Accepted shapes:
                               {"terms": {jp: ko}} | flat {jp: ko} | nested category
                               dicts of {jp: ko}. Missing glossary => batches are still
                               generated, just without a glossary subset.
  glossary_draft.json          characters+places copied into every dialogue batch
                               as core_names.

Outputs:
  tr_batches\\batch_NNNN.json  {batch_id, kind, category, containers, glossary,
                               core_names?, units:[...]}
  tr_batches_index.json        index + generation metadata + coverage/statistics

Batch assignment:
  dialogue kind = categories {spoken, other_spoken, event_text} (scene coherence).
    Units are grouped by their FIRST ref container only (dedup guarantee: every unit
    appears in exactly one batch), ordered by first-ref msgid within the container.
    Containers are walked in (archive, stream) order and accumulated until a batch
    reaches 40-80 units; container unit runs stay contiguous (only containers > 80
    units are themselves split evenly). NOTE: the "merge tiny <12-unit containers
    with the next container in the SAME archive" rule was relaxed to merge across
    archive boundaries: the real data has 589 archives with a median of ONE
    container / 8 dialogue units per archive (340 archives hold < 12 units total),
    so same-archive merging would produce 340 micro-batches. Consecutive
    (archive, stream) order still keeps adjacent scenes adjacent; each batch lists
    its containers.
  ui kind = everything else (menu_*, battle_db, ic, system, item_flat, other),
    grouped by category, ordered by (archive, stream, msgid), split evenly into
    <= 140-unit chunks (target 100-140; categories smaller than 100 stay one batch).

Per-unit fields emitted:
  key, jp_speaker (nullable), jp_body (with ⟦⟧ markers), line_count,
  pages (= jp_body ⟦P⟧ count + 1), markers (marker family list, index i = marker n=i+1),
  width_class, budget_px, per_line_px_budget, per_line_orig_px, char_budget_hint,
  has_unknown_glyph, category, ref (first ref {archive, stream, msgid}).

Width budgets ([D7-final]):
  dialogue unit budget_px = max(own_max_line_px, 480); ui = max(own_max_line_px, 48).
  per_line_orig_px = original engine px per rendered line, recomputed from the unit's
  first-ref container (glyph advance widths x 8a80 scale / 8c80 spacing state — exactly
  the build_inventory.py width engine), truncated/padded to the normalized line_count.
  per_line_px_budget[i] = max(per_line_orig_px[i], 480|48) — per-line guidance for the
  translator; the validator enforces the unit-level budget_px.
  char_budget_hint = budget_px // 22 (Korean glyph advance ~22px at 24px cell).

Glossary subset per batch: every glossary jp key found as a substring of any unit text
(jp_body + jp_speaker) in the batch, longest terms first, capped at 200/batch.

Re-running with the final glossary (glossary_full.json) simply regenerates all batch
files in place; unit partitioning is deterministic and glossary-independent.
"""
import argparse
import glob
import io
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WS = os.environ.get(
    "SO3_WS",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)
import build_inventory as bi  # noqa: E402  (parse_mclib/tokenize/WidthState reuse)

DEF_UNITS = os.path.join(FULL_KO, "translation_units.jsonl")
DEF_INV = os.path.join(FULL_KO, "inventory_containers.json")
DEF_GLOSSARY = os.path.join(FULL_KO, "glossary_full.json")
FALLBACK_GLOSSARY = os.path.join(FULL_KO, "glossary_merged.json")
DEF_DRAFT = os.path.join(FULL_KO, "glossary_draft.json")
DEF_OUT_DIR = os.path.join(FULL_KO, "tr_batches")
DEF_INDEX = os.path.join(FULL_KO, "tr_batches_index.json")

DIALOGUE_CATS = {"spoken", "other_spoken", "event_text"}
DIA_MIN, DIA_MAX = 40, 80
UI_MAX = 140
GLOSSARY_CAP = 200
KO_CHAR_PX = 22

MARKER_ALLOWANCES = {"9280": 96, "a180": 96, "a280": 144, "a380": 144}  # validator doc


def load_units(path):
    units = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                units.append(json.loads(line))
    return units


def load_glossary(path):
    """Accept {"terms":{jp:ko}} | flat {jp:ko} | nested category dicts of {jp:ko}."""
    d = json.load(open(path, encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError(f"unsupported glossary shape in {path}")
    if isinstance(d.get("terms"), dict):
        return {k: v for k, v in d["terms"].items() if isinstance(v, str)}
    if all(isinstance(v, str) for v in d.values()):
        return dict(d)
    merged = {}
    for k, v in d.items():
        if isinstance(v, dict):
            merged.update({kk: vv for kk, vv in v.items() if isinstance(vv, str)})
    if not merged:
        raise ValueError(f"unsupported glossary shape in {path}")
    return merged


def load_core_names(path):
    if not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    names = {}
    for section in ("characters", "places"):
        sec = d.get(section)
        if isinstance(sec, dict):
            names.update({k: v for k, v in sec.items() if isinstance(v, str)})
    return names


def split_even(items, max_size):
    """Even chunks of <= max_size (chunk count = ceil(n/max_size))."""
    n = len(items)
    if n <= max_size:
        return [items]
    k = math.ceil(n / max_size)
    size = math.ceil(n / k)
    return [items[i:i + size] for i in range(0, n, size)]


def compute_per_line_px(units, containers_by_sha12, ctl):
    """(unit key) -> original engine px per rendered line, from the unit's first ref.

    Reuses build_inventory's tokenizer/width engine (glyph advance widths x 8a80 scale
    / 8c80 spacing; speaker controls applied first, exactly like the inventory build).
    """
    need = defaultdict(dict)  # sha12 -> {msgid: [unit keys]}
    for u in units:
        r = u["refs"][0]
        need[r["file_sha"]].setdefault(r["msgid"], []).append(u["key"])
    out = {}
    misses = Counter()
    for sha12 in sorted(need):
        cont = containers_by_sha12.get(sha12)
        if cont is None:
            misses["container_not_found"] += len(need[sha12])
            continue
        data = open(cont["path"], "rb").read()
        hdr, segs, local_w = bi.parse_mclib(data)
        base = hdr["local_base"]
        global_w = compute_per_line_px.global_w
        seg_by_mid = {}
        for mid, off, seg in segs:
            seg_by_mid.setdefault(mid, seg)
        for mid, keys in need[sha12].items():
            seg = seg_by_mid.get(mid)
            if seg is None:
                misses["msgid_not_found"] += len(keys)
                continue
            tokens, err = bi.tokenize(seg, ctl)
            if err is not None:
                misses["untokenizable"] += len(keys)
                continue
            ws = bi.WidthState()
            c = Counter()
            spk_tokens, body_tokens, _ = bi.find_speaker_split(tokens, Counter())
            if spk_tokens:
                for t in spk_tokens:  # controls in the speaker field update width state
                    if t[0] == "g":
                        ws.advance(bi.glyph_width(t[1], base, local_w, global_w, c))
                    elif t[0] == "c":
                        ws.control(t[1], t[2])
            lines = [0]
            for t in body_tokens:
                if t[0] == "g":
                    lines[-1] += ws.advance(bi.glyph_width(t[1], base, local_w, global_w, c))
                    continue
                if t[0] == "cskip":
                    continue
                fam, op = t[1], t[2]
                ws.control(fam, op)
                if fam in (bi.NEWLINE_FAM, bi.PAGEBREAK_FAM):
                    lines.append(0)
            for key in keys:
                out[key] = lines
    return out, misses


def make_batch_unit(u, per_line_px):
    wclass = u["width_class"]
    own = u["own_max_line_px"]
    floor_px = 480 if wclass == "dialogue" else 48
    budget = max(own, floor_px)
    n_lines = u["line_count"]
    orig = list(per_line_px.get(u["key"], []))[:n_lines]
    orig += [0] * (n_lines - len(orig))
    r = u["refs"][0]
    return {
        "key": u["key"],
        "jp_speaker": u.get("jp_speaker"),
        "jp_body": u["jp_body"],
        "line_count": n_lines,
        "pages": u["jp_body"].count("⟦P⟧") + 1,
        "markers": [m["family"] for m in u.get("markers", [])],
        "width_class": wclass,
        "budget_px": budget,
        "per_line_px_budget": [max(px, floor_px) for px in orig],
        "per_line_orig_px": orig,
        "char_budget_hint": budget // KO_CHAR_PX,
        "has_unknown_glyph": bool(u.get("has_unknown_glyph")),
        "category": u["category"],
        "ref": {"archive": r["archive"], "stream": r["stream"], "msgid": r["msgid"]},
    }


def batch_glossary_subset(batch_units, glossary_items):
    """glossary_items: [(jp, ko)] sorted longest-first. Substring match on batch text."""
    blob = "\n".join(
        (u["jp_body"] + "\n" + (u["jp_speaker"] or "")) for u in batch_units)
    chars = set(blob)
    matched = []
    for jp, ko in glossary_items:
        if jp and jp[0] in chars and jp in blob:
            matched.append((jp, ko))
            if len(matched) >= GLOSSARY_CAP:
                break
    return dict(matched)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--units", default=DEF_UNITS)
    ap.add_argument("--inventory", default=DEF_INV)
    ap.add_argument("--glossary", default=DEF_GLOSSARY,
                    help="glossary json (default glossary_full.json; falls back to "
                         "glossary_merged.json; missing => no glossary subsets)")
    ap.add_argument("--draft", default=DEF_DRAFT, help="glossary_draft.json (core names)")
    ap.add_argument("--out-dir", default=DEF_OUT_DIR)
    ap.add_argument("--index", default=DEF_INDEX)
    ap.add_argument("--catalog", default=bi.CATALOG,
                    help="container_catalog.csv used to locate the global 1.72 "
                         "width row (disc-2: full_ko_d2 catalog)")
    ap.add_argument("--controls", default=None,
                    help="control table json (default: build_inventory's disc-"
                         "agnostic control_sizes_full.json)")
    args = ap.parse_args()

    t0 = time.time()
    units = load_units(args.units)
    print(f"units: {len(units)}")

    inv = json.load(open(args.inventory, encoding="utf-8"))
    containers_by_sha12 = {c["file_sha256"][:12]: c for c in inv["containers"]}
    print(f"containers in scope: {len(containers_by_sha12)}")

    glossary_path = args.glossary
    if not os.path.exists(glossary_path):
        if os.path.exists(FALLBACK_GLOSSARY):
            print(f"glossary {glossary_path} not found -> falling back to {FALLBACK_GLOSSARY}")
            glossary_path = FALLBACK_GLOSSARY
        else:
            print(f"glossary {glossary_path} not found and no fallback -> batches without glossary subset")
            glossary_path = None
    glossary = load_glossary(glossary_path) if glossary_path else {}
    glossary_items = sorted(glossary.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    core_names = load_core_names(args.draft)
    print(f"glossary: {len(glossary)} terms ({glossary_path or 'none'}); core names: {len(core_names)}")

    # --- per-line original px (first ref), via build_inventory width engine
    ctl, ctl_source, _ = bi.load_control_table(args.controls)
    global_w = b""
    import csv as _csv
    with open(args.catalog, newline="", encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            if row["version"].strip().endswith("1.72"):
                hdr, _, global_w = bi.parse_mclib(open(row["path"], "rb").read())
                break
    assert len(global_w) >= 292, "global 1.72 width table not found"
    compute_per_line_px.global_w = global_w
    print(f"control table: {ctl_source}; global widths: {len(global_w)}")
    per_line_px, px_misses = compute_per_line_px(units, containers_by_sha12, ctl)
    print(f"per-line px computed for {len(per_line_px)}/{len(units)} units "
          f"({time.time() - t0:.1f}s){'; misses: ' + str(dict(px_misses)) if px_misses else ''}")

    # --- partition
    def first_ref(u):
        return u["refs"][0]

    dialogue_units = [u for u in units if u["category"] in DIALOGUE_CATS]
    ui_units = [u for u in units if u["category"] not in DIALOGUE_CATS]
    print(f"dialogue-kind units: {len(dialogue_units)}; ui-kind units: {len(ui_units)}")

    # dialogue: group by first-ref container, order containers by (archive, stream)
    by_cont = defaultdict(list)
    for u in dialogue_units:
        by_cont[first_ref(u)["file_sha"]].append(u)
    cont_order = sorted(
        by_cont,
        key=lambda sha: (first_ref(by_cont[sha][0])["archive"],
                         first_ref(by_cont[sha][0])["stream"], sha))
    for sha in cont_order:
        by_cont[sha].sort(key=lambda u: (first_ref(u)["msgid"], u["key"]))

    dialogue_batches = []  # list of unit lists
    cur = []

    def flush():
        nonlocal cur
        if cur:
            dialogue_batches.append(cur)
            cur = []

    for sha in cont_order:
        g = by_cont[sha]
        if len(cur) + len(g) > DIA_MAX:
            # even split of the combined run: every chunk lands in [40, 80]
            # (combined > 80 here), instead of leaving a tiny pre-flush batch
            dialogue_batches.extend(split_even(cur + g, DIA_MAX))
            cur = []
            continue
        cur.extend(g)
        if len(cur) >= DIA_MIN:
            flush()
    flush()  # final tail may be < 40 (at most one such batch)

    # ui: group by category, order (archive, stream, msgid), chunks <= 140
    ui_by_cat = defaultdict(list)
    for u in ui_units:
        ui_by_cat[u["category"]].append(u)
    ui_batches = []  # (category, unit list)
    for cat in sorted(ui_by_cat):
        g = sorted(ui_by_cat[cat],
                   key=lambda u: (first_ref(u)["archive"], first_ref(u)["stream"],
                                  first_ref(u)["msgid"], u["key"]))
        for chunk in split_even(g, UI_MAX):
            ui_batches.append((cat, chunk))

    # --- emit
    os.makedirs(args.out_dir, exist_ok=True)
    for stale in glob.glob(os.path.join(args.out_dir, "batch_*.json")):
        os.remove(stale)

    index_batches = []
    seen_keys = set()
    n_gloss_total = 0
    batch_no = 0

    def emit(kind, category, batch_units_raw):
        nonlocal batch_no, n_gloss_total
        batch_no += 1
        batch_id = f"batch_{batch_no:04d}"
        bunits = [make_batch_unit(u, per_line_px) for u in batch_units_raw]
        for u in batch_units_raw:
            assert u["key"] not in seen_keys, f"unit {u['key']} assigned twice"
            seen_keys.add(u["key"])
        gsub = batch_glossary_subset(bunits, glossary_items) if glossary_items else {}
        n_gloss_total += len(gsub)
        conts = []
        seen_c = set()
        for u in batch_units_raw:
            r = first_ref(u)
            ck = (r["archive"], r["stream"], r["file_sha"])
            if ck not in seen_c:
                seen_c.add(ck)
                conts.append({"archive": r["archive"], "stream": r["stream"],
                              "file_sha": r["file_sha"]})
        rec = {"batch_id": batch_id, "kind": kind, "category": category,
               "n_units": len(bunits), "containers": conts, "glossary": gsub}
        if kind == "dialogue" and core_names:
            rec["core_names"] = core_names
        rec["units"] = bunits
        path = os.path.join(args.out_dir, batch_id + ".json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        index_batches.append({
            "batch_id": batch_id, "file": os.path.basename(path), "kind": kind,
            "category": category, "n_units": len(bunits), "n_glossary": len(gsub),
            "archives": sorted({c["archive"] for c in conts}),
        })

    for chunk in dialogue_batches:
        cat = Counter(u["category"] for u in chunk).most_common(1)[0][0]
        emit("dialogue", cat, chunk)
    for cat, chunk in ui_batches:
        emit("ui", cat, chunk)

    assert seen_keys == {u["key"] for u in units}, "coverage: every unit exactly once"

    # --- stats/report
    sizes = defaultdict(list)
    for b in index_batches:
        sizes[b["kind"]].append(b["n_units"])

    def hist(vals, edges):
        h = Counter()
        for v in vals:
            for lo, hi in edges:
                if lo <= v <= hi:
                    h[f"{lo}-{hi}"] += 1
                    break
        return {f"{lo}-{hi}": h[f"{lo}-{hi}"] for lo, hi in edges if h[f"{lo}-{hi}"]}

    dia_hist = hist(sizes.get("dialogue", []),
                    [(1, 11), (12, 39), (40, 60), (61, 80), (81, 10 ** 9)])
    ui_hist = hist(sizes.get("ui", []),
                   [(1, 49), (50, 99), (100, 120), (121, 140), (141, 10 ** 9)])

    index = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "units_file": args.units,
        "glossary_source": glossary_path,
        "core_names_source": args.draft if core_names else None,
        "n_units": len(units),
        "n_batches": len(index_batches),
        "batches_by_kind": {k: len(v) for k, v in sorted(sizes.items())},
        "units_by_kind": {k: sum(v) for k, v in sorted(sizes.items())},
        "size_histogram": {"dialogue": dia_hist, "ui": ui_hist},
        "budget_rule": {
            "dialogue": "budget_px = max(own_max_line_px, 480)",
            "ui": "budget_px = max(own_max_line_px, 48)",
            "per_line_px_budget": "max(per_line_orig_px[i], 480|48); guidance only — "
                                  "validator enforces unit budget_px",
            "char_budget_hint": f"budget_px // {KO_CHAR_PX}",
        },
        "marker_allowances_px": {**MARKER_ALLOWANCES, "default": 0},
        "marker_allowances_note": "validator adds these px per dynamic-insert marker on "
                                  "a line before comparing against budget_px (runtime "
                                  "inserts add width: 9280/a180 registry/party-slot "
                                  "strings ~4 glyphs = 96px, a280/a380 item names "
                                  "~6 glyphs = 144px; color/payload markers add 0)",
        "glossary_terms_total": len(glossary),
        "glossary_matches_total": n_gloss_total,
        "batches": index_batches,
    }
    with open(args.index, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    print(f"\n== batches: {len(index_batches)} "
          f"(dialogue {len(sizes.get('dialogue', []))}, ui {len(sizes.get('ui', []))})")
    print(f"dialogue size histogram: {dia_hist}")
    print(f"ui size histogram: {ui_hist}")
    print(f"glossary matches embedded: {n_gloss_total} "
          f"(cap {GLOSSARY_CAP}/batch, longest-first)")
    print(f"index -> {args.index}")
    print(f"elapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

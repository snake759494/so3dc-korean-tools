# -*- coding: utf-8 -*-
"""Turn relabel_worklist.json into translation-unit jsonl files per disc, then
the caller runs prepare_translation_batches on each. Each worklist entry carries
the full new unit record (+ old translation as a porting hint, which we embed
into the unit as 'old_korean_hint' so batch files carry it through)."""
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
WL = os.path.join(FULL_KO, "relabel_worklist.json")

wl = json.load(open(WL, encoding="utf-8"))
for disc, out_name in (("disc1", os.path.join(FULL_KO, "relabel_units_d1.jsonl")),
                        ("disc2", os.path.join(FULL_KO, "..", "full_ko_d2", "relabel_units_d2.jsonl"))):
    rows = wl.get(disc) or []
    n = 0
    with open(out_name, "w", encoding="utf-8") as f:
        for e in rows:
            u = dict(e.get("unit") or {})
            if "key" not in u:
                u["key"] = e["new_key"]
            ot = e.get("old_translation")
            if ot:
                u["port_hint"] = {"korean": ot.get("korean"),
                                   "speaker_korean": ot.get("speaker_korean"),
                                   "jp_body_changed": e.get("jp_body_new") != e.get("jp_body_old")}
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
            n += 1
    print(disc, "->", out_name, n, "units")

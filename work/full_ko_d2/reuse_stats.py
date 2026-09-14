# -*- coding: utf-8 -*-
"""How many disc-2 units are already translated by disc-1 tr_out (text_key match)?"""
import glob, io, json, os, sys
from collections import Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
D2 = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(os.path.dirname(D2))
FULL_KO = os.path.join(WS, "work", "full_ko") if not os.path.exists(os.path.join(WS, "full_ko")) else os.path.join(WS, "full_ko")
# resolve: D2 = ...\work\full_ko_d2 → FULL_KO = ...\work\full_ko
FULL_KO = os.path.join(os.path.dirname(D2), "full_ko")

tr = {}
for p in glob.glob(os.path.join(FULL_KO, "tr_out", "batch_*_ko.json")):
    for t in json.load(open(p, encoding="utf-8"))["translations"]:
        tr[t["key"]] = True
print("disc-1 translated keys:", len(tr))

total = 0
reused = 0
new_by_cat = Counter()
reused_by_cat = Counter()
new_chars = 0
with open(os.path.join(D2, "translation_units.jsonl"), encoding="utf-8") as f:
    for line in f:
        u = json.loads(line)
        total += 1
        cat = u.get("category", "?")
        if u["key"] in tr:
            reused += 1
            reused_by_cat[cat] += 1
        else:
            new_by_cat[cat] += 1
            new_chars += len(u.get("jp_body", ""))
print("d2 units:", total)
print("reused from disc 1:", reused, "(%.1f%%)" % (100 * reused / total))
print("NEW units needing translation:", total - reused, "chars:", new_chars)
print("new by category:", dict(new_by_cat.most_common()))

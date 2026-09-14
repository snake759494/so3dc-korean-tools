import json, os
WS = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(os.path.join(WS, "glossary_batches.json"), encoding="utf-8"))
bd = os.path.join(WS, "glossary_batches")
os.makedirs(bd, exist_ok=True)
for b in d["batches"]:
    name = "batch_%03d.json" % b["batch_id"]
    json.dump(b, open(os.path.join(bd, name), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("wrote", len(d["batches"]), "files")

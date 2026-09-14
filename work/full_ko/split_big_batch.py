# -*- coding: utf-8 -*-
"""Split an oversized batch into halves as fresh batch files (new numbers), so
each translator agent's output fits the 64k-token cap. The plan builder keys off
text_key, so the halves' _ko outputs are picked up without renaming.

  python split_big_batch.py 281 351 352   # 281 -> batches 351 (first half) + 352
"""
import json, os, sys
FULL_KO = os.path.dirname(os.path.abspath(__file__))
BATCHES = os.path.join(FULL_KO, "tr_batches")

src_n, a, b = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
src = json.load(open(os.path.join(BATCHES, "batch_%04d.json" % src_n), encoding="utf-8"))
units = src["units"]
mid = (len(units) + 1) // 2
halves = {a: units[:mid], b: units[mid:]}
for num, us in halves.items():
    d = dict(src)
    d["batch_id"] = num
    d["units"] = us
    d["split_from"] = src_n
    json.dump(d, open(os.path.join(BATCHES, "batch_%04d.json" % num), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("wrote batch_%04d.json (%d units, from %d)" % (num, len(us), src_n))

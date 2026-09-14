# -*- coding: utf-8 -*-
"""Split tr_batches_r2/batch_0023.json into batches 29 and 30 (halves)."""
import json, os
D2 = os.path.dirname(os.path.abspath(__file__))
B = os.path.join(D2, "tr_batches_r2")
src = json.load(open(os.path.join(B, "batch_0023.json"), encoding="utf-8"))
units = src["units"]
mid = (len(units) + 1) // 2
for num, us in ((29, units[:mid]), (30, units[mid:])):
    d = dict(src)
    d["batch_id"] = num
    d["units"] = us
    d["split_from"] = 23
    json.dump(d, open(os.path.join(B, "batch_%04d.json" % num), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("batch_%04d.json (%d units)" % (num, len(us)))

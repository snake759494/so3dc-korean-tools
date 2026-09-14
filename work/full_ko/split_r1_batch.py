# -*- coding: utf-8 -*-
"""Split tr_batches_r1/batch_0021.json into batches 27 and 28 (halves)."""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.join(HERE, "tr_batches_r1")
src = json.load(open(os.path.join(B, "batch_0021.json"), encoding="utf-8"))
units = src["units"]
mid = (len(units) + 1) // 2
for num, us in ((27, units[:mid]), (28, units[mid:])):
    d = dict(src)
    d["batch_id"] = num
    d["units"] = us
    d["split_from"] = 21
    json.dump(d, open(os.path.join(B, "batch_%04d.json" % num), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("batch_%04d.json (%d units)" % (num, len(us)))

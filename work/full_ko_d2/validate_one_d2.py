# -*- coding: utf-8 -*-
"""Disc-2 single-batch validator (mirrors full_ko\\validate_one.py with d2 paths).

  python validate_one_d2.py 5   -> tr_batches/batch_0005.json vs tr_out/batch_0005_ko.json
Exit 0 = no errors. 1 = errors. 2 = missing output.
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
D2 = os.path.dirname(os.path.abspath(__file__))
FULL_KO = os.path.join(os.path.dirname(D2), "full_ko")
sys.path.insert(0, FULL_KO)

import validate_translations as V  # noqa: E402

BATCHES = os.path.join(D2, "tr_batches")
OUT = os.path.join(D2, "tr_out")
CATALOG = os.path.join(D2, "container_catalog.csv")
FONT = r"D:\ps2\NanumSquareNeo-cBd.ttf"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_one_d2.py <batch number>")
    n = int(sys.argv[1])
    bfile = os.path.join(BATCHES, "batch_%04d.json" % n)
    ofile = os.path.join(OUT, "batch_%04d_ko.json" % n)
    if not os.path.exists(bfile):
        raise SystemExit("no such batch file: " + bfile)
    if not os.path.exists(ofile):
        print("MISSING OUTPUT: " + ofile)
        return 2

    batch = json.load(open(bfile, encoding="utf-8"))
    out = json.load(open(ofile, encoding="utf-8"))

    weng = V.WidthEngine(V.load_global_widths(CATALOG), FONT)
    rep = V.Reporter()
    V.check_batch(rep, batch, out, weng)

    errs = [v for v in rep.violations if v["severity"] == "error"]
    warns = [v for v in rep.violations if v["severity"] == "warning"]
    print("batch %04d: errors=%d warnings=%d" % (n, len(errs), len(warns)))
    for v in errs[:40]:
        print("  E %s key=%s %s" % (v["type"], v["key"],
                                    json.dumps(v["detail"], ensure_ascii=False)[:300]))
    if len(errs) > 40:
        print("  ... +%d more errors" % (len(errs) - 40))
    wt = {}
    for v in warns:
        wt[v["type"]] = wt.get(v["type"], 0) + 1
    if wt:
        print("  warnings by type: " + json.dumps(wt, ensure_ascii=False))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

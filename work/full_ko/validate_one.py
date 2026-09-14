# -*- coding: utf-8 -*-
"""Fast single-batch validator wrapper.

validate_translations.py walks every batch on every run; on a 4-core box with
many concurrent translator agents that O(N) sweep dominates. This checks ONE
batch and prints a compact verdict, so an agent's self-check loop stays O(1).

  python validate_one.py 24            -> checks tr_batches/batch_0024.json
                                          against tr_out/batch_0024_ko.json
Exit 0 = no errors (warnings allowed). Exit 1 = errors. Exit 2 = missing output.
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)

import validate_translations as V  # noqa: E402

WS = r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2"
BATCHES = os.path.join(FULL_KO, "tr_batches")
OUT = os.path.join(FULL_KO, "tr_out")
CATALOG = os.path.join(WS, r"work\mclib_all_decode\container_catalog.csv")
FONT = r"D:\ps2\NanumSquareNeo-cBd.ttf"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_one.py <batch number>")
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

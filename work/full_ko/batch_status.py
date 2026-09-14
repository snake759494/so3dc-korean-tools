# -*- coding: utf-8 -*-
"""Status sweep: which batches are done/clean, which need work.

Writes batch_status.json = {"clean": [...], "errors": {n: count}, "missing": [...]}
Prints a summary. Fast: one WidthEngine, one pass.
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)
import validate_translations as V  # noqa: E402

WS = os.environ.get("SO3_WS", os.path.dirname(os.path.dirname(FULL_KO)))
BATCHES = os.path.join(FULL_KO, "tr_batches")
OUT = os.path.join(FULL_KO, "tr_out")
CATALOG = os.path.join(WS, r"work\mclib_all_decode\container_catalog.csv")
FONT = os.environ.get("SO3_FONT", r"D:\ps2\NanumSquareNeo-cBd.ttf")
STATUS = os.path.join(FULL_KO, "batch_status.json")


def main():
    weng = V.WidthEngine(V.load_global_widths(CATALOG), FONT)
    clean, errors, missing, unreadable = [], {}, [], []
    etypes = {}
    n = 0
    while True:
        n += 1
        bfile = os.path.join(BATCHES, "batch_%04d.json" % n)
        if not os.path.exists(bfile):
            break
        ofile = os.path.join(OUT, "batch_%04d_ko.json" % n)
        if not os.path.exists(ofile):
            missing.append(n)
            continue
        try:
            batch = json.load(open(bfile, encoding="utf-8"))
            out = json.load(open(ofile, encoding="utf-8"))
        except Exception as exc:
            unreadable.append([n, str(exc)[:80]])
            continue
        rep = V.Reporter()
        try:
            V.check_batch(rep, batch, out, weng)
        except Exception as exc:
            unreadable.append([n, "check crashed: " + str(exc)[:80]])
            continue
        errs = [v for v in rep.violations if v["severity"] == "error"]
        if errs:
            errors[n] = len(errs)
            for v in errs:
                etypes[v["type"]] = etypes.get(v["type"], 0) + 1
        else:
            clean.append(n)
    total = n - 1
    json.dump({"total": total, "clean": clean, "errors": errors,
               "missing": missing, "unreadable": unreadable,
               "error_types": etypes},
              open(STATUS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("total batches: %d" % total)
    print("  clean       : %d" % len(clean))
    print("  with errors : %d  (%s)" % (len(errors), json.dumps(etypes, ensure_ascii=False)))
    print("  missing     : %d" % len(missing))
    print("  unreadable  : %d" % len(unreadable))
    print("pending (need agent): %d" % (len(errors) + len(missing) + len(unreadable)))
    print("-> " + STATUS)


if __name__ == "__main__":
    main()

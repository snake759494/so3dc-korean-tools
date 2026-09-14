# -*- coding: utf-8 -*-
"""v1.2.1 sanity proof: relabel-left-scope (now non-JP) messages that previously
carried garbage translations (코오/요№/유하 family) must be (a) absent from the
patch plan and (b) byte-identical to the original in the final patched ISO, at
EVERY on-disc occurrence.

Works for either disc via CLI args. Fail-closed: any mismatch -> exit 1.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import struct
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = Path(__file__).resolve().parent
sys.path.insert(0, str(FULL_KO))
import so3_full_patch as P  # noqa: E402


def logical(seg: bytes) -> bytes:
    return seg.rstrip(b"\0") + b"\0"


def read_slz(handle, offset: int) -> bytes:
    handle.seek(offset)
    header = handle.read(16)
    if header[:3] != b"SLZ":
        raise SystemExit(f"SLZ header missing at 0x{offset:X}")
    mode, comp, unpacked = header[3], struct.unpack_from("<I", header, 4)[0], \
        struct.unpack_from("<I", header, 8)[0]
    payload = handle.read(comp)
    return P.decompress_slz_payload(payload, mode, unpacked)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--original", required=True)
    ap.add_argument("--patched", required=True)
    ap.add_argument("--archive-manifest", required=True)
    ap.add_argument("--stream-manifest", required=True)
    ap.add_argument("--keys", required=True, help="comma-separated nonjp text_keys")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    keys = set(args.keys.split(","))
    inv = json.load(open(args.inventory, encoding="utf-8"))
    conts = inv["containers"] if isinstance(inv, dict) else inv
    plan = json.load(open(args.plan, encoding="utf-8"))["unique"]

    # archive extents
    arc = {}
    with open(args.archive_manifest, newline="", encoding="utf-8-sig") as h:
        for row in csv.DictReader(h):
            arc[int(row["id"])] = (int(row["iso_offset"]), int(row["bytes"]))
    # stream source offsets within archive
    srow = {}
    with open(args.stream_manifest, newline="", encoding="utf-8-sig") as h:
        for row in csv.DictReader(h):
            if int(row["depth"]) != 0:
                continue
            srow[(int(row["archive_id"]), int(row["stream_id"]))] = int(row["source_offset"])

    # collect targets: container -> {msgid} plus refs
    targets = []  # (sha, msgid, msg_sha, [(archive, stream), ...])
    for c in conts:
        hits = [(m["id"], m.get("sha")) for m in c.get("messages", [])
                if m.get("text_key") in keys]
        if not hits:
            continue
        refs = [(r["archive"], r["stream"]) for r in c["refs"]]
        for mid, msha in hits:
            targets.append((c["file_sha256"], mid, msha, refs))

    n_plan_viol = 0
    for sha, mid, _, _ in targets:
        if str(mid) in plan.get(sha, {}):
            print(f"PLAN VIOLATION: {sha[:12]}:{mid} present in plan")
            n_plan_viol += 1

    report = {"keys": sorted(keys), "targets": len(targets), "plan_violations": n_plan_viol,
              "occurrences_checked": 0, "byte_mismatches": 0,
              "orig_sha_mismatches": 0, "details": []}

    # group by (archive, stream) so each container extraction is done once
    by_stream: dict[tuple[int, int], list[tuple[str, int, str]]] = {}
    for sha, mid, msha, refs in targets:
        for ref in refs:
            by_stream.setdefault(ref, []).append((sha, mid, msha))

    cache_orig: dict[int, list] = {}
    cache_pat: dict[int, list] = {}
    ho = open(args.original, "rb")
    hp = open(args.patched, "rb")
    n_checked = n_bad = n_shabad = 0
    for (aid, sid), items in sorted(by_stream.items()):
        a_off, a_len = arc[aid]
        src = srow[(aid, sid)]
        if aid not in cache_orig:
            cache_orig.clear(); cache_pat.clear()  # streams sorted -> archives grouped
            ho.seek(a_off)
            cache_orig[aid] = P.parse_archive_layout(ho.read(a_len))
            hp.seek(a_off)
            cache_pat[aid] = P.parse_archive_layout(hp.read(a_len))
        p, r, cidx = P.locate_stream(cache_orig[aid], src)
        mo = cache_orig[aid][p].rows[r].members[cidx]
        mp = cache_pat[aid][p].rows[r].members[cidx]
        do = read_slz(ho, a_off + mo.offset)
        dp = read_slz(hp, a_off + mp.offset)
        po_, pp_ = P.Mclib.parse(do), P.Mclib.parse(dp)
        seg_o = {m: po_.segments[off] for m, off in po_.rows}
        seg_p = {m: pp_.segments[off] for m, off in pp_.rows}
        if hashlib.sha256(do).hexdigest() != items[0][0]:
            print(f"WARN: original container sha mismatch at {aid}:{sid}")
        for sha, mid, msha in items:
            n_checked += 1
            lo, lp = logical(seg_o[mid]), logical(seg_p[mid])
            if msha and hashlib.sha256(seg_o[mid]).hexdigest() != msha and \
               hashlib.sha256(lo).hexdigest() != msha:
                n_shabad += 1
                report["details"].append({"where": f"{aid}:{sid}:{mid}",
                                          "issue": "orig segment sha != inventory sha"})
            if lo != lp:
                n_bad += 1
                report["details"].append({"where": f"{aid}:{sid}:{mid}",
                                          "issue": "patched bytes differ",
                                          "orig": lo.hex()[:80], "pat": lp.hex()[:80]})
    report["occurrences_checked"] = n_checked
    report["byte_mismatches"] = n_bad
    report["orig_sha_mismatches"] = n_shabad
    json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = (n_plan_viol == 0 and n_bad == 0)
    print(f"targets(sha,mid)={len(targets)} plan_violations={n_plan_viol} "
          f"occurrences_checked={n_checked} byte_mismatches={n_bad} "
          f"orig_sha_warn={n_shabad} -> {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Disc-1 -> Disc-2 repaint transfer matching + disc-2-only texture detection.

1. sha256 every decompressed FIS member on both discs (decoded\ files).
2. For each of the 8 disc-1 repainted targets, find disc-2 members whose
   ORIGINAL decompressed bytes are byte-identical; check whether the disc-1
   patched .slz fits the disc-2 allocation and whether next_rel must be
   re-stamped.
3. Disc-2 members whose decompressed sha does not exist anywhere on disc 1
   = disc-2-only textures -> flag in catalog (d2_only) for dedicated sheets.

Outputs (work\img_ko_d2\):
  transfer_plan.json  (new_jp_targets filled after visual review)
  transfer_match_report.txt
"""
from __future__ import annotations
import csv, hashlib, json, struct
from collections import defaultdict
from pathlib import Path

WS = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\work")
D1 = WS / "full_unpack" / "disc1"
D2 = WS / "full_unpack" / "disc2"
KO1 = WS / "img_ko"
OUT = WS / "img_ko_d2"

TARGETS = [(40, 25), (42, 36), (44, 41), (2254, 38031),
           (47, 49), (47, 52), (47, 55), (47, 56)]


def fis_rows(disc):
    with open(disc / "manifests" / "stream_manifest.csv", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["magic_text"] == "FIS"]


def sha_map(disc, rows):
    """(archive,stream) -> sha256 of decompressed member bytes."""
    m = {}
    for r in rows:
        b = (disc / r["path"]).read_bytes()
        m[(int(r["archive_id"]), int(r["stream_id"]))] = hashlib.sha256(b).hexdigest()
    return m


def main():
    r1, r2 = fis_rows(D1), fis_rows(D2)
    idx1 = {(int(r["archive_id"]), int(r["stream_id"])): r for r in r1}
    idx2 = {(int(r["archive_id"]), int(r["stream_id"])): r for r in r2}
    print(f"disc1 FIS={len(r1)}  disc2 FIS={len(r2)}")
    s1, s2 = sha_map(D1, r1), sha_map(D2, r2)
    by_sha2 = defaultdict(list)
    for k, h in s2.items():
        by_sha2[h].append(k)

    # ---- labels for the 8 targets from disc-1 repaint report ----
    rep = json.load(open(KO1 / "repaint_report.json", encoding="utf-8"))
    labels = {tuple(map(int, k.split(":"))): v["label"] for k, v in rep.items()}

    reusable, missing = [], []
    lines = []
    for key in TARGETS:
        a, s = key
        h = s1[key]
        d1r = idx1[key]
        label = labels.get(key, f"{a}:{s} {d1r['fis_name'][:6]}")
        slz = (KO1 / "patched" / f"{a}_{s}.slz").read_bytes()
        smode = slz[3]
        scomp, sunp, snrel = struct.unpack_from("<III", slz, 4)
        patched_size = 16 + scomp
        matches = sorted(by_sha2.get(h, []))
        if not matches:
            missing.append({"disc1_label": label, "disc1_key": f"{a}:{s}",
                            "sha256": h, "note": "no byte-identical member on disc 2"})
            lines.append(f"[MISS] {label}  sha={h[:16]}...  -> no disc-2 match")
            continue
        for k2 in matches:
            a2, s2_ = k2
            r = idx2[k2]
            nrel2 = int(r["next_rel"] or 0)
            comp2, unp2, mode2 = int(r["compressed"]), int(r["unpacked"]), int(r["mode"])
            footprint = nrel2 if nrel2 else 16 + comp2
            alloc_src = "next_rel" if nrel2 else "chain_end(16+compressed)"
            fits = patched_size <= footprint
            if snrel == nrel2:
                nr_action = "keep (next_rel identical)"
            else:
                nr_action = f"re-stamp next_rel {snrel} -> {nrel2}"
            reusable.append({
                "disc1_label": label, "disc1_key": f"{a}:{s}",
                "d2_archive": a2, "d2_stream": s2_,
                "d2_iso_offset": int(r["iso_offset"]),
                "d2_path": r["path"],
                "d2_allocation": footprint, "d2_allocation_source": alloc_src,
                "d2_next_rel": nrel2, "d2_compressed": comp2, "d2_unpacked": unp2,
                "d2_mode": mode2,
                "patched_slz_size": patched_size,
                "patched_mode": smode, "patched_unpacked": sunp,
                "fits": fits, "slack": footprint - patched_size,
                "mode_unpacked_ok": (smode == mode2 and sunp == unp2),
                "next_rel_action": nr_action,
            })
            lines.append(
                f"[OK ] {label}  -> d2 {a2}:{s2_} iso=0x{int(r['iso_offset']):X} "
                f"alloc={footprint}({alloc_src}) patched={patched_size} "
                f"fits={fits} slack={footprint-patched_size}  {nr_action}")

    # ---- disc-2-only textures ----
    d1_shas = set(s1.values())
    cat2 = json.load(open(OUT / "texture_catalog.json", encoding="utf-8"))
    n_only = 0
    for c in cat2:
        h = s2[(c["archive_id"], c["stream_id"])]
        c["src_sha256"] = h
        c["d2_only"] = h not in d1_shas
        n_only += c["d2_only"]
    json.dump(cat2, open(OUT / "texture_catalog.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    only = [c for c in cat2 if c["d2_only"]]
    uniq_only = {}
    for c in only:
        uniq_only.setdefault(c["src_sha256"], []).append(c)
    lines.append(f"\ndisc2-only members: {n_only}  (unique by src sha: {len(uniq_only)})")
    for h, ms in sorted(uniq_only.items(), key=lambda kv: (kv[1][0]['archive_id'], kv[1][0]['stream_id'])):
        c = ms[0]
        lines.append(f"  d2-only {c['archive_id']}:{c['stream_id']} {c['name']!r} "
                     f"{c['width']}x{c['height']} {c['bpp']}b x{len(ms)} "
                     f"e={c['edge_score']} cov={c['coverage']}")

    plan = {"reusable": reusable, "missing_on_d2": missing,
            "new_jp_targets": []}   # filled after visual review of new_* sheets
    json.dump(plan, open(OUT / "transfer_plan.json", "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    report = "\n".join(lines)
    (OUT / "transfer_match_report.txt").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()

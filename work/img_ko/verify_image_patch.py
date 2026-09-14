#!/usr/bin/env python3
"""Independently verify the SO3 DC image-patched ISO.

Checks:
  1. output size == input size (== 4,689,854,464).
  2. every byte OUTSIDE the 8 member extents is byte-identical to the INPUT
     v1.0.0 ISO (streamed compare); all changed bytes fall inside the 8 extents.
  3. each patched extent: the SLZ decompresses exactly to the patched member
     bytes; FIS header/CLUT unchanged vs the original member; footprint and
     next_rel unchanged (SLZ chain consistent).
  4. collision check: the FIS member is identical in the ORIGINAL untouched ISO
     and the v1.0.0 text-patched ISO -> the text patch never touched these 8
     members, so overwriting them cannot corrupt the text patch.
  5. SHA-256 of the output ISO.
Writes verify_image_report.json.  Fail-closed (asserts).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np

WS = Path(__file__).resolve().parent
_PUBLISH = WS.parent.parent / "publish" / "so3dc-korean-tools"
if not (_PUBLISH / "so3_repack.py").exists():
    _PUBLISH = WS.parent.parent  # repo checkout: the clone root IS the workspace
sys.path.insert(0, str(WS))
sys.path.insert(0, str(_PUBLISH))
import fis_repaint as F  # noqa: E402
from so3_repack import decompress_slz_payload, read_slz_member  # noqa: E402

INPUT_ISO = Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full.iso")           # v1.0.0 text patch
OUTPUT_ISO = Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full_Img.iso")      # image patch applied
ORIG_ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
PATCHED = WS / "patched"
EXPECT_SIZE = 4689854464

TARGETS = [(40, 25), (42, 36), (44, 41), (2254, 38031),
           (47, 49), (47, 52), (47, 55), (47, 56)]


def load_targets(transfer_plan: Path | None) -> list[dict]:
    """[{key, src, expect_off}]; default = disc-1 (src == key).

    Independent re-implementation of the apply tool's target expansion so the
    verifier does not share its code path.
    """
    if transfer_plan is None:
        return [{"key": k, "src": k, "expect_off": None} for k in TARGETS]
    plan = json.load(open(transfer_plan, encoding="utf-8"))
    out = []
    for e in plan["reusable"]:
        src = tuple(int(x) for x in e["disc1_key"].split(":"))
        assert src in TARGETS, f"unknown transfer source {src}"
        out.append({"key": (int(e["d2_archive"]), int(e["d2_stream"])), "src": src,
                    "expect_off": int(e["d2_iso_offset"])})
    assert {t["src"] for t in out} == set(TARGETS), "transfer plan misses sources"
    assert len({t["key"] for t in out}) == len(out), "duplicate transfer targets"
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def members_from_catalog(catalog_path: Path, keys: list[tuple[int, int]]):
    cat = json.load(open(catalog_path, encoding="utf-8"))
    out = {}
    for c in cat:
        k = (c["archive_id"], c["stream_id"])
        if k in keys:
            out[k] = {"iso_offset": c["iso_offset"], "next_rel": c["next_rel"],
                      "compressed": c["compressed"], "unpacked": c["unpacked"]}
    missing = [k for k in keys if k not in out]
    assert not missing, f"catalog missing members: {missing}"
    return out


def streamed_outside_compare(in_iso, out_iso, extents):
    """Stream-compare input vs output; classify every differing byte as inside
    or outside an extent.  extents: sorted list of (start, end)."""
    starts = np.array([s for s, _ in extents], dtype=np.int64)
    ends = np.array([e for _, e in extents], dtype=np.int64)
    CHUNK = 1 << 24  # 16 MiB
    pos = 0
    total_changed = 0
    outside_changed = 0
    per_extent = {i: 0 for i in range(len(extents))}
    outside_examples = []
    with in_iso.open("rb") as fa, out_iso.open("rb") as fb:
        while True:
            a = fa.read(CHUNK)
            b = fb.read(CHUNK)
            if not a and not b:
                break
            if len(a) != len(b):
                raise AssertionError("length mismatch during stream compare")
            if a != b:
                aa = np.frombuffer(a, np.uint8)
                bb = np.frombuffer(b, np.uint8)
                loc = np.nonzero(aa != bb)[0].astype(np.int64)
                abspos = loc + pos
                idx = np.searchsorted(starts, abspos, side="right") - 1
                inside = (idx >= 0) & (abspos < ends[np.clip(idx, 0, len(ends) - 1)])
                total_changed += int(abspos.size)
                out_mask = ~inside
                outside_changed += int(out_mask.sum())
                if out_mask.any() and len(outside_examples) < 10:
                    outside_examples.extend(int(x) for x in abspos[out_mask][:10])
                for i in np.unique(idx[inside]):
                    per_extent[int(i)] += int((idx[inside] == i).sum())
            pos += len(a)
    return {"total_changed": total_changed, "outside_changed": outside_changed,
            "per_extent": per_extent, "outside_examples": outside_examples}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, default=INPUT_ISO,
                    help="text(/name)-patched input ISO")
    ap.add_argument("--output", type=Path, default=OUTPUT_ISO,
                    help="image-patched output ISO under verification")
    ap.add_argument("--orig", type=Path, default=ORIG_ISO,
                    help="untouched original ISO (collision check)")
    ap.add_argument("--catalog", type=Path, default=WS / "texture_catalog.json")
    ap.add_argument("--transfer-plan", type=Path, default=None,
                    help="disc-2 transfer_plan.json (default: disc-1 targets)")
    ap.add_argument("--expect-size", type=int, default=EXPECT_SIZE)
    ap.add_argument("--report", type=Path, default=WS / "verify_image_report.json")
    args = ap.parse_args()
    input_iso, output_iso, orig_iso = args.input, args.output, args.orig
    expect_size = args.expect_size
    targets = load_targets(args.transfer_plan)

    members = members_from_catalog(args.catalog, [t["key"] for t in targets])
    report = {"input_iso": str(input_iso), "output_iso": str(output_iso),
              "orig_untouched_iso": str(orig_iso)}
    ok = True

    # (1) size
    out_size = output_iso.stat().st_size
    in_size = input_iso.stat().st_size
    report["output_size"] = out_size
    report["size_ok"] = (out_size == expect_size == in_size)
    ok &= report["size_ok"]

    # extents = [start, start+footprint)
    ext_list = []
    ext_keys = []
    for t in targets:
        key = t["key"]
        d = members[key]
        off = d["iso_offset"]
        if t["expect_off"] is not None:
            assert off == t["expect_off"], \
                f"{key}: catalog iso_offset {off} != transfer plan {t['expect_off']}"
        _, slz = read_slz_member(input_iso, off)  # original on-disk header
        footprint = slz["next_rel"] if slz["next_rel"] else (16 + slz["compressed"])
        ext_list.append((off, off + footprint))
        ext_keys.append((t, off, footprint, slz["next_rel"]))
    order = sorted(range(len(ext_list)), key=lambda i: ext_list[i][0])
    ext_sorted = [ext_list[i] for i in order]
    keys_sorted = [ext_keys[i] for i in order]

    # (2) streamed outside compare
    cmp = streamed_outside_compare(input_iso, output_iso, ext_sorted)
    report["stream_compare"] = {
        "total_changed_bytes": cmp["total_changed"],
        "changed_outside_extents": cmp["outside_changed"],
        "outside_examples": cmp["outside_examples"],
    }
    report["outside_identical"] = (cmp["outside_changed"] == 0)
    ok &= report["outside_identical"]

    # (3)+(4) per-member extent verification
    per = []
    sum_ext_changed = 0
    for j, (t, off, footprint, onext) in enumerate(keys_sorted):
        a, s = t["key"]
        sa, ss = t["src"]
        bin_ = (PATCHED / f"{sa}_{ss}.bin").read_bytes()
        # decode output member
        odec, oslz = read_slz_member(output_iso, off)
        slz_decodes = (odec == bin_)
        # footprint (allocation) unchanged: the member start is fixed, next_rel is
        # preserved, and the new member fits within the original allocation; the
        # allocation boundary itself is proven unchanged by the outside-identical
        # streamed compare (the next member / trailing bytes never moved).
        used = 16 + oslz["compressed"]
        footprint_ok = (used <= footprint)
        next_rel_ok = (oslz["next_rel"] == onext)
        # FIS header/CLUT unchanged vs original member (from the text patch)
        origdec, _ = read_slz_member(input_iso, off)
        ot = F.FISTexture(origdec); pt = F.FISTexture(odec)
        hdr_ok = origdec[:ot.pixel_start] == odec[:pt.pixel_start]
        # collision: member identical in ORIG untouched vs the text-patched input
        orig_untouched, _ = read_slz_member(orig_iso, off)
        text_untouched = (orig_untouched == origdec)
        # changed bytes within this extent (from stream compare)
        ec = cmp["per_extent"][j]
        sum_ext_changed += ec
        m_ok = slz_decodes and footprint_ok and next_rel_ok and hdr_ok and text_untouched
        ok &= m_ok
        per.append({
            "member": f"{a}:{s}", "src": f"{sa}:{ss}", "iso_offset": off,
            "footprint": footprint,
            "next_rel": onext, "old_compressed": oslz["compressed"],
            "slz_decodes_to_bin": slz_decodes, "footprint_ok": footprint_ok,
            "next_rel_ok": next_rel_ok, "fis_header_clut_unchanged": hdr_ok,
            "text_patch_untouched_this_member": text_untouched,
            "changed_bytes_in_extent": ec, "verified": m_ok,
        })
    report["members"] = per
    report["sum_extent_changed_bytes"] = sum_ext_changed
    report["changed_equals_extent_sum"] = (sum_ext_changed == cmp["total_changed"]
                                           and cmp["outside_changed"] == 0)
    ok &= report["changed_equals_extent_sum"]

    # (5) sha256
    report["output_iso_sha256"] = sha256_file(output_iso)
    report["text_patch_fully_preserved"] = report["outside_identical"] and all(
        m["text_patch_untouched_this_member"] for m in per)
    report["ALL_OK"] = bool(ok)

    args.report.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"size_ok={report['size_ok']} ({out_size})")
    print(f"outside_identical={report['outside_identical']} "
          f"(changed_outside={cmp['outside_changed']})")
    print(f"total_changed_bytes={cmp['total_changed']} == sum_extent={sum_ext_changed} "
          f"-> {report['changed_equals_extent_sum']}")
    print("per-member:")
    for m in per:
        print(f"  {m['member']:<12} slz->bin={m['slz_decodes_to_bin']} "
              f"fp_ok={m['footprint_ok']} nr_ok={m['next_rel_ok']} "
              f"hdr_ok={m['fis_header_clut_unchanged']} text_untouched={m['text_patch_untouched_this_member']} "
              f"changed={m['changed_bytes_in_extent']}")
    print(f"text_patch_fully_preserved={report['text_patch_fully_preserved']}")
    print(f"SHA-256(output)={report['output_iso_sha256']}")
    print(f"ALL_OK={report['ALL_OK']}")
    if not ok:
        raise SystemExit("VERIFICATION FAILED")


if __name__ == "__main__":
    main()

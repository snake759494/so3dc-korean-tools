#!/usr/bin/env python3
"""Apply the SO3 DC image patch (8 repainted FIS texture members) to the ISO.

Writes each recompressed SLZ member in place at its original iso_offset, then
zero-fills to the original member's allocation footprint so the member footprint
and the SLZ chain's next_rel stay valid.  The member start, next_rel, and every
byte outside the 8 member extents are preserved exactly -- the ISO size and each
archive's byte length never change.

Write path mirrors so3_full_patch.patch_archives: temp-copy + per-extent
overwrite + re-read verification + atomic os.replace promote.  Fail-closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

WS = Path(__file__).resolve().parent
_PUBLISH = WS.parent.parent / "publish" / "so3dc-korean-tools"
if not (_PUBLISH / "so3_repack.py").exists():
    _PUBLISH = WS.parent.parent  # repo checkout: the clone root IS the workspace
sys.path.insert(0, str(WS))
sys.path.insert(0, str(_PUBLISH))
from so3_repack import decompress_slz_payload  # noqa: E402

INPUT_ISO = Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full.iso")
OUTPUT_ISO = Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full_Img.iso")
PATCHED = WS / "patched"

TARGETS = [(40, 25), (42, 36), (44, 41), (2254, 38031),
           (47, 49), (47, 52), (47, 55), (47, 56)]


def load_targets(transfer_plan: Path | None) -> list[dict]:
    """Target list [{key, src, expect_off}]; default = disc-1 (src == key).

    With --transfer-plan (disc 2), targets come from the plan's `reusable`
    entries: each disc-2 (archive, stream) member is patched with the disc-1
    repaint named by disc1_key, and its catalog iso_offset must equal the
    plan's d2_iso_offset (fail-closed).  Every disc-1 source must be used.
    """
    if transfer_plan is None:
        return [{"key": k, "src": k, "expect_off": None} for k in TARGETS]
    plan = json.load(open(transfer_plan, encoding="utf-8"))
    if plan.get("missing_on_d2"):
        raise SystemExit(f"transfer plan has missing_on_d2: {plan['missing_on_d2']}")
    out = []
    for e in plan["reusable"]:
        src = tuple(int(x) for x in e["disc1_key"].split(":"))
        if src not in TARGETS:
            raise SystemExit(f"transfer plan source {src} not a patched member")
        if not e.get("fits"):
            raise SystemExit(f"transfer entry {e['d2_archive']}:{e['d2_stream']} does not fit")
        out.append({"key": (int(e["d2_archive"]), int(e["d2_stream"])), "src": src,
                    "expect_off": int(e["d2_iso_offset"])})
    if {t["src"] for t in out} != set(TARGETS):
        raise SystemExit("transfer plan does not cover all 8 disc-1 sources")
    if len({t["key"] for t in out}) != len(out):
        raise SystemExit("transfer plan has duplicate targets")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def catalog_members(catalog_path: Path, keys: list[tuple[int, int]]) -> dict:
    cat = json.load(open(catalog_path, encoding="utf-8"))
    out = {}
    for c in cat:
        k = (c["archive_id"], c["stream_id"])
        if k in keys:
            out[k] = {"iso_offset": c["iso_offset"], "next_rel": c["next_rel"],
                      "compressed": c["compressed"], "unpacked": c["unpacked"]}
    missing = [k for k in keys if k not in out]
    if missing:
        raise SystemExit(f"catalog missing members: {missing}")
    return out


def build_writes(input_iso: Path, targets: list[dict] | None = None,
                 catalog_path: Path | None = None) -> list[dict]:
    """Return per-member write plans: {start, footprint, data, ...}."""
    targets = targets if targets is not None else load_targets(None)
    catalog_path = catalog_path or (WS / "texture_catalog.json")
    members = catalog_members(catalog_path, [t["key"] for t in targets])
    writes = []
    with input_iso.open("rb") as f:
        for t in targets:
            key = t["key"]
            a, s = key
            sa, ss = t["src"]
            d = members[key]
            off = d["iso_offset"]
            if t["expect_off"] is not None and off != t["expect_off"]:
                raise SystemExit(
                    f"{key}: catalog iso_offset {off} != transfer plan {t['expect_off']}")
            slz = (PATCHED / f"{sa}_{ss}.slz").read_bytes()
            bin_ = (PATCHED / f"{sa}_{ss}.bin").read_bytes()
            if slz[:3] != b"SLZ":
                raise SystemExit(f"{key}: bad .slz magic")
            smode = slz[3]
            scomp, sunp, snrel = struct.unpack_from("<III", slz, 4)
            payload = slz[16:]
            if len(payload) != scomp or sunp != len(bin_):
                raise SystemExit(f"{key}: .slz header/payload inconsistent")
            if decompress_slz_payload(payload, smode, sunp) != bin_:
                raise SystemExit(f"{key}: .slz does not decode to .bin")

            # read the ORIGINAL on-disk SLZ header and preserve its next_rel
            f.seek(off)
            oh = f.read(16)
            if oh[:3] != b"SLZ":
                raise SystemExit(f"{key}: no SLZ at iso_offset 0x{off:X}")
            omode = oh[3]
            ocomp, ounp, onext = struct.unpack_from("<III", oh, 4)
            if omode != smode or ounp != sunp:
                raise SystemExit(f"{key}: mode/unpacked mismatch orig vs patched")
            if snrel != onext:
                # fix: preserve the original on-disk next_rel exactly
                snrel = onext

            # allocation footprint of the original member
            footprint = onext if onext else (16 + ocomp)
            if 16 + len(payload) > footprint:
                raise SystemExit(
                    f"{key}: patched member {16+len(payload)} exceeds footprint {footprint}")

            new_hdr = b"SLZ" + bytes([smode]) + struct.pack("<III", len(payload), sunp, onext)
            data = new_hdr + payload + b"\x00" * (footprint - 16 - len(payload))
            assert len(data) == footprint
            writes.append({
                "key": f"{a}:{s}", "src": f"{sa}:{ss}", "start": off,
                "footprint": footprint, "data": data, "next_rel": onext,
                "old_compressed": ocomp, "new_compressed": len(payload),
                "unpacked": sunp,
            })
    # overlap / bounds sanity
    iso_size = input_iso.stat().st_size
    ordered = sorted(writes, key=lambda w: w["start"])
    for i, w in enumerate(ordered):
        end = w["start"] + w["footprint"]
        if end > iso_size:
            raise SystemExit(f"{w['key']}: extent past ISO end")
        if i + 1 < len(ordered) and end > ordered[i + 1]["start"]:
            raise SystemExit(f"{w['key']}: extent overlaps {ordered[i+1]['key']}")
    return writes


def apply_patch(input_iso: Path, output_iso: Path, *, force: bool = False,
                targets: list[dict] | None = None,
                catalog_path: Path | None = None) -> dict:
    if input_iso.resolve() == output_iso.resolve():
        raise SystemExit("input and output must differ")
    if output_iso.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing output: {output_iso}")
    writes = build_writes(input_iso, targets, catalog_path)
    in_size = input_iso.stat().st_size

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=output_iso.parent, prefix=f".{output_iso.name}.", suffix=".tmp", delete=False)
    temp_path = Path(tmp.name)
    tmp.close()
    try:
        shutil.copyfile(input_iso, temp_path)
        with temp_path.open("r+b") as out:
            for w in writes:
                out.seek(w["start"])
                out.write(w["data"])
                if out.tell() != w["start"] + w["footprint"]:
                    raise AssertionError(f"{w['key']}: write length mismatch")
        if temp_path.stat().st_size != in_size:
            raise AssertionError("output ISO size changed")
        # re-read verification
        with temp_path.open("rb") as chk:
            for w in writes:
                chk.seek(w["start"])
                if chk.read(w["footprint"]) != w["data"]:
                    raise AssertionError(f"{w['key']}: re-read verify failed at 0x{w['start']:X}")
        if output_iso.exists():
            output_iso.unlink()
        os.replace(temp_path, output_iso)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    report = {
        "input_iso": str(input_iso),
        "output_iso": str(output_iso),
        "input_size": in_size,
        "output_size": output_iso.stat().st_size,
        "output_iso_sha256": sha256_file(output_iso),
        "members": [{k: w[k] for k in ("key", "src", "start", "footprint", "next_rel",
                                       "old_compressed", "new_compressed", "unpacked")}
                    for w in writes],
    }
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, default=INPUT_ISO)
    ap.add_argument("--output", type=Path, default=OUTPUT_ISO)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--report", type=Path, default=WS / "apply_image_report.json")
    ap.add_argument("--catalog", type=Path, default=WS / "texture_catalog.json",
                    help="texture_catalog.json for the target disc")
    ap.add_argument("--transfer-plan", type=Path, default=None,
                    help="disc-2 transfer_plan.json: patch its reusable targets "
                         "with the disc-1 repaints (default: disc-1 targets)")
    args = ap.parse_args()
    rep = apply_patch(args.input, args.output, force=args.force,
                      targets=load_targets(args.transfer_plan),
                      catalog_path=args.catalog)
    args.report.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(f"OUTPUT: {rep['output_iso']}")
    print(f"size: {rep['output_size']} (input {rep['input_size']})")
    print(f"SHA-256: {rep['output_iso_sha256']}")
    for w in rep["members"]:
        print(f"  {w['key']:<12} @0x{w['start']:X} footprint={w['footprint']} "
              f"old_comp={w['old_compressed']} new_comp={w['new_compressed']} next_rel={w['next_rel']}")
    print(f"report -> {args.report}")


if __name__ == "__main__":
    main()

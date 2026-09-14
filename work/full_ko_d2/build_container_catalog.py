# -*- coding: utf-8 -*-
"""build_container_catalog.py — mclib container catalog straight from a stream
manifest (disc-2 replacement for disc 1's work\\mclib_all_decode analysis run).

Disc 1's container_catalog.csv was produced by analyze_all_mclib.py over the
mclib_all_decode corpus.  Disc 2 skips that heavyweight decode pass: this tool
filters the disc's stream_manifest.csv for magic_text startswith 'so3mclib'
(depth 0) and parses every decoded mclib header directly, emitting one row PER
OCCURRENCE (disc 1 has 7,786 rows for 1,516 unique files) so that downstream
refs/occurrence aggregation (build_inventory, expand_unique_to_occurrences,
verify_full_iso.load_catalog_occurrences) works unchanged.

Emitted columns = every column a pipeline consumer actually reads
(file_sha256, path, version, archive_id, stream_id across build_inventory /
prepare_translation_batches / validate_translations / so3_full_patch /
verify_full_iso / solve_controls) + the header-derived geometry columns of the
disc-1 catalog, same names/order.  The four decode-analysis columns
(valid_messages, fully_decoded_messages, unresolved_messages,
glyph_operands_before_unresolved) are NOT reproduced — nothing consumes them.

Also writes container_catalog_report.json next to the CSV:
  * occurrence/unique counts, version histogram
  * overlap vs disc 1's catalog (same file_sha256) — expect heavy overlap for
    menus/battle/system containers
  * glyph coverage gap: distinct 24px local-glyph bitmap sha256s of this
    disc's unique containers that are NOT labeled in the disc-1 OCR map
    (font_ocr\\glyph_mapping_ordered_24.json) + glyph_labels_extra.json —
    this gap drives the next labeling step.

Usage (defaults = disc 2):
  python build_container_catalog.py
  python build_container_catalog.py --stream-manifest ...\\disc1\\manifests\\stream_manifest.csv \
      --disc-root ...\\disc1 --out sanity_disc1_catalog.csv   (disc-1 equivalence sanity check)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
D2_ROOT = WS / "work" / "full_unpack" / "disc2"
OUT_DIR = WS / "work" / "full_ko_d2"
DISC1_CATALOG = WS / "work" / "mclib_all_decode" / "container_catalog.csv"
GLYPH24 = WS / "work" / "font_ocr" / "glyph_mapping_ordered_24.json"
GLYPH_EXTRA = WS / "work" / "full_ko" / "glyph_labels_extra.json"

# disc-1 catalog column order minus the four decode-analysis columns
COLUMNS = [
    "archive_id", "stream_id", "path", "file_sha256", "version",
    "table_start", "table_end", "width_start", "bitmap_start", "glyph_count",
    "cache_width", "cache_height", "glyph_width", "glyph_height", "glyph_stride",
    "local_glyph_code_base", "mapping_count", "file_size", "actual_file_size",
    "glyph_bytes", "text_start", "text_end", "text_bytes",
    "structure_valid", "structure_errors", "byte_identical_multiplicity",
]


def parse_header(data: bytes) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if len(data) < 0x80 or not data.startswith(b"so3mclib "):
        return {}, ["not an so3mclib (magic/size)"]
    version = data[:0x10].split(b"\0")[0].decode("ascii", "replace").strip()
    u = lambda o: struct.unpack_from("<I", data, o)[0]
    h = {
        "version": version,
        "table_start": u(0x10), "text_start": u(0x14), "width_start": u(0x18),
        "bitmap_start": u(0x1C), "glyph_count": u(0x20),
        "cache_width": u(0x24), "cache_height": u(0x28),
        "glyph_width": u(0x2C), "glyph_height": u(0x30), "glyph_stride": u(0x34),
        "local_glyph_code_base": u(0x38), "mapping_count": u(0x3C),
        "file_size": u(0x40),
    }
    h["actual_file_size"] = len(data)
    h["glyph_bytes"] = h["glyph_stride"] * h["glyph_height"] // 2
    h["table_end"] = h["text_start"]
    h["text_end"] = h["width_start"] if h["glyph_count"] else h["file_size"]
    h["text_bytes"] = h["text_end"] - h["text_start"]
    if h["file_size"] != len(data):
        errors.append(f"header file_size {h['file_size']} != actual {len(data)}")
    if h["mapping_count"] and h["table_start"] + h["mapping_count"] * 8 > len(data):
        errors.append("mapping table exceeds file")
    if h["glyph_count"]:
        if h["bitmap_start"] + h["glyph_count"] * h["glyph_bytes"] > len(data):
            errors.append("bitmap section exceeds file")
        if h["width_start"] + h["glyph_count"] > len(data):
            errors.append("width table exceeds file")
    if h["text_bytes"] < 0:
        errors.append("negative text extent")
    return h, errors


def local_bitmap_shas(data: bytes, h: dict) -> set[str]:
    """sha256 of every local glyph bitmap in a container (24px cells only)."""
    out: set[str] = set()
    if not h.get("glyph_count") or h.get("glyph_width") != 24:
        return out
    bpg = h["glyph_bytes"]
    start = h["bitmap_start"]
    for i in range(h["glyph_count"]):
        out.add(hashlib.sha256(data[start + i * bpg:start + (i + 1) * bpg]).hexdigest())
    return out


def load_labeled_shas(glyph_map: Path, glyph_extra: Path) -> tuple[set[str], set[str]]:
    """(labeled shas from the OCR map, additional labeled shas from extras)."""
    ocr: set[str] = set()
    if glyph_map.exists():
        d = json.loads(glyph_map.read_text(encoding="utf-8"))
        ocr = {g["bitmap_sha256"] for g in d["glyphs"] if g.get("unicode")}
    extra: set[str] = set()
    if glyph_extra.exists():
        labels = json.loads(glyph_extra.read_text(encoding="utf-8")).get("labels", {})
        for sha, rec in labels.items():
            ch = rec.get("char") or ""
            if len(ch) == 1 and ch != "?" and rec.get("confidence") in ("high", "medium"):
                extra.add(sha)
    return ocr, extra


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stream-manifest", type=Path,
                    default=D2_ROOT / "manifests" / "stream_manifest.csv")
    ap.add_argument("--disc-root", type=Path, default=D2_ROOT,
                    help="root the manifest 'path' column is relative to")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "container_catalog.csv")
    ap.add_argument("--report", type=Path, default=None,
                    help="JSON report path (default: <out dir>\\container_catalog_report.json)")
    ap.add_argument("--disc1-catalog", type=Path, default=DISC1_CATALOG,
                    help="disc-1 catalog for the file_sha256 overlap report "
                         "(missing => overlap section skipped)")
    ap.add_argument("--glyph-map", type=Path, default=GLYPH24)
    ap.add_argument("--glyph-extra", type=Path, default=GLYPH_EXTRA)
    ap.add_argument("--no-glyph-gap", action="store_true",
                    help="skip the glyph coverage gap scan")
    args = ap.parse_args()

    t0 = time.time()
    if not args.stream_manifest.exists():
        print(f"stream manifest not found: {args.stream_manifest}")
        return 2
    report_path = args.report or (args.out.parent / "container_catalog_report.json")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows_in = []
    n_nested = 0
    with args.stream_manifest.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row["magic_text"].startswith("so3mclib"):
                continue
            if int(row["depth"]) != 0:
                n_nested += 1
                continue
            rows_in.append(row)
    print(f"manifest mclib rows (depth 0): {len(rows_in)}"
          + (f"; nested skipped: {n_nested}" if n_nested else ""))

    # parse each occurrence; cache per decoded file path (occurrences of the
    # same on-disc stream may share a decoded file only if the unpacker deduped;
    # normally every occurrence has its own decoded copy)
    out_rows = []
    sha_of_path: dict[str, str] = {}
    header_of_sha: dict[str, dict] = {}
    data_of_sha: dict[str, bytes] = {}   # kept only for unique shas (glyph gap)
    n_bad = 0
    for i, row in enumerate(rows_in):
        rel = row["path"]
        path = ((args.disc_root / rel) if not os.path.isabs(rel) else Path(rel)).resolve()
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"  READ FAIL {row['archive_id']}:{row['stream_id']} {path}: {exc}")
            n_bad += 1
            continue
        sha = sha_of_path.get(str(path)) or hashlib.sha256(data).hexdigest()
        sha_of_path[str(path)] = sha
        if sha not in header_of_sha:
            h, errors = parse_header(data)
            h["_errors"] = errors
            header_of_sha[sha] = h
            if h and not args.no_glyph_gap:
                data_of_sha[sha] = data
        h = header_of_sha[sha]
        if not h or (h.get("_errors") and "not an so3mclib" in h["_errors"][0]):
            n_bad += 1
            continue
        out_rows.append({
            "archive_id": int(row["archive_id"]), "stream_id": int(row["stream_id"]),
            "path": str(path), "file_sha256": sha, "version": h["version"],
            "table_start": h["table_start"], "table_end": h["table_end"],
            "width_start": h["width_start"], "bitmap_start": h["bitmap_start"],
            "glyph_count": h["glyph_count"],
            "cache_width": h["cache_width"], "cache_height": h["cache_height"],
            "glyph_width": h["glyph_width"], "glyph_height": h["glyph_height"],
            "glyph_stride": h["glyph_stride"],
            "local_glyph_code_base": h["local_glyph_code_base"],
            "mapping_count": h["mapping_count"],
            "file_size": h["file_size"], "actual_file_size": h["actual_file_size"],
            "glyph_bytes": h["glyph_bytes"],
            "text_start": h["text_start"], "text_end": h["text_end"],
            "text_bytes": h["text_bytes"],
            "structure_valid": 0 if h["_errors"] else 1,
            "structure_errors": ";".join(h["_errors"]),
            "byte_identical_multiplicity": 0,  # filled below
        })
        if (i + 1) % 1000 == 0:
            print(f"  ...{i + 1}/{len(rows_in)} ({time.time() - t0:.1f}s)")

    mult = Counter(r["file_sha256"] for r in out_rows)
    for r in out_rows:
        r["byte_identical_multiplicity"] = mult[r["file_sha256"]]
    out_rows.sort(key=lambda r: (r["archive_id"], r["stream_id"]))

    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)
    version_hist = Counter(r["version"] for r in out_rows)
    n_unique = len(mult)
    print(f"catalog: {len(out_rows)} occurrence rows, {n_unique} unique files "
          f"-> {args.out}")
    print(f"versions: {dict(version_hist)}; structure errors: "
          f"{sum(1 for r in out_rows if not r['structure_valid'])}; unreadable/non-mclib: {n_bad}")

    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stream_manifest": str(args.stream_manifest),
        "occurrence_rows": len(out_rows),
        "unique_files": n_unique,
        "nested_mclib_rows_skipped": n_nested,
        "unreadable_or_bad": n_bad,
        "versions": dict(version_hist),
        "structure_invalid_rows": sum(1 for r in out_rows if not r["structure_valid"]),
    }

    # --- overlap vs disc 1
    if args.disc1_catalog.exists():
        d1_shas = set()
        with args.disc1_catalog.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d1_shas.add(row["file_sha256"])
        my_shas = set(mult)
        shared = my_shas & d1_shas
        shared_occ = sum(mult[s] for s in shared)
        report["disc1_overlap"] = {
            "disc1_catalog": str(args.disc1_catalog),
            "disc1_unique_files": len(d1_shas),
            "unique_files_shared_with_disc1": len(shared),
            "unique_files_new_on_this_disc": len(my_shas - d1_shas),
            "occurrence_rows_covered_by_disc1_files": shared_occ,
        }
        print(f"disc-1 overlap: {len(shared)}/{len(my_shas)} unique files already in "
              f"disc 1's catalog ({shared_occ}/{len(out_rows)} occurrences)")

    # --- glyph coverage gap (24px local atlases vs disc-1 OCR map + extras)
    if not args.no_glyph_gap:
        ocr, extra = load_labeled_shas(args.glyph_map, args.glyph_extra)
        all_local: set[str] = set()
        per_container_unlabeled = {}
        for sha, data in data_of_sha.items():
            h = header_of_sha[sha]
            shas = local_bitmap_shas(data, h)
            all_local |= shas
            miss = shas - ocr - extra
            if miss:
                per_container_unlabeled[sha[:12]] = len(miss)
        unlabeled = all_local - ocr - extra
        report["glyph_coverage"] = {
            "glyph_map": str(args.glyph_map),
            "glyph_extra": str(args.glyph_extra),
            "labeled_shas_ocr_map": len(ocr),
            "labeled_shas_extra": len(extra),
            "distinct_local_bitmap_shas_24px": len(all_local),
            "unlabeled_local_bitmap_shas": len(unlabeled),
            "containers_with_unlabeled_glyphs": len(per_container_unlabeled),
            "worst_containers": dict(sorted(per_container_unlabeled.items(),
                                            key=lambda kv: -kv[1])[:20]),
        }
        print(f"glyph coverage: {len(all_local)} distinct 24px local bitmaps, "
              f"{len(unlabeled)} unlabeled (gap for the labeling step) across "
              f"{len(per_container_unlabeled)} containers")

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    print(f"report -> {report_path}")
    print(f"elapsed {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

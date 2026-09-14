#!/usr/bin/env python3
"""Verify a So3Unpack output tree against its CSV manifests."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    args = ap.parse_args()
    root = args.root.resolve()
    manifests = root / "manifests"

    archives = list(rows(manifests / "archive_manifest.csv"))
    streams = list(rows(manifests / "stream_manifest.csv"))
    bad: list[str] = []
    raw_bytes = decoded_bytes = 0

    for r in archives:
        size = int(r["bytes"])
        if not r["raw_path"]:
            continue
        path = root / r["raw_path"]
        if not path.is_file() or path.stat().st_size != size:
            bad.append(f"raw {r['id']}: expected {size}, got {path.stat().st_size if path.exists() else 'missing'}")
        else:
            raw_bytes += size

    for r in streams:
        size = int(r["unpacked"])
        if r["error"]:
            bad.append(f"stream {r['stream_id']}: decoder error: {r['error']}")
        if not r["path"]:
            continue
        path = root / r["path"]
        if not path.is_file() or path.stat().st_size != size:
            bad.append(f"stream {r['stream_id']}: expected {size}, got {path.stat().st_size if path.exists() else 'missing'}")
        else:
            decoded_bytes += size

    root_streams = sum(r["depth"] == "0" for r in streams)
    nested_streams = len(streams) - root_streams
    print(f"archives={len(archives)} nonempty={sum(int(r['bytes']) > 0 for r in archives)} raw_bytes={raw_bytes}")
    print(f"streams={len(streams)} root={root_streams} nested={nested_streams} decoded_bytes={decoded_bytes}")
    print(f"errors={len(bad)}")
    for line in bad[:100]:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Combine independently reviewed SO3 manual glyph-evidence documents."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    entries_by_prefix: dict[str, dict[str, object]] = {}
    provenance = []
    for path in args.inputs:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"{path} does not contain an entries list")
        provenance.append(
            {
                "path": str(path.resolve()),
                "sha256": file_sha256(path),
                "entries": len(entries),
            }
        )
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise ValueError(f"non-object evidence entry in {path}")
            prefix = str(raw_entry.get("sha256_prefix") or "").lower()
            character = raw_entry.get("unicode")
            if not prefix or any(character not in "0123456789abcdef" for character in prefix):
                raise ValueError(f"invalid SHA-256 prefix in {path}: {prefix!r}")
            if not isinstance(character, str) or len(character) != 1:
                raise ValueError(f"evidence {prefix!r} in {path} must contain one character")
            previous = entries_by_prefix.get(prefix)
            if previous is not None and previous.get("unicode") != character:
                raise ValueError(
                    f"conflicting evidence for {prefix}: {previous.get('unicode')!r} != {character!r}"
                )
            if previous is None:
                entries_by_prefix[prefix] = dict(raw_entry)

    output = {
        "schema_version": 1,
        "description": "Combined manually reviewed SO3 glyph labels",
        "key_policy": "sha256_prefix must resolve to exactly one glyph in the selected geometry",
        "provenance": provenance,
        "entries": list(entries_by_prefix.values()),
    }
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "inputs": len(args.inputs),
                "input_entries": sum(item["entries"] for item in provenance),
                "combined_entries": len(entries_by_prefix),
                "conflicts": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()

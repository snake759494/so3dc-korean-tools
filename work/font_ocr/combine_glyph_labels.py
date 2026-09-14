#!/usr/bin/env python3
"""Combine geometry-specific SO3 bitmap labels into one verified label set."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUTS = [
    ROOT / "work/font_ocr/verified_glyph_labels_24.json",
    ROOT / "work/font_ocr/verified_glyph_labels_32.json",
]
DEFAULT_OUTPUT = ROOT / "work/font_ocr/verified_glyph_labels.json"
KEY_PATTERN = re.compile(r"^(\d+)x(\d+):([0-9a-f]{64})$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        code == 0x3007
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="*", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if len(args.inputs) < 2:
        raise ValueError("at least two geometry-specific label documents are required")

    labels: dict[str, dict[str, object]] = {}
    input_records: list[dict[str, object]] = []
    geometry_counts: collections.Counter[str] = collections.Counter()
    source_counts: collections.Counter[str] = collections.Counter()

    for path in args.inputs:
        document = json.loads(path.read_text(encoding="utf-8"))
        source_labels = document.get("labels")
        if not isinstance(source_labels, dict):
            raise ValueError(f"{path} does not contain a labels object")

        input_records.append(
            {
                "path": str(path.resolve()),
                "sha256": file_sha256(path),
                "schema_version": document.get("schema_version"),
                "summary": document.get("summary"),
            }
        )
        for key, raw_label in source_labels.items():
            match = KEY_PATTERN.fullmatch(key)
            if match is None:
                raise ValueError(f"invalid bitmap label key in {path}: {key!r}")
            if not isinstance(raw_label, dict):
                raise ValueError(f"label {key!r} in {path} is not an object")
            character = raw_label.get("unicode")
            if not isinstance(character, str) or len(character) != 1:
                raise ValueError(f"label {key!r} in {path} must contain one Unicode character")
            if raw_label.get("apply") is not True:
                raise ValueError(f"label {key!r} in {path} is not explicitly approved for application")

            previous = labels.get(key)
            if previous is not None and previous.get("unicode") != character:
                raise ValueError(
                    f"conflicting labels for {key}: {previous.get('unicode')!r} != {character!r}"
                )
            labels[key] = dict(raw_label)
            geometry_counts[f"{match.group(1)}x{match.group(2)}"] += previous is None
            if previous is None:
                source_counts[str(raw_label.get("source") or "unknown")] += 1

    ordered_labels = {key: labels[key] for key in sorted(labels)}
    summary = {
        "input_documents": len(input_records),
        "labeled_glyphs": len(ordered_labels),
        "labeled_cjk_ideographs": sum(
            is_cjk(str(label["unicode"])) for label in ordered_labels.values()
        ),
        "geometry_counts": dict(sorted(geometry_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "conflicts": 0,
    }
    output = {
        "schema_version": 1,
        "title": "SO3 verified and high-confidence SHA-keyed glyph labels (all geometries)",
        "status": "automatic_high_confidence_plus_manual_evidence",
        "summary": summary,
        "provenance": {"inputs": input_records},
        "labels": ordered_labels,
    }
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()

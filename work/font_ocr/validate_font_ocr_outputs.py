#!/usr/bin/env python3
"""Validate the reproducibility and application of SO3 font OCR outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FONT_OCR = ROOT / "work/font_ocr"
CATALOG = ROOT / "work/kanji_deep/mclib_catalog"
MAPPING = ROOT / "work/dialogue_locator/font_mapping_table.json"
DIALOGUE = ROOT / "work/dialogue_locator/all_dialogue_targets_ja_v2.json"


def load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def is_cjk(character: str | None) -> bool:
    if not isinstance(character, str) or len(character) != 1:
        return False
    code = ord(character)
    return (
        code == 0x3007
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def main() -> None:
    geometry_documents: dict[str, dict[str, object]] = {}
    expected_labels: dict[str, str] = {}
    expected_cjk: dict[str, list[dict[str, object]]] = {}

    for size in (24, 32):
        geometry_name = f"{size}x{size}"
        ordered_path = FONT_OCR / f"glyph_mapping_ordered_{size}.json"
        csv_path = CATALOG / f"unique_{size}px_glyphs.csv"
        ordered_document = load(ordered_path)
        geometry_documents[geometry_name] = ordered_document
        glyphs = ordered_document["glyphs"]
        if not isinstance(glyphs, list):
            raise ValueError(f"{ordered_path} glyphs is not a list")

        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            csv_by_index = {
                int(row["union_index"]): row for row in csv.DictReader(handle)
            }
        indices = [int(record["union_index"]) for record in glyphs]
        if indices != sorted(indices) or len(indices) != len(set(indices)):
            raise ValueError(f"{geometry_name} ordered mapping is not strictly atlas ordered")

        cjk_records = []
        for record in glyphs:
            union_index = int(record["union_index"])
            digest = str(record["bitmap_sha256"])
            key = str(record["key"])
            if csv_by_index[union_index]["sha256"] != digest:
                raise ValueError(f"{geometry_name} CSV hash mismatch at {union_index}")
            if key != f"{geometry_name}:{digest}":
                raise ValueError(f"{geometry_name} malformed key at {union_index}")
            character = record.get("unicode")
            if character is not None:
                previous = expected_labels.setdefault(key, str(character))
                if previous != character:
                    raise ValueError(f"conflicting ordered labels for {key}")
            if is_cjk(character):
                cjk_records.append(record)
        expected_cjk[geometry_name] = cjk_records

    combined = load(FONT_OCR / "verified_glyph_labels.json")
    labels = combined["labels"]
    if not isinstance(labels, dict):
        raise ValueError("combined labels is not an object")
    if set(labels) != set(expected_labels):
        raise ValueError("combined labels do not exactly equal the geometry-specific labels")
    for key, character in expected_labels.items():
        if labels[key].get("unicode") != character or labels[key].get("apply") is not True:
            raise ValueError(f"combined label does not apply the expected character: {key}")

    compact = load(FONT_OCR / "kanji_mapping_by_atlas_order.json")
    groups = compact["groups"]
    if not isinstance(groups, list):
        raise ValueError("compact kanji mapping groups is not a list")
    compact_count = 0
    for group in groups:
        geometry = group["geometry"]
        geometry_name = f"{geometry[0]}x{geometry[1]}"
        entries = group["entries"]
        expected = expected_cjk[geometry_name]
        if len(entries) != len(expected):
            raise ValueError(f"compact {geometry_name} CJK count mismatch")
        for position, (entry, record) in enumerate(zip(entries, expected)):
            if entry["kanji_order"] != position:
                raise ValueError(f"compact {geometry_name} kanji_order mismatch")
            for field in ("union_index", "union_label", "bitmap_sha256", "key", "unicode"):
                if entry[field] != record[field]:
                    raise ValueError(f"compact {geometry_name} {field} mismatch at {position}")
        compact_count += len(entries)

    mapping = load(MAPPING)
    slots = mapping["global_slots"] + mapping["local_slots"]
    actual_keys = {
        f"{slot['geometry'][0]}x{slot['geometry'][1]}:{slot['bitmap_sha256']}"
        for slot in slots
    }
    missing_keys = set(labels) - actual_keys
    if missing_keys:
        raise ValueError(f"{len(missing_keys)} combined labels do not resolve to real slots")
    for slot in slots:
        key = f"{slot['geometry'][0]}x{slot['geometry'][1]}:{slot['bitmap_sha256']}"
        if key in labels and slot.get("unicode") != labels[key].get("unicode"):
            raise ValueError(f"combined label was not applied to slot key {key}")
    if mapping["scope"]["font_ocr_conflicts"] != 0:
        raise ValueError("font OCR label conflicts are present in the full mapping")
    if mapping["scope"]["font_ocr_unique_labels_used"] != len(labels):
        raise ValueError("not every combined label was used by the full mapping")

    for size in (24, 32):
        if size == 32:
            manual_path = FONT_OCR / "manual_glyph_evidence_32.json"
        else:
            combined_manual_path = FONT_OCR / "manual_glyph_evidence_24_combined.json"
            manual_path = (
                combined_manual_path
                if combined_manual_path.exists()
                else FONT_OCR / "manual_glyph_evidence.json"
            )
        manual = load(manual_path)
        hashes = [
            str(record["bitmap_sha256"])
            for record in geometry_documents[f"{size}x{size}"]["glyphs"]
        ]
        for entry in manual["entries"]:
            prefix = str(entry["sha256_prefix"]).lower()
            if sum(digest.startswith(prefix) for digest in hashes) != 1:
                raise ValueError(f"{size}px manual SHA prefix is not unique: {prefix}")

    dialogue = load(DIALOGUE)
    conversion_summary = dialogue.get("conversion_summary")
    if not isinstance(conversion_summary, dict):
        raise ValueError("dialogue catalog is missing conversion_summary")

    result = {
        "status": "PASS",
        "geometry_glyphs": {
            name: len(document["glyphs"]) for name, document in geometry_documents.items()
        },
        "combined_labels": len(labels),
        "kanji_bitmap_shapes": compact_count,
        "font_ocr_conflicts": 0,
        "mapping_scope": mapping["scope"],
        "dialogue_conversion_summary": conversion_summary,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

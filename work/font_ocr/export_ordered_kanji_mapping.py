#!/usr/bin/env python3
"""Export a compact kanji-only mapping in source atlas order."""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    (
        [24, 24],
        ROOT / "work/font_ocr/glyph_mapping_ordered_24.json",
        ROOT / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.png",
        ROOT / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.csv",
    ),
    (
        [32, 32],
        ROOT / "work/font_ocr/glyph_mapping_ordered_32.json",
        ROOT / "work/kanji_deep/mclib_catalog/unique_32px_glyphs.png",
        ROOT / "work/kanji_deep/mclib_catalog/unique_32px_glyphs.csv",
    ),
]
OUTPUT = ROOT / "work/font_ocr/kanji_mapping_by_atlas_order.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    groups: list[dict[str, object]] = []
    total_entries = 0
    characters: set[str] = set()
    source_counts: collections.Counter[str] = collections.Counter()
    review_counts: collections.Counter[str] = collections.Counter()

    for geometry, mapping_path, atlas_path, csv_path in SOURCES:
        document = json.loads(mapping_path.read_text(encoding="utf-8"))
        entries = []
        for record in sorted(document["glyphs"], key=lambda item: int(item["union_index"])):
            character = record.get("unicode")
            if not is_cjk(character):
                continue
            source = str(record.get("source") or "unknown")
            review_status = str(record.get("review_status") or "unknown")
            entries.append(
                {
                    "kanji_order": len(entries),
                    "union_index": int(record["union_index"]),
                    "union_label": record["union_label"],
                    "bitmap_sha256": record["bitmap_sha256"],
                    "key": record["key"],
                    "unicode": character,
                    "codepoint": f"U+{ord(character):04X}",
                    "source": source,
                    "confidence": record.get("confidence"),
                    "review_status": review_status,
                }
            )
            characters.add(character)
            source_counts[source] += 1
            review_counts[review_status] += 1

        total_entries += len(entries)
        groups.append(
            {
                "geometry": geometry,
                "atlas": {
                    "path": str(atlas_path.resolve()),
                    "sha256": file_sha256(atlas_path),
                    "mapping_csv": str(csv_path.resolve()),
                    "mapping_csv_sha256": file_sha256(csv_path),
                },
                "ordered_mapping_source": {
                    "path": str(mapping_path.resolve()),
                    "sha256": file_sha256(mapping_path),
                },
                "kanji_glyph_count": len(entries),
                "entries": entries,
            }
        )

    output = {
        "schema_version": 1,
        "title": "Star Ocean 3 DC kanji bitmap mapping in font-atlas order",
        "ordering": (
            "Groups are ordered by geometry; entries preserve the union_index order of each "
            "unique glyph atlas. kanji_order is zero-based within its geometry group."
        ),
        "key_policy": "<width>x<height>:<SHA-256 of raw decoded glyph bitmap>",
        "summary": {
            "geometry_groups": len(groups),
            "kanji_bitmap_shapes": total_entries,
            "distinct_unicode_kanji": len(characters),
            "geometry_counts": {
                f"{group['geometry'][0]}x{group['geometry'][1]}": group["kanji_glyph_count"]
                for group in groups
            },
            "source_counts": dict(sorted(source_counts.items())),
            "review_status_counts": dict(sorted(review_counts.items())),
            "conflicts": 0,
        },
        "groups": groups,
    }
    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()

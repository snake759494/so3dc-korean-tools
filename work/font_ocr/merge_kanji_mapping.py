#!/usr/bin/env python3
"""Merge independent SO3 glyph OCR evidence into stable SHA-keyed labels."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WINDOWS = ROOT / "work/font_ocr/windows_ocr_candidates.json"
DEFAULT_TEMPLATE = ROOT / "work/font_ocr/template_match_candidates.json"
DEFAULT_MANUAL = ROOT / "work/font_ocr/manual_glyph_evidence.json"
DEFAULT_MAPPING = ROOT / "work/dialogue_locator/font_mapping_table.json"
DEFAULT_ORDERED = ROOT / "work/font_ocr/kanji_mapping_ordered.json"
DEFAULT_LABELS = ROOT / "work/font_ocr/verified_glyph_labels.json"
DEFAULT_REVIEW = ROOT / "work/font_ocr/kanji_review_queue.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_cjk(character: str | None) -> bool:
    if character is None or len(character) != 1:
        return False
    code = ord(character)
    return (
        code == 0x3007  # IDEOGRAPHIC NUMBER ZERO, used as Japanese kanji numeral 〇
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def dump(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=Path, default=DEFAULT_WINDOWS)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--manual", type=Path, default=DEFAULT_MANUAL)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--ordered", type=Path, default=DEFAULT_ORDERED)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    args = parser.parse_args()

    windows_document = json.loads(args.windows.read_text(encoding="utf-8"))
    template_document = json.loads(args.template.read_text(encoding="utf-8"))
    manual_document = json.loads(args.manual.read_text(encoding="utf-8"))
    mapping_document = json.loads(args.mapping.read_text(encoding="utf-8"))

    raw_geometry = windows_document.get("geometry", [24, 24])
    if (
        not isinstance(raw_geometry, list)
        or len(raw_geometry) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in raw_geometry)
    ):
        raise ValueError(f"invalid Windows candidate geometry: {raw_geometry!r}")
    geometry = list(raw_geometry)
    glyph_width, glyph_height = geometry
    geometry_name = f"{glyph_width}x{glyph_height}"
    key_prefix = f"{geometry_name}:"
    size_summary_name = (
        f"{glyph_width}px" if glyph_width == glyph_height else f"{geometry_name}px"
    )

    windows = windows_document["candidates"]
    template_by_hash = {item["bitmap_sha256"]: item for item in template_document["matches"]}
    hashes = [item["bitmap_sha256"] for item in windows]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Windows candidate list contains duplicate bitmap hashes")
    if set(hashes) != set(template_by_hash):
        raise ValueError("Windows and template candidate hash sets differ")

    existing_by_hash: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: {"unicode": set(), "sources": set()}
    )
    for entry in mapping_document["global_slots"] + mapping_document["local_slots"]:
        if entry.get("geometry") != geometry or not entry["unicode"]:
            continue
        # A regenerated mapping may already contain a previous OCR pass.  Do
        # not feed those labels back as independent preexisting evidence.
        if str(entry.get("unicode_source") or "").startswith("font_ocr:"):
            continue
        digest = str(entry["bitmap_sha256"])
        existing_by_hash[digest]["unicode"].add(str(entry["unicode"]))
        if entry["unicode_source"]:
            existing_by_hash[digest]["sources"].add(str(entry["unicode_source"]))

    manual_by_hash: dict[str, dict[str, object]] = {}
    for entry in manual_document["entries"]:
        prefix = str(entry["sha256_prefix"]).lower()
        matched = [digest for digest in hashes if digest.startswith(prefix)]
        if len(matched) != 1:
            raise ValueError(f"manual prefix {prefix!r} resolved to {len(matched)} local hashes")
        digest = matched[0]
        if digest in manual_by_hash and manual_by_hash[digest]["unicode"] != entry["unicode"]:
            raise ValueError(f"conflicting manual labels for {digest}")
        manual_by_hash[digest] = entry

    ordered: list[dict[str, object]] = []
    labels: dict[str, dict[str, object]] = {}
    review: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    source_counts: collections.Counter[str] = collections.Counter()

    for windows_item in windows:
        digest = str(windows_item["bitmap_sha256"])
        template_item = template_by_hash[digest]
        template_chars = [item["character"] for item in template_item["top5"]]
        ocr = windows_item["ocr_consensus"]
        if ocr == template_chars[0]:
            template_relation = "top1"
        elif ocr in template_chars:
            template_relation = "top5"
        else:
            template_relation = "outside_top5"

        existing = sorted(existing_by_hash[digest]["unicode"])
        manual = manual_by_hash.get(digest)
        chosen: str | None = None
        source: str | None = None
        confidence: str | None = None
        review_status: str | None = None

        if len(existing) > 1:
            conflicts.append(
                {"bitmap_sha256": digest, "kind": "existing_unicode_conflict", "values": existing}
            )
        elif manual is not None and existing and manual["unicode"] != existing[0]:
            conflicts.append(
                {
                    "bitmap_sha256": digest,
                    "kind": "manual_existing_conflict",
                    "manual": manual["unicode"],
                    "existing": existing[0],
                }
            )
        elif manual is not None:
            chosen = str(manual["unicode"])
            source = "manual_bitmap_and_context"
            confidence = str(manual.get("confidence", "confirmed"))
            review_status = "human_confirmed"
        elif len(existing) == 1:
            chosen = existing[0]
            source = "preexisting_direct_or_hash_evidence"
            confidence = "confirmed"
            review_status = "preexisting_confirmed"
        elif windows_item["ocr_class"] == "cjk_ideograph" and windows_item["status"] == "unanimous":
            chosen = str(ocr)
            source = "windows_japanese_ocr_3of3"
            confidence = "high"
            review_status = "automatic_high_confidence"
        elif (
            windows_item["ocr_class"] == "cjk_ideograph"
            and windows_item["status"] == "majority"
            and template_relation in {"top1", "top5"}
        ):
            chosen = str(ocr)
            source = "windows_japanese_ocr_2of3_plus_msgothic_top5"
            confidence = "high"
            review_status = "automatic_cross_checked"

        record = {
            "union_index": int(windows_item["union_index"]),
            "union_label": windows_item["union_label"],
            "bitmap_sha256": digest,
            "key": f"{key_prefix}{digest}",
            "unicode": chosen,
            "is_cjk_ideograph": is_cjk(chosen),
            "source": source,
            "confidence": confidence,
            "review_status": review_status,
            "windows_ocr": {
                "consensus": ocr,
                "class": windows_item["ocr_class"],
                "status": windows_item["status"],
                "votes": windows_item["votes"],
                "by_variant": windows_item["ocr_by_variant"],
            },
            "template_match": {
                "relation_to_windows": template_relation,
                "top5": template_item["top5"],
                "margin_top1_top2": template_item["margin_top1_top2"],
            },
            "preexisting_unicode": existing,
            "manual_evidence": manual,
        }
        ordered.append(record)
        if chosen is not None:
            if len(chosen) != 1:
                raise ValueError(f"label must be one Unicode character: {chosen!r}")
            labels[record["key"]] = {
                "unicode": chosen,
                "source": source,
                "confidence": confidence,
                "review_status": review_status,
                "union_index": record["union_index"],
                "union_label": record["union_label"],
                "apply": True,
            }
            source_counts[str(source)] += 1
        elif windows_item["ocr_class"] == "cjk_ideograph" or template_item["top5"][0]["score"] >= 0.75:
            review.append(record)

    if conflicts:
        conflict_hashes = {item["bitmap_sha256"] for item in conflicts}
        for digest in conflict_hashes:
            labels.pop(f"{key_prefix}{digest}", None)

    summary = {
        "geometry": geometry,
        "glyph_key_prefix": key_prefix,
        f"unique_local_{size_summary_name}_glyphs": len(ordered),
        "labeled_glyphs": len(labels),
        "labeled_cjk_ideographs": sum(is_cjk(item["unicode"]) for item in labels.values()),
        "review_queue": len(review),
        "conflicts": len(conflicts),
        "source_counts": dict(sorted(source_counts.items())),
        "windows_validation": windows_document["summary"],
        "template_validation": {
            "evaluated": template_document["accuracy"]["evaluated"],
            "top1_correct": template_document["accuracy"]["top1_correct"],
            "top5_correct": template_document["accuracy"]["top5_correct"],
        },
    }
    provenance = {
        "windows_candidates": {"path": str(args.windows.resolve()), "sha256": file_sha256(args.windows)},
        "template_candidates": {"path": str(args.template.resolve()), "sha256": file_sha256(args.template)},
        "manual_evidence": {"path": str(args.manual.resolve()), "sha256": file_sha256(args.manual)},
        "preexisting_mapping": {"path": str(args.mapping.resolve()), "sha256": file_sha256(args.mapping)},
    }
    dump(
        args.ordered,
        {
            "schema_version": 1,
            "title": f"SO3 local {geometry_name} glyph mapping in union order",
            "status": "automatic_high_confidence_plus_manual_evidence",
            "summary": summary,
            "provenance": provenance,
            "conflicts": conflicts,
            "glyphs": ordered,
        },
    )
    dump(
        args.labels,
        {
            "schema_version": 1,
            "title": f"SO3 verified and high-confidence {geometry_name} SHA-keyed glyph labels",
            "summary": summary,
            "provenance": provenance,
            "labels": labels,
        },
    )
    dump(
        args.review,
        {
            "schema_version": 1,
            "title": f"SO3 {geometry_name} kanji glyphs requiring manual review",
            "summary": summary,
            "provenance": provenance,
            "glyphs": review,
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.ordered}")
    print(f"wrote {args.labels}")
    print(f"wrote {args.review}")


if __name__ == "__main__":
    main()

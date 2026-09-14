#!/usr/bin/env python3
"""Align Windows OCR word boxes to fixed SO3 glyph cells and vote variants."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "work/font_ocr/windows_rows/manifest.json"
DEFAULT_RAW = ROOT / "work/font_ocr/windows_ocr_raw.json"
DEFAULT_MAPPING = ROOT / "work/dialogue_locator/font_mapping_table.json"
DEFAULT_OUTPUT = ROOT / "work/font_ocr/windows_ocr_candidates.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def characters(text: str) -> list[str]:
    return list(unicodedata.normalize("NFC", text.replace(" ", "")))


def classify(character: str | None) -> str | None:
    if character is None or len(character) != 1:
        return None
    code = ord(character)
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return "cjk_ideograph"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"
    if character.isascii():
        return "ascii"
    return "other"


def align_words(words: list[dict[str, object]], cell_count: int, cell_width: int) -> tuple[dict[int, str], list[dict[str, object]]]:
    assignments: dict[int, str] = {}
    issues: list[dict[str, object]] = []
    for word in words:
        chars = characters(str(word["text"]))
        if not chars:
            continue
        left = float(word["x"])
        right = left + float(word["width"])
        start = max(0, min(cell_count - 1, int(left // cell_width)))
        end = max(0, min(cell_count - 1, int(max(left, right - 0.001) // cell_width)))
        span = end - start + 1
        if len(chars) == 1:
            columns = [max(0, min(cell_count - 1, int(((left + right) / 2) // cell_width)))]
        elif len(chars) == span:
            columns = list(range(start, end + 1))
        else:
            issues.append(
                {
                    "kind": "multi_character_span_mismatch",
                    "text": str(word["text"]),
                    "characters": chars,
                    "start_column": start,
                    "end_column": end,
                }
            )
            continue
        for column, character in zip(columns, chars):
            if column in assignments and assignments[column] != character:
                issues.append(
                    {
                        "kind": "cell_collision",
                        "column": column,
                        "previous": assignments[column],
                        "new": character,
                    }
                )
                assignments.pop(column, None)
            elif column not in assignments:
                assignments[column] = character
    return assignments, issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    raw_by_key = {
        (int(item["row_number"]), str(item["variant"])): item["ocr"]
        for item in raw["results"]
    }
    variants = [str(item) for item in manifest["variants"]]
    cell_width = int(manifest["output_cell_width"])
    glyph_width = int(manifest["glyph_width"])
    glyph_height = int(manifest["glyph_height"])
    geometry = [glyph_width, glyph_height]
    key_prefix = f"{glyph_width}x{glyph_height}:"

    known_by_hash: dict[str, set[str]] = collections.defaultdict(set)
    for entry in mapping["global_slots"] + mapping["local_slots"]:
        if entry["geometry"] == geometry and entry["unicode"]:
            known_by_hash[str(entry["bitmap_sha256"])].add(str(entry["unicode"]))

    candidates: list[dict[str, object]] = []
    issue_count = 0
    for row in manifest["rows"]:
        cells = row["cells"]
        aligned: dict[str, dict[int, str]] = {}
        row_issues: dict[str, list[dict[str, object]]] = {}
        for variant in variants:
            ocr = raw_by_key[(int(row["row_number"]), variant)]
            words = ocr.get("words") or []
            if isinstance(words, dict):
                words = [words]
            assignments, issues = align_words(words, len(cells), cell_width)
            aligned[variant] = assignments
            if issues:
                row_issues[variant] = issues
                issue_count += len(issues)

        for cell in cells:
            column = int(cell["column"])
            readings = {variant: aligned[variant].get(column) for variant in variants}
            votes = collections.Counter(value for value in readings.values() if value is not None)
            winner = None
            winner_votes = 0
            if votes:
                winner, winner_votes = votes.most_common(1)[0]
                tied = sum(1 for count in votes.values() if count == winner_votes) > 1
                if tied:
                    winner = None
                    winner_votes = 0
            observed = sum(value is not None for value in readings.values())
            if winner_votes == len(variants):
                status = "unanimous"
            elif winner_votes >= 2:
                status = "majority"
            elif observed == 0:
                status = "unrecognized"
            elif winner is None:
                status = "conflict"
            else:
                status = "single_variant"

            digest = str(cell["bitmap_sha256"])
            existing = sorted(known_by_hash.get(digest, set()))
            candidates.append(
                {
                    "key": f"{key_prefix}{digest}",
                    "bitmap_sha256": digest,
                    "union_index": int(cell["union_index"]),
                    "union_label": f"U{int(cell['union_index']):04X}",
                    "source_index": int(cell["source_index"]),
                    "ocr_by_variant": readings,
                    "ocr_consensus": winner,
                    "ocr_class": classify(winner),
                    "votes": winner_votes,
                    "variants_recognized": observed,
                    "status": status,
                    "existing_unicode": existing,
                    "matches_existing": len(existing) == 1 and winner == existing[0],
                }
            )

    known_single = [item for item in candidates if len(item["existing_unicode"]) == 1]
    known_recognized = [item for item in known_single if item["ocr_consensus"] is not None]
    known_correct = [item for item in known_recognized if item["matches_existing"]]
    status_counts = collections.Counter(str(item["status"]) for item in candidates)
    class_counts = collections.Counter(str(item["ocr_class"]) for item in candidates)
    summary = {
        "glyphs": len(candidates),
        "status_counts": dict(sorted(status_counts.items())),
        "class_counts": dict(sorted(class_counts.items())),
        "cjk_consensus": sum(item["ocr_class"] == "cjk_ideograph" for item in candidates),
        "known_single_character_glyphs": len(known_single),
        "known_recognized": len(known_recognized),
        "known_correct": len(known_correct),
        "known_precision_when_recognized": (
            len(known_correct) / len(known_recognized) if known_recognized else None
        ),
        "alignment_issue_count": issue_count,
    }
    document = {
        "schema_version": 1,
        "title": f"SO3 {glyph_width}px unique glyph Windows OCR candidates",
        "status": "automatic_candidates_not_human_reviewed",
        "geometry": geometry,
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": file_sha256(args.manifest),
        "raw_ocr": str(args.raw.resolve()),
        "raw_ocr_sha256": file_sha256(args.raw),
        "existing_mapping": str(args.mapping.resolve()),
        "existing_mapping_sha256": file_sha256(args.mapping),
        "summary": summary,
        "candidates": candidates,
    }
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()

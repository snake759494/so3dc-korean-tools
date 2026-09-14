#!/usr/bin/env python3
"""Rank CP932 kanji candidates for an SO3 square-glyph union atlas.

The game glyph and a rendered MS Gothic candidate are cropped to their ink
bounding boxes, fitted into a centered 20x20 region, converted to binary
masks, and compared with cosine similarity.  The default run covers the
archive-local union range U0291..U4713 and writes five candidates per glyph.

The output is deliberately write-once by default.  Choose another --output
path if a previous result exists; this avoids silently replacing user data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version


SCRIPT_VERSION = "1.1.0"
DEFAULT_CELL_SIZE = 24
DEFAULT_ATLAS_COLUMNS = 32
DEFAULT_UNION_START = 291
DEFAULT_UNION_END = 4713
DEFAULT_TOP_K = 5


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cp932_cjk_candidates() -> list[str]:
    """Return distinct two-byte CP932 CJK characters in code-point order."""

    candidates: set[str] = set()
    leads = list(range(0x81, 0xA0)) + list(range(0xE0, 0xFD))
    for lead in leads:
        for trail in range(0x40, 0xFD):
            if trail == 0x7F:
                continue
            try:
                char = bytes((lead, trail)).decode("cp932")
            except UnicodeDecodeError:
                continue
            if len(char) == 1 and 0x3400 <= ord(char) <= 0x9FFF:
                candidates.add(char)
    return sorted(candidates, key=ord)


def normalize_gray(
    image: np.ndarray,
    *,
    cell_size: int,
    fit_size: int,
    bbox_threshold: int,
) -> np.ndarray:
    """Crop ink, preserve its aspect, and center it in a square output cell."""

    gray = np.asarray(image, dtype=np.uint8)
    ys, xs = np.where(gray > bbox_threshold)
    if not len(xs):
        return np.zeros((cell_size, cell_size), dtype=np.float32)

    cropped = gray[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    scale = min(fit_size / cropped.shape[1], fit_size / cropped.shape[0])
    width = max(1, round(cropped.shape[1] * scale))
    height = max(1, round(cropped.shape[0] * scale))
    resized = cv2.resize(cropped, (width, height), interpolation=cv2.INTER_AREA)

    normalized = np.zeros((cell_size, cell_size), dtype=np.float32)
    x = (cell_size - width) // 2
    y = (cell_size - height) // 2
    normalized[y : y + height, x : x + width] = resized.astype(np.float32) / 255.0
    return normalized


def binary_unit_vector(gray: np.ndarray, *, threshold: float) -> np.ndarray:
    mask = (gray > threshold).astype(np.float32).reshape(-1)
    norm = float(np.linalg.norm(mask))
    return mask / norm if norm else mask


def render_candidate_matrix(
    candidates: list[str],
    *,
    cell_size: int,
    font_path: Path,
    font_index: int,
    render_size: int,
    canvas_size: int,
    fit_size: int,
    bbox_threshold: int,
    binary_threshold: float,
) -> np.ndarray:
    font = ImageFont.truetype(str(font_path), render_size, index=font_index)
    matrix = np.empty((len(candidates), cell_size * cell_size), dtype=np.float32)
    center = canvas_size // 2

    for index, char in enumerate(candidates):
        canvas = Image.new("L", (canvas_size, canvas_size), 0)
        ImageDraw.Draw(canvas).text(
            (center, center),
            char,
            font=font,
            fill=255,
            anchor="mm",
        )
        normalized = normalize_gray(
            np.asarray(canvas),
            cell_size=cell_size,
            fit_size=fit_size,
            bbox_threshold=bbox_threshold,
        )
        matrix[index] = binary_unit_vector(normalized, threshold=binary_threshold)
    return matrix


def read_union_rows(path: Path, *, cell_size: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"empty union CSV: {path}")
    required = {"width", "height", "union_index", "sha256"}
    missing = required.difference(rows[0])
    if missing:
        raise ValueError(f"union CSV lacks columns: {sorted(missing)}")
    for expected, row in enumerate(rows):
        actual = int(row["union_index"])
        if actual != expected:
            raise ValueError(f"non-contiguous union index at row {expected}: {actual}")
        if (int(row["width"]), int(row["height"])) != (cell_size, cell_size):
            raise ValueError(f"U{actual:04d} is not a {cell_size}x{cell_size} glyph")
    return rows


def extract_union_cell(
    atlas: np.ndarray,
    union_index: int,
    *,
    cell_size: int,
    atlas_columns: int,
    scale: int,
) -> np.ndarray:
    scaled_cell = cell_size * scale
    x = (union_index % atlas_columns) * scaled_cell
    y = (union_index // atlas_columns) * scaled_cell
    cell = atlas[y : y + scaled_cell, x : x + scaled_cell]
    if cell.shape != (scaled_cell, scaled_cell):
        raise ValueError(f"atlas does not contain complete U{union_index:04d}")
    if scale == 1:
        return cell.copy()
    # The catalog generator uses nearest-neighbour integer upscaling.  Sampling
    # one pixel from each block recovers the exact grayscale source cell.
    return cell[::scale, ::scale].copy()


def confirmed_direct_cjk(
    mapping_path: Path,
    rows: list[dict[str, str]],
    *,
    cell_size: int,
    union_start: int,
    union_end: int,
    candidate_set: set[str],
) -> tuple[dict[int, str], dict[str, Any]]:
    document = json.loads(mapping_path.read_text(encoding="utf-8"))
    labels_by_hash: dict[str, set[str]] = {}
    considered_slots = 0
    for section in ("global_slots", "local_slots"):
        for slot in document.get(section, []):
            char = slot.get("unicode")
            if (
                slot.get("unicode_source") != "direct_evidence"
                or slot.get("geometry") != [cell_size, cell_size]
                or not isinstance(char, str)
                or len(char) != 1
                or char not in candidate_set
            ):
                continue
            considered_slots += 1
            labels_by_hash.setdefault(slot["bitmap_sha256"], set()).add(char)

    union_by_hash = {row["sha256"].lower(): int(row["union_index"]) for row in rows}
    confirmed: dict[int, str] = {}
    conflicting_hashes: dict[str, list[str]] = {}
    missing_hashes: list[str] = []
    for digest, chars in labels_by_hash.items():
        if len(chars) != 1:
            conflicting_hashes[digest] = sorted(chars, key=ord)
            continue
        union_index = union_by_hash.get(digest.lower())
        if union_index is None:
            missing_hashes.append(digest)
            continue
        if union_start <= union_index <= union_end:
            confirmed[union_index] = next(iter(chars))

    metadata = {
        "definition": (
            f"{cell_size}x{cell_size} CJK slot with unicode_source=direct_evidence; identical bitmap "
            "propagation is excluded from accuracy"
        ),
        "direct_slots_considered": considered_slots,
        "distinct_labeled_hashes": len(labels_by_hash),
        "labels_in_requested_union_range": len(confirmed),
        "conflicting_hashes": conflicting_hashes,
        "hashes_missing_from_union_csv": missing_hashes,
    }
    return confirmed, metadata


def rounded(value: float) -> float:
    return round(float(value), 8)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    root = script_dir.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--atlas",
        type=Path,
        default=root / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.png",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=root / "work/kanji_deep/mclib_catalog/unique_24px_glyphs.csv",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=root / "work/dialogue_locator/font_mapping_table.json",
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=Path(r"C:\Windows\Fonts\msgothic.ttc"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=script_dir / "template_match_candidates.json",
    )
    parser.add_argument("--union-start", type=int, default=DEFAULT_UNION_START)
    parser.add_argument(
        "--union-end",
        type=int,
        default=None,
        help=f"inclusive end index (default: final CSV row; original 24px end is {DEFAULT_UNION_END})",
    )
    parser.add_argument("--cell-size", type=int, default=DEFAULT_CELL_SIZE)
    parser.add_argument("--atlas-columns", type=int, default=DEFAULT_ATLAS_COLUMNS)
    parser.add_argument("--atlas-scale", type=int, default=2)
    parser.add_argument("--font-index", type=int, default=0)
    parser.add_argument(
        "--render-size",
        type=int,
        default=None,
        help="font render size (default: cell size)",
    )
    parser.add_argument(
        "--canvas-size",
        type=int,
        default=None,
        help="temporary render canvas side (default: 4 * cell size)",
    )
    parser.add_argument(
        "--fit-size",
        type=int,
        default=None,
        help="normalized ink-box side (default: cell size - 4)",
    )
    parser.add_argument("--bbox-threshold", type=int, default=18)
    parser.add_argument("--binary-threshold", type=float, default=0.2)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--batch-size", type=int, default=128)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.cell_size < 8 or args.atlas_columns < 1:
        raise ValueError("cell-size >= 8 and atlas-columns >= 1 are required")
    if args.render_size is None:
        args.render_size = args.cell_size
    if args.canvas_size is None:
        args.canvas_size = args.cell_size * 4
    if args.fit_size is None:
        args.fit_size = args.cell_size - 4
    if not (1 <= args.fit_size <= args.cell_size):
        raise ValueError("fit-size must be between 1 and cell-size")
    inputs = (args.atlas, args.csv, args.mapping, args.font)
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    if args.output.exists():
        raise FileExistsError(
            f"refusing to overwrite existing output: {args.output}; choose a new --output"
        )
    if args.atlas_scale < 1 or args.top_k < 2 or args.batch_size < 1:
        raise ValueError("atlas-scale >= 1, top-k >= 2, and batch-size >= 1 are required")

    rows = read_union_rows(args.csv, cell_size=args.cell_size)
    union_end = len(rows) - 1 if args.union_end is None else args.union_end
    if not (0 <= args.union_start <= union_end < len(rows)):
        raise ValueError(
            f"union range {args.union_start}..{union_end} is outside 0..{len(rows)-1}"
        )

    atlas_image = Image.open(args.atlas).convert("L")
    atlas = np.asarray(atlas_image)
    expected_width = args.atlas_columns * args.cell_size * args.atlas_scale
    required_rows = (len(rows) + args.atlas_columns - 1) // args.atlas_columns
    expected_height = required_rows * args.cell_size * args.atlas_scale
    if atlas.shape != (expected_height, expected_width):
        raise ValueError(
            f"unexpected atlas dimensions {atlas.shape[::-1]}; "
            f"expected {(expected_width, expected_height)}"
        )

    candidates = cp932_cjk_candidates()
    if len(candidates) != 6682:
        raise ValueError(f"unexpected CP932 CJK candidate count: {len(candidates)}")
    candidate_set = set(candidates)
    candidate_matrix = render_candidate_matrix(
        candidates,
        cell_size=args.cell_size,
        font_path=args.font,
        font_index=args.font_index,
        render_size=args.render_size,
        canvas_size=args.canvas_size,
        fit_size=args.fit_size,
        bbox_threshold=args.bbox_threshold,
        binary_threshold=args.binary_threshold,
    )

    confirmed, confirmed_metadata = confirmed_direct_cjk(
        args.mapping,
        rows,
        cell_size=args.cell_size,
        union_start=args.union_start,
        union_end=union_end,
        candidate_set=candidate_set,
    )

    union_indices = list(range(args.union_start, union_end + 1))
    matches: list[dict[str, Any]] = []
    evaluation_cases: list[dict[str, Any]] = []
    top1_hits = 0
    top5_hits = 0
    rank_sum = 0

    for batch_start in range(0, len(union_indices), args.batch_size):
        batch_indices = union_indices[batch_start : batch_start + args.batch_size]
        target_matrix = np.empty(
            (len(batch_indices), args.cell_size * args.cell_size), dtype=np.float32
        )
        ink_pixels: list[int] = []
        for offset, union_index in enumerate(batch_indices):
            cell = extract_union_cell(
                atlas,
                union_index,
                cell_size=args.cell_size,
                atlas_columns=args.atlas_columns,
                scale=args.atlas_scale,
            )
            normalized = normalize_gray(
                cell,
                cell_size=args.cell_size,
                fit_size=args.fit_size,
                bbox_threshold=args.bbox_threshold,
            )
            binary = normalized > args.binary_threshold
            ink_pixels.append(int(binary.sum()))
            target_matrix[offset] = binary_unit_vector(
                normalized, threshold=args.binary_threshold
            )

        scores = target_matrix @ candidate_matrix.T
        # Candidates are already in Unicode code-point order.  Stable sorting
        # therefore gives a deterministic code-point tie-break.
        order = np.argsort(-scores, axis=1, kind="stable")[:, : args.top_k]

        for offset, union_index in enumerate(batch_indices):
            ranked: list[dict[str, Any]] = []
            for rank, candidate_index in enumerate(order[offset], start=1):
                char = candidates[int(candidate_index)]
                ranked.append(
                    {
                        "rank": rank,
                        "character": char,
                        "codepoint": f"U+{ord(char):04X}",
                        "score": rounded(scores[offset, candidate_index]),
                    }
                )
            margin = ranked[0]["score"] - ranked[1]["score"]
            expected = confirmed.get(union_index)
            entry: dict[str, Any] = {
                "union_id": f"U{union_index:04d}",
                "union_index": union_index,
                "bitmap_sha256": rows[union_index]["sha256"].lower(),
                "ink_pixels_after_normalization": ink_pixels[offset],
                "top5": ranked,
                "margin_top1_top2": rounded(margin),
                "confirmed_unicode": expected,
            }
            matches.append(entry)

            if expected is not None:
                expected_rank = next(
                    (item["rank"] for item in ranked if item["character"] == expected),
                    None,
                )
                top1_hits += expected_rank == 1
                top5_hits += expected_rank is not None
                rank_sum += expected_rank if expected_rank is not None else len(candidates) + 1
                evaluation_cases.append(
                    {
                        "union_id": entry["union_id"],
                        "expected": expected,
                        "predicted": ranked[0]["character"],
                        "expected_rank_within_top5": expected_rank,
                        "top1_score": ranked[0]["score"],
                        "margin_top1_top2": entry["margin_top1_top2"],
                    }
                )

    evaluation_count = len(evaluation_cases)
    document = {
        "schema_version": 1,
        "tool": {
            "name": "template_match_kanji.py",
            "version": SCRIPT_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "inputs": {
            "atlas": {
                "path": str(args.atlas.resolve()),
                "sha256": sha256_file(args.atlas),
                "mode": atlas_image.mode,
                "dimensions": list(atlas_image.size),
            },
            "union_csv": {
                "path": str(args.csv.resolve()),
                "sha256": sha256_file(args.csv),
                "row_count": len(rows),
            },
            "confirmed_mapping": {
                "path": str(args.mapping.resolve()),
                "sha256": sha256_file(args.mapping),
            },
            "font": {
                "path": str(args.font.resolve()),
                "sha256": sha256_file(args.font),
                "face_index": args.font_index,
            },
        },
        "settings": {
            "requested_union_range": {
                "start": args.union_start,
                "end": union_end,
                "inclusive": True,
                "count": len(union_indices),
            },
            "atlas": {
                "columns": args.atlas_columns,
                "cell_size": [args.cell_size, args.cell_size],
                "integer_scale": args.atlas_scale,
                "downsample": "take upper-left pixel of each nearest-neighbour scale block",
            },
            "candidate_set": {
                "encoding": "cp932",
                "lead_byte_ranges": ["0x81..0x9F", "0xE0..0xFC"],
                "trail_byte_range": "0x40..0xFC excluding 0x7F",
                "unicode_filter": "U+3400..U+9FFF",
                "deduplication": "Unicode scalar",
                "ordering": "ascending Unicode code point",
                "count": len(candidates),
            },
            "rendering": {
                "engine": "Pillow ImageFont/FreeType",
                "font_size_px": args.render_size,
                "canvas_size_px": [args.canvas_size, args.canvas_size],
                "position": [args.canvas_size // 2, args.canvas_size // 2],
                "anchor": "mm",
                "fill": 255,
            },
            "normalization": {
                "ink_bbox_threshold_0_255": args.bbox_threshold,
                "fit_box": [args.fit_size, args.fit_size],
                "output_box": [args.cell_size, args.cell_size],
                "aspect_ratio": "preserved",
                "centering": "integer floor",
                "resize_interpolation": "OpenCV INTER_AREA",
                "binary_threshold_0_1": args.binary_threshold,
            },
            "matching": {
                "feature": "L2-normalized flattened binary ink mask",
                "similarity": "cosine (dot product of unit vectors)",
                "top_k": args.top_k,
                "tie_break": "ascending Unicode code point via stable sort",
                "margin": "top1_score - top2_score",
                "batch_size": args.batch_size,
            },
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
            "opencv": cv2.__version__,
            "pillow": pillow_version,
        },
        "accuracy": {
            **confirmed_metadata,
            "evaluated": evaluation_count,
            "top1_correct": top1_hits,
            "top1_accuracy": rounded(top1_hits / evaluation_count)
            if evaluation_count
            else None,
            "top5_correct": top5_hits,
            "top5_accuracy": rounded(top5_hits / evaluation_count)
            if evaluation_count
            else None,
            "mean_capped_rank": rounded(rank_sum / evaluation_count)
            if evaluation_count
            else None,
            "cases": evaluation_cases,
        },
        "matches": matches,
        "notes": [
            "Template rankings are OCR candidates, not proof of character identity.",
            "Low-margin results and visually similar kanji require message-context review.",
            "Accuracy uses only pre-existing direct evidence and excludes hash-propagated labels.",
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {args.output} ({len(matches)} glyphs, {len(candidates)} candidates, "
        f"accuracy {top1_hits}/{evaluation_count} top-1, {top5_hits}/{evaluation_count} top-5)"
    )


if __name__ == "__main__":
    main()

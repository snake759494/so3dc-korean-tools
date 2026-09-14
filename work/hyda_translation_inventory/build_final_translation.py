#!/usr/bin/env python3
"""Merge the three reviewed Hyda translation chunks into release data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PUBLIC_OUTPUT = ROOT / "publish/so3dc-korean-tools/translations/hyda_ko.json"
BUILD_OUTPUT = HERE / "hyda_ko_build.json"

SPEAKER_NORMALIZATION = {
    "45⟦L:383⟧で立つ男": "45도로 선 남자",
    "キレイなオバサン": "고운 아주머니",
    "軽そうなお姉さん": "가벼워 보이는 아가씨",
    "エクスペルの男性": "엑스펠 출신 남성",
    "偉そうな地球人": "거드름 피우는 지구인",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_entries() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    overlay = json.loads((HERE / "ko_chunk_03.json").read_text(encoding="utf-8"))[
        "translations"
    ]
    for chunk_id in (1, 2, 3):
        path = HERE / f"translation_chunk_{chunk_id:02d}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        for source in document["entries"]:
            row = dict(source)
            if chunk_id == 3:
                translated = overlay[row["id"]]
                row["speaker_korean"] = translated["speaker_korean"]
                row["korean"] = translated["korean"]
            if row["speaker_japanese"] in SPEAKER_NORMALIZATION:
                row["speaker_korean"] = SPEAKER_NORMALIZATION[row["speaker_japanese"]]
            if row["speaker_japanese"] is None:
                row["speaker_korean"] = None
            result.append(row)
    result.sort(key=lambda item: int(item["index"]))
    return result


def validate(rows: list[dict[str, object]]) -> None:
    if len(rows) != 434:
        raise ValueError(f"translation count {len(rows)} != 434")
    if [int(row["index"]) for row in rows] != list(range(1, 435)):
        raise ValueError("translation indices are not exactly 1..434")
    ids = [str(row["id"]) for row in rows]
    digests = [str(row["exact_sha256"]) for row in rows]
    if len(set(ids)) != len(ids) or len(set(digests)) != len(digests):
        raise ValueError("duplicate translation id or exact SHA-256")
    occurrence_count = 0
    occurrence_keys: set[tuple[int, int, int]] = set()
    for row in rows:
        korean = str(row["korean"])
        if not korean:
            raise ValueError(f"empty Korean body: {row['id']}")
        lines = korean.split("\n")
        if len(lines) != int(row["original_line_count"]):
            raise ValueError(f"line count mismatch: {row['id']}")
        if any(len(line) > 26 for line in lines):
            raise ValueError(f"line exceeds 26 characters: {row['id']}")
        if "⟦" in korean or "⟧" in korean:
            raise ValueError(f"unresolved glyph marker: {row['id']}")
        if row["speaker_japanese"] is not None and not row["speaker_korean"]:
            raise ValueError(f"explicit speaker lacks Korean text: {row['id']}")
        occurrences = row["occurrences"]
        if not occurrences:
            raise ValueError(f"translation has no occurrences: {row['id']}")
        occurrence_count += len(occurrences)
        for item in occurrences:
            key = (
                int(item["archive_id"]),
                int(item["stream_id"]),
                int(item["message_id"]),
            )
            if key in occurrence_keys:
                raise ValueError(f"duplicate physical occurrence: {key}")
            occurrence_keys.add(key)
    if occurrence_count != 653 or len(occurrence_keys) != 653:
        raise ValueError(
            f"physical occurrence coverage {occurrence_count}/{len(occurrence_keys)} != 653"
        )


def main() -> None:
    rows = load_entries()
    validate(rows)
    common = {
        "schema_version": 1,
        "title": "Star Ocean 3 DC Disc 1 Hyda Korean translation",
        "status": "translation_complete_static_runtime_unverified",
        "translation_count": 434,
        "physical_occurrence_count": 653,
        "font": "NanumSquareNeo-cBd.ttf",
        "font_sha256": "4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767",
    }
    public_entries = [
        {
            "id": row["id"],
            "index": row["index"],
            "exact_sha256": row["exact_sha256"],
            "speaker_korean": row["speaker_korean"],
            "korean": row["korean"],
            "source_line_count": row["original_line_count"],
            "occurrences": row["occurrences"],
        }
        for row in rows
    ]
    build_entries = [
        {
            "id": row["id"],
            "index": row["index"],
            "exact_sha256": row["exact_sha256"],
            "japanese": row["japanese"],
            "speaker_korean": row["speaker_korean"],
            "korean": row["korean"],
            "source_line_count": row["original_line_count"],
            "occurrences": row["occurrences"],
        }
        for row in rows
    ]
    PUBLIC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_OUTPUT.write_text(
        json.dumps({**common, "translations": public_entries}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    BUILD_OUTPUT.write_text(
        json.dumps({**common, "translations": build_entries}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "translations": len(rows),
                "occurrences": sum(len(row["occurrences"]) for row in rows),
                "public_output": str(PUBLIC_OUTPUT),
                "public_sha256": sha256_file(PUBLIC_OUTPUT),
                "build_output": str(BUILD_OUTPUT),
                "build_sha256": sha256_file(BUILD_OUTPUT),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

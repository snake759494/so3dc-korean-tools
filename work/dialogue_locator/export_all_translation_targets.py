"""Export every validated SO3 mclib message segment to a JSON catalog.

The source analysis deliberately keeps Japanese glyph-code streams lossless.
Only strings confirmed by the existing render/token evidence are assigned
Unicode text; all other records remain explicit decode targets instead of
being guessed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "work" / "mclib_all_decode" / "unique_exact_segments.csv"
SUMMARY = ROOT / "work" / "mclib_all_decode" / "summary.json"
OUTPUT = ROOT / "work" / "dialogue_locator" / "all_dialogue_targets.json"


CONFIRMED = {
    (1220, 6194, 3, 393): {
        "kind": "signage",
        "source_text": "1階客室通路",
        "translation_preview": "1층 객실 통로",
        "confidence": "rendered_original_png",
    },
    (1220, 6194, 5, 406): {
        "kind": "opening_dialogue",
        "source_text": "ソフィア\n「ねぇ、フェイト、\nココのホテルってさぁ…。\n104号室が無いよね。\nこれって何でなのかな?",
        "translation_preview": "소피아\n「페이트, 봐.\n이 호텔은…\n104호가 없어.\n왜?",
        "confidence": "token_decode_and_render_evidence",
    },
    (1220, 6194, 14, 654): {
        "kind": "hotel_notice",
        "source_text": "当ホテルのプライベートビーチには\n一切の人工生物が使用されておりません。",
        "translation_preview": "당ホテルのプライベートビーチには\n일절の인공생물が사용されておりません。",
        "confidence": "rendered_original_png",
    },
    (66, 57, 393, 2268): {"kind": "name", "source_text": "澤村 栄公", "confidence": "rendered_original_png"},
    (88, 205, 50554, 10811): {"kind": "item", "source_text": "真手打ちそば", "confidence": "rendered_original_png"},
    (3454, 60027, 1247, 8307): {"kind": "status_text", "source_text": "HP回復77777", "confidence": "rendered_original_png"},
    (94, 252, 76044, 59482): {"kind": "location", "source_text": "お姫様の実験場", "confidence": "rendered_original_png"},
    (1688, 25688, 1, 0): {"kind": "location", "source_text": "リーベルの部屋", "confidence": "rendered_original_png"},
    (3454, 60029, 267, 4331): {"kind": "item", "source_text": "ダークハンター", "confidence": "rendered_original_png"},
    (85, 201, 13602, 855): {"kind": "location", "source_text": "鉱山町カルサアB", "confidence": "rendered_original_png"},
    (77, 70, 50113, 3244): {"kind": "item", "source_text": "ブレイズシンボル", "confidence": "rendered_original_png"},
    (87, 203, 76327, 75263): {"kind": "item", "source_text": "鋼製の半身鎧", "confidence": "rendered_original_png"},
    (38, 19, 1134, 16744): {"kind": "battle_text", "source_text": "Preemptive Attack", "confidence": "rendered_original_png"},
    (88, 205, 70676, 59059): {"kind": "battle_text", "source_text": "チンケな者どもを瞬殺する", "confidence": "rendered_original_png"},
    (2737, 51283, 1, 0): {"kind": "location", "source_text": "スフィア211　1階", "confidence": "rendered_original_png"},
    (85, 201, 13825, 585): {"kind": "dialogue", "source_text": "「あー地上も久しぶりだわねぇ", "confidence": "rendered_original_png"},
    (1538, 19973, 1, 0): {"kind": "location", "source_text": "水没都市 サーフェリオ", "confidence": "rendered_original_png"},
    (79, 85, 6713, 7704): {"kind": "ability", "source_text": "アドレー ： 必殺技 8", "confidence": "rendered_original_png"},
    (95, 280, 70118, 46633): {"kind": "ability", "source_text": "風属性呪紋ダメージ+30%", "confidence": "rendered_original_png"},
    (85, 201, 13911, 1759): {"kind": "dialogue", "source_text": "「俺は、", "confidence": "rendered_original_png"},
    (6068, 61963, 1000, 16540): {"kind": "battle_text", "source_text": "戦闘中 敵を気絶させる 広範囲", "confidence": "rendered_original_png"},
    (1775, 28119, 10418, 5600): {"kind": "location", "source_text": "General store Fairy Tear", "confidence": "rendered_original_png"},
    (2645, 46948, 139, 1475): {"kind": "dialogue", "source_text": "また…?", "confidence": "rendered_glyphs_after_control_skip"},
}


def number(value: str) -> int:
    return int(value, 10)


def main() -> None:
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    records = []
    with INPUT.open(newline="", encoding="utf-8-sig") as handle:
        for ordinal, row in enumerate(csv.DictReader(handle), start=1):
            key = (
                number(row["representative_archive_id"]),
                number(row["representative_stream_id"]),
                number(row["representative_message_id"]),
                number(row["representative_offset"]),
            )
            confirmed = CONFIRMED.get(key)
            record = {
                "ordinal": ordinal,
                "exact_sha256": row["exact_sha256"],
                "segment_bytes": number(row["segment_bytes"]),
                "representative": {
                    "archive_id": key[0],
                    "stream_id": key[1],
                    "message_id": key[2],
                    "text_offset": key[3],
                    "container_file": Path(row["representative_path"]).name,
                },
                "raw_bytes_hex": row["representative_hex"],
                "occurrence_count": number(row["all_mapping_occurrences"]),
                "unique_container_occurrence_count": number(
                    row["unique_container_mapping_occurrences"]
                ),
                "translation_status": "confirmed" if confirmed else "needs_glyph_decode",
                "source_text": confirmed["source_text"] if confirmed else None,
            }
            if confirmed:
                record.update({key: value for key, value in confirmed.items() if key != "source_text"})
            records.append(record)

    document = {
        "schema_version": 1,
        "title": "Star Ocean 3 DC Disc 1 전체 대사/텍스트 번역 대상 카탈로그",
        "status": "exhaustive_raw_message_catalog",
        "game": {
            "title": "Star Ocean 3 Till the End of Time Director's Cut",
            "platform": "PlayStation 2",
            "disc": 1,
        },
        "scope": {
            "archive_occurrences": summary["containers"]["occurrences"],
            "mapping_message_occurrences": summary["mappings_and_messages"][
                "mapping_rows_all_container_occurrences"
            ],
            "unique_exact_message_segments": len(records),
            "source_csv": "work/mclib_all_decode/unique_exact_segments.csv",
            "deduplication": "exact message bytes; occurrence_count preserves repetitions",
        },
        "decode_policy": {
            "message_boundaries": summary["method"]["message_boundaries"],
            "glyph_code": summary["method"]["glyph_code"],
            "verified_controls": ["8080 newline", "8A80 + little-endian float32 scale"],
            "unknown_controls": "preserved in raw_bytes_hex; no parameter lengths guessed",
            "unicode_policy": "only confirmed render/token strings receive source_text",
        },
        "length_sample_20": [
            {
                "length_bytes": record["segment_bytes"],
                "source_text": record["source_text"],
                "archive_id": record["representative"]["archive_id"],
                "stream_id": record["representative"]["stream_id"],
                "message_id": record["representative"]["message_id"],
                "text_offset": record["representative"]["text_offset"],
            }
            for length in range(10, 30)
            for record in records
            if record["segment_bytes"] == length
            and record.get("source_text")
            and not (
                record["representative"]["archive_id"] == 1220
                and record["representative"]["stream_id"] == 6194
                and record["representative"]["message_id"] == 3
            )
            and next(
                (
                    candidate
                    for candidate in records
                    if candidate["segment_bytes"] == length
                    and candidate.get("source_text")
                    and not (
                        candidate["representative"]["archive_id"] == 1220
                        and candidate["representative"]["stream_id"] == 6194
                        and candidate["representative"]["message_id"] == 3
                    )
                ),
                None,
            )
            is record
        ],
        "records": records,
        "limitations": [
            "이 파일은 모든 메시지 경계와 원문 바이트를 포함하지만, 게임의 글리프 인덱스 자체에는 Unicode 문자표가 없어서 대부분 source_text가 비어 있다.",
            "needs_glyph_decode 레코드는 일본어 대사 후보이며 raw_bytes_hex를 기반으로 글리프 OCR/대응표 분석을 진행해야 한다.",
            "미확정 high/high 제어 뒤의 바이트도 손실 없이 raw_bytes_hex에 보존했다.",
        ],
    }
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    confirmed_count = sum(1 for record in records if record["translation_status"] == "confirmed")
    print(f"wrote {OUTPUT} ({len(records)} records; {confirmed_count} confirmed text strings)")


if __name__ == "__main__":
    main()

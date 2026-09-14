#!/usr/bin/env python3
"""Build the redistributable Hyda patch manifest from the private catalogue.

The output contains hashes and structural offsets only.  Japanese strings,
encoded message bytes, and extracted game resources are deliberately omitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


EXPECTED_OCCURRENCES = 653
EXPECTED_UNIQUE_SEGMENTS = 434
BANK_STREAMS = {
    1204: 5829, 1206: 5984, 1208: 6005, 1210: 6040,
    1212: 6061, 1214: 6120, 1216: 6141, 1218: 6169,
    1220: 6194, 1222: 6258, 1224: 6438, 1226: 6459,
    1228: 6543, 1230: 6621, 1232: 6653, 1234: 6684,
    1236: 6715, 1243: 6989, 1245: 7017, 1247: 7081,
    1249: 7154, 1251: 7227, 1253: 7300, 1255: 7404,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def build_manifest(
    source_path: Path,
    *,
    expected_occurrences: int = EXPECTED_OCCURRENCES,
    expected_unique_segments: int = EXPECTED_UNIQUE_SEGMENTS,
    expected_banks: int | None = None,
) -> dict[str, object]:
    document = json.loads(source_path.read_text(encoding="utf-8"))
    dialogues = document.get("dialogues")
    require(isinstance(dialogues, list), "source dialogues array missing")
    require(len(dialogues) == expected_occurrences, "source occurrence count mismatch")
    occurrences: list[dict[str, object]] = []
    seen: set[tuple[int, int, int]] = set()
    hashes: set[str] = set()
    banks: dict[tuple[int, int], dict[str, object]] = {}
    for row in dialogues:
        require(isinstance(row, dict), "source dialogue row is not an object")
        archive_id = int(row["archive_id"])
        stream_id = int(row["stream_id"])
        message_id = int(row["message_id"])
        require(BANK_STREAMS.get(archive_id) == stream_id, "row outside verified banks")
        identity = (archive_id, stream_id, message_id)
        require(identity not in seen, "duplicate physical occurrence")
        seen.add(identity)
        digest = str(row["exact_sha256"]).lower()
        require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "invalid segment hash")
        raw = bytes.fromhex(str(row["raw_bytes_hex"]))
        require(hashlib.sha256(raw).hexdigest() == digest, "segment hash mismatch")
        hashes.add(digest)
        speaker = row.get("speaker")
        evidence = row.get("evidence")
        require(isinstance(speaker, dict) and isinstance(speaker.get("mode"), str),
                "speaker mode missing")
        require(isinstance(evidence, dict), "evidence missing")
        body_start = evidence.get("body_start_offset")
        require(isinstance(body_start, int) and body_start >= 0, "body offset missing")
        source_text = row.get("japanese")
        require(isinstance(source_text, str), "decoded source text missing")
        source_line_count = len(
            source_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        )
        compact_evidence: dict[str, int] = {"body_start_offset": body_start}
        for key in ("speaker_field_start_offset", "speaker_field_end_offset"):
            value = evidence.get(key)
            if value is not None:
                require(isinstance(value, int) and value >= 0, f"bad {key}")
                compact_evidence[key] = value
        occurrence = {
            "archive_id": archive_id,
            "stream_id": stream_id,
            "message_id": message_id,
            "exact_sha256": digest,
            "speaker": {"mode": speaker["mode"]},
            "evidence": compact_evidence,
            "source_line_count": source_line_count,
            "file_sha256": str(row["file_sha256"]),
            "font_fingerprint_sha256": str(row["font_fingerprint_sha256"]),
        }
        occurrences.append(occurrence)
        bank_key = (archive_id, stream_id)
        fingerprint = {
            "archive_id": archive_id,
            "stream_id": stream_id,
            "file_sha256": str(row["file_sha256"]),
            "font_fingerprint_sha256": str(row["font_fingerprint_sha256"]),
        }
        if bank_key in banks:
            require(banks[bank_key] == fingerprint, "bank fingerprint conflict")
        else:
            banks[bank_key] = fingerprint

    require(len(hashes) == expected_unique_segments, "unique segment count mismatch")
    expected_bank_count = len(BANK_STREAMS) if expected_banks is None else expected_banks
    require(len(banks) == expected_bank_count, "event bank count mismatch")
    occurrences.sort(key=lambda row: (
        int(row["archive_id"]), int(row["stream_id"]), int(row["message_id"])
    ))
    event_banks = [banks[key] for key in sorted(banks)]
    return {
        "schema_version": 1,
        "title": "SO3 DC Disc 1 Hyda Korean patch structural manifest",
        "content_policy": "hashes_and_offsets_only_no_extracted_text_or_game_bytes",
        "source_catalogue_sha256": sha256_file(source_path),
        "summary": {
            "event_bank_count": len(event_banks),
            "occurrence_count": len(occurrences),
            "unique_exact_segment_count": len(hashes),
        },
        "event_banks": event_banks,
        "occurrences": occurrences,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_catalogue", type=Path)
    parser.add_argument("output_manifest", type=Path)
    args = parser.parse_args()
    if args.source_catalogue.resolve() == args.output_manifest.resolve():
        parser.error("output manifest aliases source catalogue")
    try:
        manifest = build_manifest(args.source_catalogue)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a control-safe translation inventory for the 653 Hyda occurrences.

This script does not translate or patch the ISO.  It turns the confirmed
dialogue whitelist into three review artifacts:

* one translation template per exact original byte segment;
* one explicit patch target per archive/stream/message occurrence;
* one problem list for source glyphs or bytecode requiring manual handling.

The exact source hash is intentionally part of every key.  Offsets alone are
not stable after the first variable-length replacement in an mclib.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "work/early_game_dialogue/hyda_hotel_dialogue.json"
MAPPING = ROOT / "work/dialogue_locator/font_mapping_table.json"
OUT_DIR = ROOT / "work/hyda_translation_inventory"
OUT_INVENTORY = OUT_DIR / "inventory.json"
OUT_TEMPLATES = OUT_DIR / "translation_templates.json"
OUT_CSV = OUT_DIR / "translation_templates.csv"
OUT_PROBLEMS = OUT_DIR / "problem_records.json"
OUT_CHUNKS = [OUT_DIR / f"translation_chunk_{index:02d}.json" for index in range(1, 4)]


CHARACTER_NAMES = {
    1: "フェイト",
    2: "ソフィア",
    3: "マリア",
    4: "クリフ",
    5: "ネル",
    6: "アルベル",
    7: "ロジャー",
    8: "スフレ",
    9: "アドレー",
    10: "ミラージュ",
}

CONTROL_SIZES = {
    "8080": 2,
    "8180": 2,
    "8280": 3,
    "8480": 2,
    "8580": 6,
    "8680": 6,
    "8780": 2,
    "8880": 3,
    "8980": 2,
    "8a80": 6,
    "8b80": 2,
    "9080": 2,
    "9280": 3,
    "9380": 3,
    "9480": 6,
    "9580": 6,
    "9c80": 5,
    "9d80": 3,
    "9e80": 6,
    "9f80": 2,
}

CONTROL_ROLES = {
    "8080": "newline",
    "8180": "message_block_or_page_boundary",
    "8280": "opaque_control_with_u8_operand",
    "8480": "message_flow_control",
    "8580": "layout_float32",
    "8680": "layout_float32",
    "8780": "message_flow_control",
    "8880": "style_index_u8",
    "8980": "message_flow_control",
    "8a80": "scale_float32",
    "8b80": "message_flow_control",
    "9080": "ruby_base_begin",
    "9180": "ruby_metadata_zero_terminated",
    "9280": "opaque_control_with_u8_operand",
    "9380": "character_reference_u8",
    "9480": "opaque_control_with_u32_operand",
    "9580": "opaque_control_with_u32_operand",
    "9c80": "opaque_control_with_3byte_operand",
    "9d80": "opaque_control_with_u8_operand",
    "9e80": "event_cue_u16_group_u16_sequence",
    "9f80": "message_flow_control",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def glyph_code(data: bytes, offset: int) -> tuple[int | None, int]:
    if offset >= len(data) or data[offset] == 0:
        return None, 0
    first = data[offset]
    if first < 0x80:
        return first, 1
    if offset + 1 < len(data) and data[offset + 1] < 0x80:
        return (first & 0x7F) | (data[offset + 1] << 7), 2
    return None, 0


def source_category(source: str | None) -> str:
    if source is None:
        return "unmapped"
    if source.startswith("font_ocr:"):
        return "font_ocr"
    return source


class MappingLookup:
    def __init__(self, document: dict[str, Any]) -> None:
        self.global_slots = {
            int(row["code"]): row for row in document["global_slots"]
        }
        self.local_slots = {
            (int(row["archive_id"]), int(row["stream_id"]), int(row["code"])): row
            for row in document["local_slots"]
        }

    def lookup(self, record: dict[str, Any], code: int) -> dict[str, Any] | None:
        local_base = int(record["font"]["local_base"])
        if local_base != 1 and code < local_base:
            return self.global_slots.get(code)
        return self.local_slots.get(
            (int(record["archive_id"]), int(record["stream_id"]), code)
        )


def append_text_token(
    tokens: list[dict[str, Any]],
    code: int,
    raw: bytes,
    mapping: dict[str, Any] | None,
) -> None:
    character = mapping.get("unicode") if mapping else None
    mapping_source = mapping.get("unicode_source") if mapping else None
    source_kind = "global" if mapping and int(mapping["archive_id"]) == 8 else "local"
    marker = character if character else f"⟦{source_kind[0].upper()}:{code}⟧"
    if (
        tokens
        and tokens[-1]["type"] == "text"
        and tokens[-1]["mapping_source"] == mapping_source
        and tokens[-1]["source_kind"] == source_kind
    ):
        token = tokens[-1]
        token["source"] += marker
        token["glyph_codes"].append(code)
        token["raw_hex"] += raw.hex()
        return
    tokens.append(
        {
            "type": "text",
            "source": marker,
            "glyph_codes": [code],
            "raw_hex": raw.hex(),
            "source_kind": source_kind,
            "mapping_source": mapping_source,
        }
    )


def tokenise_body(
    record: dict[str, Any], lookup: MappingLookup
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = bytes.fromhex(record["body_raw_bytes_hex"])
    tokens: list[dict[str, Any]] = []
    offset = 0
    control_signature: list[str] = []
    unknown_control = None
    terminator_offset = None

    while offset < len(data):
        if data[offset] == 0:
            terminator_offset = offset
            tokens.append({"type": "terminator", "raw_hex": "00"})
            offset += 1
            break

        code, size = glyph_code(data, offset)
        if code is not None:
            mapping = lookup.lookup(record, code)
            append_text_token(tokens, code, data[offset : offset + size], mapping)
            offset += size
            continue

        if offset + 1 >= len(data):
            unknown_control = {"offset": offset, "opcode": data[offset:].hex()}
            break
        opcode = data[offset : offset + 2].hex()
        if opcode == "9180":
            end = data.find(b"\x00", offset + 2)
            if end < 0:
                unknown_control = {"offset": offset, "opcode": opcode}
                break
            raw = data[offset : end + 1]
            tokens.append(
                {
                    "type": "control",
                    "opcode": opcode,
                    "role": CONTROL_ROLES[opcode],
                    "raw_hex": raw.hex(),
                    "preservation": "preserve_exact_as_part_of_ruby_pair",
                }
            )
            control_signature.append(raw.hex())
            offset = end + 1
            continue

        size = CONTROL_SIZES.get(opcode)
        if size is None or offset + size > len(data):
            unknown_control = {"offset": offset, "opcode": opcode}
            break
        raw = data[offset : offset + size]
        role = CONTROL_ROLES[opcode]
        token: dict[str, Any] = {
            "type": "control",
            "opcode": opcode,
            "role": role,
            "raw_hex": raw.hex(),
            "preservation": "preserve_exact_order_and_operand",
        }
        if opcode == "8080":
            token["type"] = "newline"
            token["token"] = "\\n"
        elif opcode == "8180":
            token["type"] = "block_boundary"
            token["token"] = "{BLOCK_8180}"
        elif opcode == "9380":
            reference_id = raw[2]
            token.update(
                {
                    "type": "placeholder",
                    "reference_id": reference_id,
                    "source_name": CHARACTER_NAMES.get(reference_id),
                    "token": f"{{CHAR:{reference_id:02d}}}",
                    "preservation": "preserve_character_identity",
                }
            )
        elif opcode in {"8580", "8680", "8a80"}:
            token["float32"] = struct.unpack_from("<f", raw, 2)[0]
        elif opcode == "9e80":
            group, sequence = struct.unpack_from("<HH", raw, 2)
            token["event_group"] = group
            token["event_sequence"] = sequence
        tokens.append(token)
        control_signature.append(raw.hex())
        offset += size

    trailing = data[offset:]
    signature_bytes = "|".join(control_signature).encode("ascii")
    return tokens, {
        "parser_complete": unknown_control is None and terminator_offset is not None,
        "unknown_control": unknown_control,
        "terminator_offset": terminator_offset,
        "trailing_raw_hex": trailing.hex(),
        "control_signature_sha256": hashlib.sha256(signature_bytes).hexdigest(),
        "control_sequence": control_signature,
    }


def line_metrics(tokens: list[dict[str, Any]]) -> dict[str, Any]:
    """Count visible cells between newline and candidate block boundaries.

    A glyph is one cell for this conservative preflight metric.  A character
    reference counts the source name's Unicode length, because it expands at
    runtime.  Pixel width remains the release gate.
    """

    segments = [0]
    boundary_types: list[str] = []
    for token in tokens:
        if token["type"] == "text":
            segments[-1] += len(token["glyph_codes"])
        elif token["type"] == "placeholder":
            segments[-1] += len(token.get("source_name") or "")
        elif token["type"] in {"newline", "block_boundary"}:
            boundary_types.append(token["type"])
            segments.append(0)
    return {
        "structural_segment_count": len(segments),
        "source_visible_glyph_counts": segments,
        "source_max_visible_glyphs": max(segments, default=0),
        "newline_count": boundary_types.count("newline"),
        "block_boundary_count": boundary_types.count("block_boundary"),
        "note": "Preflight only; final limit is rendered pixel width at the preserved scale.",
    }


def speaker_source(record: dict[str, Any]) -> str | None:
    speaker = record["speaker"]
    return speaker.get("japanese") or speaker.get("partial_japanese")


def occurrence_key(record: dict[str, Any]) -> str:
    return (
        f"HYOCC-A{int(record['archive_id']):04d}"
        f"-S{int(record['stream_id']):04d}"
        f"-M{int(record['message_id']):05d}"
        f"-X{record['exact_sha256']}"
    )


def patch_target(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "occurrence_key": occurrence_key(record),
        "archive_id": int(record["archive_id"]),
        "stream_id": int(record["stream_id"]),
        "message_id": int(record["message_id"]),
        "dialogue_id_snapshot": record["dialogue_id"],
        "file_sha256": record["file_sha256"],
        "font_fingerprint_sha256": record["font_fingerprint_sha256"],
        "exact_sha256": record["exact_sha256"],
        "text_offset_in_original": int(record["text_offset"]),
        "source_segment_bytes": int(record["segment_bytes"]),
        "event_group": record.get("event_group"),
        "event_sequence": record.get("event_sequence"),
        "location": record.get("location"),
    }


def problem_flags(record: dict[str, Any], metrics: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if record["body"]["decode_status"] != "complete":
        flags.append("unmapped_body_glyph")
    if record["speaker"]["decode_status"] not in {
        "complete",
        "speaker_not_encoded_in_message",
    }:
        flags.append("unmapped_speaker_glyph")
    controls = record["body"]["controls"]
    if "9080" in controls or "9180" in controls:
        flags.append("ruby_pair_requires_encoder_policy")
    if "9380" in controls:
        flags.append("dynamic_character_reference")
    if record["speaker"]["mode"] == "implicit_or_continuation":
        flags.append("implicit_continuation_speaker")
    if metrics["structural_segment_count"] > 10:
        flags.append("ui_like_many_segments")
    if metrics["source_max_visible_glyphs"] >= 28:
        flags.append("wide_scaled_or_layout_sensitive_segment")
    return flags


def build_template(
    representative: dict[str, Any],
    group: list[dict[str, Any]],
    lookup: MappingLookup,
) -> dict[str, Any]:
    tokens, parser = tokenise_body(representative, lookup)
    metrics = line_metrics(tokens)
    flags = problem_flags(representative, metrics)
    speaker = representative["speaker"]
    targets = sorted(
        (patch_target(row) for row in group),
        key=lambda row: (row["archive_id"], row["stream_id"], row["message_id"]),
    )
    semantic_hash_payload = json.dumps(
        {
            "speaker": speaker_source(representative),
            "body": representative.get("japanese"),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return {
        "template_key": f"HYT-X{representative['exact_sha256']}",
        "expected_original_exact_sha256": representative["exact_sha256"],
        "semantic_text_sha256": hashlib.sha256(semantic_hash_payload).hexdigest(),
        "source": {
            "speaker_japanese": speaker_source(representative),
            "body_japanese": representative.get("japanese"),
            "body_decode_status": representative["body"]["decode_status"],
            "dialogue_format": representative["dialogue_format"],
        },
        "translation": {
            "speaker_korean": "",
            "body_korean": "",
            "status": "pending",
            "translator_note": "",
        },
        "speaker_contract": {
            "mode": speaker["mode"],
            "reference_id": speaker.get("reference_id"),
            "raw_field_bytes_hex": speaker.get("raw_field_bytes_hex"),
            "decode_status": speaker["decode_status"],
            "unmapped_codes": speaker["unmapped_codes"],
            "policy": (
                "preserve reference identity; Korean display requires a Korean name table or deliberate literalisation"
                if speaker["mode"] == "character_reference"
                else "translate literal speaker glyph run"
                if speaker["mode"] == "literal_glyphs"
                else "do not synthesize a speaker field for this continuation"
            ),
        },
        "body_contract": {
            "tokens": tokens,
            "parser": parser,
            "layout_metrics": metrics,
            "unmapped_codes": representative["body"]["unmapped_codes"],
            "mapping_conflict_codes": representative["body"]["mapping_conflict_codes"],
        },
        "review_flags": flags,
        "source_resolution_blocked": any(flag.startswith("unmapped_") for flag in flags),
        "occurrence_count": len(targets),
        "logical_record_count": len({row["dialogue_id_snapshot"] for row in targets}),
        "patch_targets": targets,
    }


def visible_mapping_evidence(
    records: list[dict[str, Any]], lookup: MappingLookup
) -> dict[str, Any]:
    occurrence_counts: Counter[str] = Counter()
    raw_source_counts: Counter[str] = Counter()
    distinct_slots: set[tuple[int, int, int, str]] = set()
    for record in records:
        chunks = [bytes.fromhex(record["body_raw_bytes_hex"])]
        if record["speaker"]["mode"] == "literal_glyphs":
            chunks.append(bytes.fromhex(record["speaker"]["raw_glyph_bytes_hex"] or ""))
        for data in chunks:
            offset = 0
            while offset < len(data) and data[offset] != 0:
                code, size = glyph_code(data, offset)
                if code is not None:
                    mapping = lookup.lookup(record, code)
                    raw_source = mapping.get("unicode_source") if mapping else None
                    category = source_category(raw_source)
                    occurrence_counts[category] += 1
                    raw_source_counts[str(raw_source)] += 1
                    distinct_slots.add(
                        (
                            int(record["archive_id"]),
                            int(record["stream_id"]),
                            code,
                            category,
                        )
                    )
                    offset += size
                    continue
                if offset + 1 >= len(data):
                    break
                opcode = data[offset : offset + 2].hex()
                if opcode == "9180":
                    end = data.find(b"\x00", offset + 2)
                    if end < 0:
                        break
                    offset = end + 1
                    continue
                size = CONTROL_SIZES.get(opcode)
                if size is None:
                    break
                offset += size
    distinct_by_category = Counter(slot[3] for slot in distinct_slots)
    return {
        "visible_glyph_occurrences_by_mapping_category": dict(
            sorted(occurrence_counts.items())
        ),
        "distinct_context_slots_by_mapping_category": dict(
            sorted(distinct_by_category.items())
        ),
        "raw_unicode_source_counts": dict(sorted(raw_source_counts.items())),
        "warning": (
            "complete decode means every glyph has a label; it does not prove OCR labels are semantically correct. "
            "Human Japanese proofreading remains required before Korean translation."
        ),
    }


def unknown_code_inventory(
    templates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for template in templates:
        for field, codes in (
            ("body", template["body_contract"]["unmapped_codes"]),
            ("speaker", template["speaker_contract"]["unmapped_codes"]),
        ):
            for code in codes:
                key = (field, int(code))
                row = grouped.setdefault(
                    key,
                    {
                        "field": field,
                        "code": int(code),
                        "template_keys": [],
                        "occurrence_keys": [],
                        "examples": [],
                    },
                )
                row["template_keys"].append(template["template_key"])
                row["occurrence_keys"].extend(
                    target["occurrence_key"] for target in template["patch_targets"]
                )
                example = (
                    template["source"]["body_japanese"]
                    if field == "body"
                    else template["source"]["speaker_japanese"]
                )
                if example not in row["examples"]:
                    row["examples"].append(example)
    rows = []
    for row in grouped.values():
        row["template_count"] = len(set(row["template_keys"]))
        row["occurrence_count"] = len(set(row["occurrence_keys"]))
        row["template_keys"] = sorted(set(row["template_keys"]))
        row["occurrence_keys"] = sorted(set(row["occurrence_keys"]))
        row["examples"] = row["examples"][:5]
        rows.append(row)
    return sorted(rows, key=lambda row: (row["field"], row["code"]))


def control_statistics(
    records: list[dict[str, Any]], templates: list[dict[str, Any]]
) -> dict[str, Any]:
    invocation_occurrence: Counter[str] = Counter()
    records_with_occurrence: Counter[str] = Counter()
    for record in records:
        for opcode, count in record["body"]["controls"].items():
            invocation_occurrence[opcode] += int(count)
            records_with_occurrence[opcode] += 1
    invocation_templates: Counter[str] = Counter()
    records_with_templates: Counter[str] = Counter()
    for template in templates:
        controls = Counter(
            token["opcode"]
            for token in template["body_contract"]["tokens"]
            if token["type"] not in {"text", "terminator"}
        )
        for opcode, count in controls.items():
            invocation_templates[opcode] += count
            records_with_templates[opcode] += 1
    rows = []
    for opcode in sorted(set(invocation_occurrence) | set(invocation_templates)):
        rows.append(
            {
                "opcode": opcode,
                "role": CONTROL_ROLES.get(opcode, "unknown"),
                "occurrence_invocations": invocation_occurrence[opcode],
                "occurrence_records_containing": records_with_occurrence[opcode],
                "template_invocations": invocation_templates[opcode],
                "templates_containing": records_with_templates[opcode],
                "preservation": "preserve exact order and operands",
            }
        )
    return {"controls": rows}


def write_csv(templates: list[dict[str, Any]]) -> None:
    fields = [
        "template_key",
        "exact_sha256",
        "occurrence_count",
        "logical_record_count",
        "speaker_mode",
        "speaker_japanese",
        "body_japanese",
        "body_decode_status",
        "source_resolution_blocked",
        "review_flags",
        "segment_count",
        "max_source_visible_glyphs",
        "speaker_korean",
        "body_korean",
        "translation_status",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for template in templates:
            writer.writerow(
                {
                    "template_key": template["template_key"],
                    "exact_sha256": template["expected_original_exact_sha256"],
                    "occurrence_count": template["occurrence_count"],
                    "logical_record_count": template["logical_record_count"],
                    "speaker_mode": template["speaker_contract"]["mode"],
                    "speaker_japanese": template["source"]["speaker_japanese"],
                    "body_japanese": template["source"]["body_japanese"],
                    "body_decode_status": template["source"]["body_decode_status"],
                    "source_resolution_blocked": template["source_resolution_blocked"],
                    "review_flags": "|".join(template["review_flags"]),
                    "segment_count": template["body_contract"]["layout_metrics"][
                        "structural_segment_count"
                    ],
                    "max_source_visible_glyphs": template["body_contract"][
                        "layout_metrics"
                    ]["source_max_visible_glyphs"],
                    "speaker_korean": "",
                    "body_korean": "",
                    "translation_status": "pending",
                }
            )


def translation_text_weight(template: dict[str, Any]) -> int:
    """Approximate human translation work without counting control syntax."""

    speaker = template["source"]["speaker_japanese"] or ""
    body = template["source"]["body_japanese"] or ""
    visible_body = sum(
        template["body_contract"]["layout_metrics"]["source_visible_glyph_counts"]
    )
    # Speaker names also need a Korean form even for character references.
    return max(1, visible_body + len(speaker))


def write_translation_chunks(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedily balance 434 exact templates across three text-volume chunks."""

    source_order = sorted(
        templates,
        key=lambda template: (
            template["patch_targets"][0]["archive_id"],
            template["patch_targets"][0]["stream_id"],
            template["patch_targets"][0]["message_id"],
            template["expected_original_exact_sha256"],
        ),
    )
    global_index = {
        template["template_key"]: index
        for index, template in enumerate(source_order, start=1)
    }
    bins: list[dict[str, Any]] = [
        {"weight": 0, "templates": []} for _ in OUT_CHUNKS
    ]
    for template in sorted(
        templates,
        key=lambda row: (
            -translation_text_weight(row),
            global_index[row["template_key"]],
        ),
    ):
        destination = min(
            range(len(bins)),
            key=lambda index: (bins[index]["weight"], len(bins[index]["templates"]), index),
        )
        bins[destination]["templates"].append(template)
        bins[destination]["weight"] += translation_text_weight(template)

    summaries: list[dict[str, Any]] = []
    for chunk_number, (path, bucket) in enumerate(zip(OUT_CHUNKS, bins), start=1):
        bucket_templates = sorted(
            bucket["templates"], key=lambda row: global_index[row["template_key"]]
        )
        entries = []
        for template in bucket_templates:
            representative = template["patch_targets"][0]
            body = template["source"]["body_japanese"] or ""
            index = global_index[template["template_key"]]
            entries.append(
                {
                    "id": f"HYDA-{index:04d}",
                    "index": index,
                    "exact_sha256": template["expected_original_exact_sha256"],
                    "archive_id": representative["archive_id"],
                    "stream_id": representative["stream_id"],
                    "message_id": representative["message_id"],
                    "occurrences": [
                        {
                            "archive_id": target["archive_id"],
                            "stream_id": target["stream_id"],
                            "message_id": target["message_id"],
                        }
                        for target in template["patch_targets"]
                    ],
                    "speaker_japanese": template["source"]["speaker_japanese"],
                    "speaker_korean": "",
                    "japanese": body,
                    "korean": "",
                    "original_line_count": max(1, body.count("\n") + 1),
                }
            )
        document = {
            "schema_version": 1,
            "chunk_id": f"hyda_translation_{chunk_number:02d}",
            "balancing_basis": "source visible glyph count plus speaker length",
            "entry_count": len(entries),
            "text_weight": bucket["weight"],
            "entries": entries,
        }
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summaries.append(
            {
                "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                "entry_count": len(entries),
                "text_weight": bucket["weight"],
            }
        )
    if sum(row["entry_count"] for row in summaries) != 434:
        raise AssertionError("translation chunks do not cover all 434 templates")
    return summaries


def main() -> None:
    source_document = json.loads(SOURCE.read_text(encoding="utf-8"))
    mapping_document = json.loads(MAPPING.read_text(encoding="utf-8"))
    records = source_document["dialogues"]
    lookup = MappingLookup(mapping_document)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["exact_sha256"]].append(record)
    templates = [
        build_template(rows[0], rows, lookup)
        for _, rows in sorted(grouped.items())
    ]

    occurrence_keys = [
        target["occurrence_key"]
        for template in templates
        for target in template["patch_targets"]
    ]
    if len(occurrence_keys) != 653 or len(set(occurrence_keys)) != 653:
        raise AssertionError("expected 653 unique physical occurrence keys")
    if len(templates) != 434:
        raise AssertionError("expected 434 exact-byte translation templates")
    if not all(template["body_contract"]["parser"]["parser_complete"] for template in templates):
        raise AssertionError("body token parser did not consume every template")

    occurrence_multiplicity = Counter(
        template["occurrence_count"] for template in templates
    )
    logical_multiplicity = Counter(
        template["logical_record_count"] for template in templates
    )
    cross_bank_templates = [
        template
        for template in templates
        if len(
            {
                (row["archive_id"], row["stream_id"])
                for row in template["patch_targets"]
            }
        )
        > 1
    ]
    same_bank_repeats = [
        template
        for template in templates
        if any(
            count > 1
            for count in Counter(
                (row["archive_id"], row["stream_id"])
                for row in template["patch_targets"]
            ).values()
        )
    ]
    flags_occurrence = Counter()
    flags_template = Counter()
    for template in templates:
        for flag in template["review_flags"]:
            flags_template[flag] += 1
            flags_occurrence[flag] += template["occurrence_count"]

    segment_lengths = [
        length
        for template in templates
        for length in template["body_contract"]["layout_metrics"][
            "source_visible_glyph_counts"
        ]
    ]
    speaker_mode_occurrence = Counter(record["speaker"]["mode"] for record in records)
    speaker_mode_template = Counter(
        template["speaker_contract"]["mode"] for template in templates
    )
    body_status_occurrence = Counter(record["body"]["decode_status"] for record in records)
    body_status_template = Counter(
        template["source"]["body_decode_status"] for template in templates
    )
    speaker_status_occurrence = Counter(
        record["speaker"]["decode_status"] for record in records
    )
    speaker_status_template = Counter(
        template["speaker_contract"]["decode_status"] for template in templates
    )

    chunk_summaries = write_translation_chunks(templates)
    inventory = {
        "schema_version": 1,
        "status": "translation_inventory_only_no_iso_patch",
        "source": {
            "path": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "sha256": sha256_file(SOURCE),
            "mapping_path": str(MAPPING.relative_to(ROOT)).replace("\\", "/"),
            "mapping_sha256": sha256_file(MAPPING),
        },
        "counts": {
            "physical_occurrence_targets": len(records),
            "unique_occurrence_keys": len(set(occurrence_keys)),
            "logical_source_records": len({row["dialogue_id"] for row in records}),
            "exact_byte_translation_templates": len(templates),
            "unique_speaker_plus_body_semantics": len(
                {
                    (
                        template["source"]["speaker_japanese"],
                        template["source"]["body_japanese"],
                    )
                    for template in templates
                }
            ),
            "unique_body_semantics": len(
                {template["source"]["body_japanese"] for template in templates}
            ),
            "translation_reuse_savings_vs_occurrences": len(records) - len(templates),
        },
        "deduplication": {
            "occurrence_multiplicity_per_template": {
                str(key): value for key, value in sorted(occurrence_multiplicity.items())
            },
            "logical_record_multiplicity_per_template": {
                str(key): value for key, value in sorted(logical_multiplicity.items())
            },
            "cross_bank_reused_templates": len(cross_bank_templates),
            "cross_bank_reused_occurrences": sum(
                row["occurrence_count"] for row in cross_bank_templates
            ),
            "cross_bank_patterns": dict(
                sorted(
                    Counter(
                        ",".join(
                            map(
                                str,
                                sorted(
                                    {target["archive_id"] for target in row["patch_targets"]}
                                ),
                            )
                        )
                        for row in cross_bank_templates
                    ).items()
                )
            ),
            "same_bank_repeated_templates": len(same_bank_repeats),
            "warning": (
                "Translate a template once, but patch every listed occurrence. "
                "Never perform an unrestricted global byte-pattern replacement."
            ),
        },
        "decode_readiness": {
            "body_status_occurrences": dict(sorted(body_status_occurrence.items())),
            "body_status_templates": dict(sorted(body_status_template.items())),
            "speaker_status_occurrences": dict(sorted(speaker_status_occurrence.items())),
            "speaker_status_templates": dict(sorted(speaker_status_template.items())),
            "templates_blocked_by_unmapped_body_or_speaker": sum(
                row["source_resolution_blocked"] for row in templates
            ),
            "occurrences_blocked_by_unmapped_body_or_speaker": sum(
                row["occurrence_count"]
                for row in templates
                if row["source_resolution_blocked"]
            ),
            "body_mapping_conflict_occurrences": sum(
                bool(row["body"]["mapping_conflict_codes"]) for row in records
            ),
            "speaker_mapping_conflict_occurrences": sum(
                bool(row["speaker"]["mapping_conflict_codes"]) for row in records
            ),
        },
        "speaker": {
            "mode_occurrences": dict(sorted(speaker_mode_occurrence.items())),
            "mode_templates": dict(sorted(speaker_mode_template.items())),
            "character_reference_warning": (
                "Preserving 9380 keeps character identity but does not itself Koreanize the rendered name. "
                "Patch the central name resource or deliberately literalize each reference."
            ),
        },
        "review_flags": {
            "templates": dict(sorted(flags_template.items())),
            "occurrences": dict(sorted(flags_occurrence.items())),
        },
        "layout": {
            "structural_segments": len(segment_lengths),
            "source_visible_glyphs_min": min(segment_lengths),
            "source_visible_glyphs_median": statistics.median(segment_lengths),
            "source_visible_glyphs_p95": sorted(segment_lengths)[
                int(len(segment_lengths) * 0.95)
            ],
            "source_visible_glyphs_max": max(segment_lengths),
            "segments_over_20_glyphs": sum(value > 20 for value in segment_lengths),
            "rule": (
                "Retain every 8080 and 8180 boundary and all scale/layout operands. "
                "Use rendered pixel width as the release gate; character count is preflight only."
            ),
        },
        "mapping_evidence": visible_mapping_evidence(records, lookup),
        "control_statistics": control_statistics(records, templates),
        "stable_key_contract": {
            "translation_template_key": "HYT-X{full exact_sha256 of the original complete message segment}",
            "physical_occurrence_key": (
                "HYOCC-A{archive_id}-S{stream_id}-M{message_id}-X{full exact_sha256}"
            ),
            "required_source_assertions": [
                "archive_id and stream_id are in the 24-bank whitelist",
                "file_sha256 matches the unmodified decompressed source container",
                "message_id resolves through the original message table",
                "exact_sha256 matches before replacement",
                "font_fingerprint_sha256 matches the expected local font context",
            ],
            "offset_rule": (
                "text_offset_in_original is diagnostic only. Batch-rebuild a container from its original table; "
                "never locate later messages by stale offsets after an earlier replacement."
            ),
        },
        "translation_contract": [
            "Translate only text tokens and literal speaker text; never edit raw control operands in translator input.",
            "Keep 8080 newline and 8180 block/page-boundary tokens in the same order and count.",
            "Keep 9e80 event cue group/sequence bytes exact; they define fixed-scene ordering.",
            "Keep 9380 character-reference identity exact, or perform an explicit audited literalisation/name-table conversion.",
            "Treat each 9080/9180 ruby pair atomically; do not preserve only one half or translate its zero-terminated payload as normal text.",
            "Keep float/style/layout controls (8580/8680/8880/8a80/9480/9580 and other opaque controls) byte-exact.",
            "Keep the final 00 terminator and rebuild the message table and compressed member after variable-length encoding.",
            "Reject a build if any Korean character lacks a font mapping, any source hash differs, or any translated line exceeds its verified pixel-width budget.",
        ],
        "translation_chunks": chunk_summaries,
    }

    problems = {
        "schema_version": 1,
        "summary": {
            "blocked_templates": inventory["decode_readiness"][
                "templates_blocked_by_unmapped_body_or_speaker"
            ],
            "blocked_occurrences": inventory["decode_readiness"][
                "occurrences_blocked_by_unmapped_body_or_speaker"
            ],
            "review_flag_template_counts": dict(sorted(flags_template.items())),
        },
        "unknown_codes": unknown_code_inventory(templates),
        "templates_requiring_review": [
            template
            for template in templates
            if template["review_flags"]
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_INVENTORY.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OUT_TEMPLATES.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending_translation",
                "template_count": len(templates),
                "physical_occurrence_count": len(records),
                "templates": templates,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    OUT_PROBLEMS.write_text(
        json.dumps(problems, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(templates)
    print(json.dumps(inventory["counts"], ensure_ascii=False, indent=2))
    print(json.dumps(inventory["decode_readiness"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

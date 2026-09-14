from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_hyda_patch_manifest as manifest_builder  # noqa: E402
import verify_hyda_dialogue_iso as verify  # noqa: E402
from so3_repack import encode_glyph_code  # noqa: E402


class HydaIndependentVerifierTests(unittest.TestCase):
    def test_public_payload_has_exact_coverage_and_no_extracted_japanese(self) -> None:
        manifest_path = ROOT / "translations" / "hyda_patch_manifest.json"
        translation_path = ROOT / "translations" / "hyda_ko.json"
        manifest_raw = manifest_path.read_text(encoding="utf-8")
        translation_raw = translation_path.read_text(encoding="utf-8")
        for raw in (manifest_raw, translation_raw):
            self.assertIsNone(re.search(r'"(?:raw_bytes_hex|message_hex|japanese)"', raw))
            self.assertIsNone(re.search(r"[ぁ-ゖァ-ヺ一-龯]", raw))

        manifest = json.loads(manifest_raw)
        translations = json.loads(translation_raw)
        self.assertEqual(manifest["summary"]["event_bank_count"], 24)
        self.assertEqual(manifest["summary"]["occurrence_count"], 653)
        self.assertEqual(manifest["summary"]["unique_exact_segment_count"], 434)
        self.assertEqual(translations["translation_count"], 434)
        self.assertEqual(translations["physical_occurrence_count"], 653)

        catalogue, plan = verify.load_expectations(manifest_path, translation_path)
        self.assertEqual(len(catalogue), 653)
        self.assertEqual(len(plan), 653)
        self.assertEqual(len({text.exact_sha256 for text in plan.values()}), 434)
        self.assertEqual({key.archive_id for key in plan}, set(verify.BANK_STREAMS))

    def test_conservative_local_code_scan_overprotects_control_operands(self) -> None:
        base = 301
        raw = (
            encode_glyph_code(base)
            + bytes.fromhex("9e80")
            + encode_glyph_code(base + 2)
            + b"\0"
        )
        self.assertEqual(
            verify.conservative_local_codes(raw, base, 3),
            {base, base + 2},
        )

    def test_compact_manifest_omits_text_and_raw_game_bytes(self) -> None:
        raw = encode_glyph_code(272) + encode_glyph_code(50) + b"\0"
        row = {
            "archive_id": 1204,
            "stream_id": 5829,
            "message_id": 1,
            "exact_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes_hex": raw.hex(),
            "japanese": "「あ",
            "speaker": {"mode": "implicit_or_continuation"},
            "evidence": {"body_start_offset": 0},
            "file_sha256": "a" * 64,
            "font_fingerprint_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            source.write_text(json.dumps({"dialogues": [row]}), encoding="utf-8")
            compact = manifest_builder.build_manifest(
                source,
                expected_occurrences=1,
                expected_unique_segments=1,
                expected_banks=1,
            )
        self.assertEqual(compact["summary"]["occurrence_count"], 1)
        encoded = json.dumps(compact, ensure_ascii=False)
        self.assertNotIn("raw_bytes_hex", encoded)
        self.assertNotIn("japanese", encoded.lower())
        occurrence = compact["occurrences"][0]
        self.assertEqual(occurrence["source_line_count"], 1)
        self.assertEqual(occurrence["speaker"]["mode"], "implicit_or_continuation")

    def test_control_signature_ignores_only_replaced_visible_controls(self) -> None:
        raw = (
            bytes.fromhex("9e8001000200")
            + encode_glyph_code(400)
            + bytes.fromhex("938001")
            + bytes.fromhex("9080")
            + bytes.fromhex("91800100")
            + verify.NEWLINE
            + encode_glyph_code(401)
            + bytes.fromhex("9f80848000")
        )
        tokens, terminator = verify.tokenize(raw, 0)
        self.assertEqual(terminator, len(raw) - 1)
        signature = verify.preserved_control_signature(tokens)
        self.assertEqual(
            signature,
            ("9e8001000200", "8080", "9f80", "8480"),
        )

    def test_decode_visible_accepts_only_verified_local_glyphs_and_space(self) -> None:
        tokens = [
            verify.Token("glyph", encode_glyph_code(400), 400),
            verify.Token("glyph", encode_glyph_code(verify.SPACE_CODE), verify.SPACE_CODE),
            verify.Token("control", verify.NEWLINE),
            verify.Token("glyph", encode_glyph_code(401), 401),
            verify.Token("control", bytes.fromhex("9f80")),
        ]
        self.assertEqual(verify.decode_visible(tokens, {400: "한", 401: "글"}), "한 \n글")
        with self.assertRaisesRegex(AssertionError, "unverified patched glyph"):
            verify.decode_visible([verify.Token("glyph", encode_glyph_code(402), 402)], {})

    def test_patched_speaker_boundary_is_located_after_literal_name(self) -> None:
        prefix = bytes.fromhex("888006868000000000")
        speaker = encode_glyph_code(400) + encode_glyph_code(401)
        suffix = bytes.fromhex("898087808080")
        raw = prefix + speaker + suffix + encode_glyph_code(402) + b"\0"
        source = {
            "speaker": {"mode": "character_reference"},
            "evidence": {
                "speaker_field_start_offset": len(prefix),
                "body_start_offset": len(prefix) + 3 + len(suffix),
            },
        }
        body_start, field_start, field_end = verify.patched_body_start(raw, source)
        self.assertEqual((field_start, field_end), (len(prefix), len(prefix) + len(speaker)))
        self.assertEqual(body_start, len(prefix) + len(speaker) + len(suffix))
        self.assertEqual(verify.decode_speaker(raw, field_start, field_end, {400: "화", 401: "자"}), "화자")

    def test_iso_diff_scope_rejects_changes_outside_allowed_range(self) -> None:
        original = bytes(range(100))
        patched = bytearray(original)
        patched[25] ^= 0xFF
        with tempfile.TemporaryDirectory() as temporary:
            left = Path(temporary) / "left.bin"
            right = Path(temporary) / "right.bin"
            left.write_bytes(original)
            right.write_bytes(patched)
            self.assertEqual(
                verify.verify_diff_scope(left, right, [(20, 20)], total_size=100),
                1,
            )
            patched[75] ^= 0xFF
            right.write_bytes(patched)
            with self.assertRaisesRegex(AssertionError, "outside allowed archives"):
                verify.verify_diff_scope(left, right, [(20, 20)], total_size=100)


if __name__ == "__main__":
    unittest.main()

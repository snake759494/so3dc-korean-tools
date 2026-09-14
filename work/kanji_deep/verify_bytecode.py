#!/usr/bin/env python3
"""Verify the fully decoded local-code-base=1 mclib grammar against atlas bounds."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    data = args.input.read_bytes()
    table_start, text_start, width_start = struct.unpack_from("<3I", data, 0x10)
    glyph_count = struct.unpack_from("<I", data, 0x20)[0]
    local_code_base, record_count = struct.unpack_from("<2I", data, 0x38)
    if local_code_base != 1:
        raise ValueError("this verifier intentionally covers the confirmed local-code-base=1 grammar")
    text = data[text_start:width_start]
    table = [struct.unpack_from("<II", data, table_start + i * 8) for i in range(record_count)]
    offsets = sorted(set(offset for _message_id, offset in table) | {len(text)})
    end_for = dict(zip(offsets, offsets[1:]))
    result = {
        "records": record_count,
        "complete_known_grammar": 0,
        "terminated": 0,
        "glyph_operands": 0,
        "extended_glyph_operands": 0,
        "newline_controls": 0,
        "scale_controls": 0,
        "max_glyph_index": -1,
        "invalid_glyph_indices": 0,
        "unknown_controls": {},
    }
    for _message_id, begin in table:
        pos, end = begin, end_for.get(begin, len(text))
        complete = True
        while pos < end:
            value = text[pos]
            if value == 0:
                result["terminated"] += 1
                pos += 1
                break
            if value < 0x80:
                index = value - 1
                pos += 1
            elif pos + 1 < end and text[pos + 1] < 0x80:
                index = ((value & 0x7F) | (text[pos + 1] << 7)) - 1
                result["extended_glyph_operands"] += 1
                pos += 2
            elif text[pos : pos + 2] == b"\x80\x80":
                result["newline_controls"] += 1
                pos += 2
                continue
            elif text[pos : pos + 2] == b"\x8a\x80" and pos + 6 <= end:
                result["scale_controls"] += 1
                pos += 6
                continue
            else:
                key = text[pos : pos + 2].hex()
                unknown = result["unknown_controls"]
                assert isinstance(unknown, dict)
                unknown[key] = unknown.get(key, 0) + 1
                complete = False
                break
            result["glyph_operands"] += 1
            result["max_glyph_index"] = max(int(result["max_glyph_index"]), index)
            if not 0 <= index < glyph_count:
                result["invalid_glyph_indices"] += 1
        result["complete_known_grammar"] += int(complete)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

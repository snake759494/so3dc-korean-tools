#!/usr/bin/env python3
"""Decode selected SO3 mclib message bytecode and render it from embedded glyphs."""

from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path

from PIL import Image


def decode_glyph(raw: bytes, width: int, height: int) -> Image.Image:
    values: list[int] = []
    for value in raw:
        values.extend((value & 15, value >> 4))
    image = Image.new("L", (width, height))
    image.putdata([value * 17 for value in values[: width * height]])
    return image


def decode_tokens(encoded: bytes) -> tuple[list[int | None], list[str]]:
    """Decode glyph operands and the verified newline/scale controls.

    Character codes are unsigned LEB7: a one-byte value below 0x80, or
    ``(first & 0x7f) | (second << 7)`` when the first byte has bit 7 set and
    the second is below 0x80.  High/high pairs enter the control namespace;
    80 80 is newline and 8a 80 + float32 is scale.
    Returned integers are character codes; the header's local-code base maps
    them to either the shared global font or this member's local atlas.
    """
    tokens: list[int | None] = []
    controls: list[str] = []
    pos = 0
    while pos < len(encoded):
        value = encoded[pos]
        if value == 0:
            break
        if value < 0x80:
            tokens.append(value)
            pos += 1
            continue
        if pos + 1 < len(encoded) and encoded[pos + 1] < 0x80:
            following = encoded[pos + 1]
            code = (value & 0x7F) | (following << 7)
            if code:
                tokens.append(code)
                pos += 2
                continue
        if encoded[pos : pos + 2] == b"\x80\x80":
            tokens.append(None)
            pos += 2
            continue
        if encoded[pos : pos + 2] == b"\x8a\x80" and pos + 6 <= len(encoded):
            scale = struct.unpack_from("<f", encoded, pos + 2)[0]
            controls.append(f"offset=0x{pos:X}: scale={scale:g}")
            pos += 6
            continue
        # Its parameter length depends on the opcode.  Stop here so parameter
        # bytes are never misreported as glyph operands.
        controls.append(
            f"offset=0x{pos:X}: undecoded control tail={encoded[pos:].hex()}"
        )
        break
    return tokens, controls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("message_ids", nargs="+", type=lambda x: int(x, 0))
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--global-mclib", type=Path)
    args = ap.parse_args()

    data = args.input.read_bytes()
    table_start, table_end, aux_start, bitmap_start = struct.unpack_from("<4I", data, 0x10)
    glyph_count = struct.unpack_from("<I", data, 0x20)[0]
    glyph_w, glyph_h, glyph_stride = struct.unpack_from("<3I", data, 0x2C)
    local_code_base = struct.unpack_from("<I", data, 0x38)[0]
    mapping_count = struct.unpack_from("<I", data, 0x3C)[0]
    glyph_bytes = glyph_stride * glyph_h // 2
    table = [struct.unpack_from("<II", data, table_start + i * 8) for i in range(mapping_count)]
    text = data[table_end:aux_start]
    widths = data[aux_start : aux_start + glyph_count]

    global_data = args.global_mclib.read_bytes() if args.global_mclib else b""
    if global_data:
        global_aux, global_bitmap = struct.unpack_from("<2I", global_data, 0x18)
        global_count = struct.unpack_from("<I", global_data, 0x20)[0]
        global_w, global_h, global_stride = struct.unpack_from("<3I", global_data, 0x2C)
        global_widths = global_data[global_aux : global_aux + global_count]
        global_glyph_bytes = global_stride * global_h // 2
        if (global_w, global_h) != (glyph_w, glyph_h):
            raise ValueError("global/local mclib cell geometries differ")
    else:
        global_bitmap = global_glyph_bytes = 0
        global_widths = b""
    by_id = {message_id: offset for message_id, offset in table}
    sorted_offsets = sorted(set(offset for _message_id, offset in table) | {len(text)})
    end_for = {begin: end for begin, end in zip(sorted_offsets, sorted_offsets[1:])}

    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for message_id in args.message_ids:
        begin = by_id[message_id]
        encoded = text[begin : end_for.get(begin, len(text))]
        tokens, controls = decode_tokens(encoded)
        lines: list[list[tuple[str, int]]] = [[]]
        for token in tokens:
            if token is None:
                lines.append([])
            else:
                if token >= local_code_base:
                    index = token - local_code_base
                    if not 0 <= index < glyph_count:
                        raise ValueError(f"local code {token} -> out-of-range index {index}")
                    lines[-1].append(("local", index))
                else:
                    if not global_data:
                        raise ValueError(
                            f"code {token} requires --global-mclib (local base={local_code_base})"
                        )
                    index = token - 1
                    if not 0 <= index < len(global_widths):
                        raise ValueError(f"global code {token} -> out-of-range index {index}")
                    lines[-1].append(("global", index))

        def advance_of(ref: tuple[str, int]) -> int:
            source, index = ref
            return max(1, (widths if source == "local" else global_widths)[index])

        line_widths = [sum(advance_of(ref) for ref in line) for line in lines]
        image = Image.new("L", (max(1, *line_widths), max(1, len(lines) * glyph_h)), 0)
        for line_number, line in enumerate(lines):
            x = 0
            for source, index in line:
                advance = advance_of((source, index))
                if source == "local":
                    offset = bitmap_start + index * glyph_bytes
                    glyph = decode_glyph(data[offset : offset + glyph_bytes], glyph_w, glyph_h)
                else:
                    offset = int(global_bitmap) + index * int(global_glyph_bytes)
                    glyph = decode_glyph(
                        global_data[offset : offset + int(global_glyph_bytes)], glyph_w, glyph_h
                    )
                image.paste(glyph, (x, line_number * glyph_h), glyph)
                x += advance
        image = image.resize((image.width * args.scale, image.height * args.scale), Image.Resampling.NEAREST)
        filename = f"message_{message_id:06d}.png"
        image.save(args.output / filename)
        manifest.append(
            {
                "message_id": message_id,
                "text_offset": begin,
                "encoded_hex": encoded.hex(),
                "character_codes": " ".join("NL" if token is None else str(token) for token in tokens),
                "controls": "; ".join(controls),
                "output": filename,
            }
        )
    with (args.output / "messages.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Independent structural and visual verification of SO3 so3mclib files."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw


@dataclass
class Header:
    version: str
    table_start: int
    table_end: int
    width_start: int
    bitmap_start: int
    glyph_count: int
    cache_width: int
    cache_height: int
    glyph_width: int
    glyph_height: int
    glyph_stride: int
    flags: int
    mapping_count: int
    file_size: int


def parse_header(data: bytes) -> Header:
    if not data.startswith(b"so3mclib "):
        raise ValueError("not an so3mclib file")
    values = struct.unpack_from("<13I", data, 0x10)
    return Header(data[:16].split(b"\0", 1)[0].decode("ascii"), *values)


def decode_bitmap(raw: bytes, width: int, height: int, low_nibble_first: bool = True) -> Image.Image:
    pixels: list[int] = []
    for byte in raw:
        lo, hi = byte & 0x0F, byte >> 4
        pixels.extend((lo, hi) if low_nibble_first else (hi, lo))
    image = Image.new("L", (width, height), 0)
    image.putdata([v * 17 for v in pixels[: width * height]])
    return image


def glyph_image(data: bytes, header: Header, index: int) -> Image.Image:
    glyph_bytes = header.glyph_stride * header.glyph_height // 2
    begin = header.bitmap_start + index * glyph_bytes
    return decode_bitmap(data[begin : begin + glyph_bytes], header.glyph_width, header.glyph_height)


def message_segments(data: bytes, header: Header) -> dict[int, bytes]:
    if not header.mapping_count:
        return {}
    table = [struct.unpack_from("<II", data, header.table_start + i * 8) for i in range(header.mapping_count)]
    offsets = sorted(offset for _, offset in table)
    if len(set(offsets)) != len(offsets):
        raise ValueError("message offsets are not unique")
    text = data[header.table_end : header.width_start]
    ends = {offset: offsets[i + 1] if i + 1 < len(offsets) else len(text) for i, offset in enumerate(offsets)}
    result = {message_id: text[offset : ends[offset]] for message_id, offset in table}
    if any(not value or value[-1] != 0 for value in result.values()):
        raise ValueError("a sorted-offset message interval does not end in 00")
    return result


def decode_events(encoded: bytes, glyph_count: int) -> list[dict[str, object]]:
    """Decode glyph operands and the two controls observed in this sample.

    Glyph codes are one- or two-byte unsigned LEB128 values, stored one-based.
    Because this library has only 624 glyphs, ordinary glyph codes need at most
    two bytes.  80 80 is a newline.  8A 80 + float32 is a scale command.
    """
    events: list[dict[str, object]] = []
    pos = 0
    while pos < len(encoded):
        first = encoded[pos]
        if first == 0:
            events.append({"kind": "end", "offset": pos})
            if any(encoded[pos + 1 :]):
                raise ValueError(f"nonzero bytes after terminator at {pos:#x}")
            break
        if first < 0x80:
            index = first - 1
            events.append({"kind": "glyph", "offset": pos, "bytes": f"{first:02x}", "index": index})
            pos += 1
            continue
        if pos + 1 >= len(encoded):
            raise ValueError("truncated high-bit byte")
        second = encoded[pos + 1]
        if first == 0x80 and second == 0x80:
            events.append({"kind": "newline", "offset": pos, "bytes": "8080"})
            pos += 2
            continue
        if first == 0x8A and second == 0x80:
            if pos + 6 > len(encoded):
                raise ValueError("truncated scale control")
            value = struct.unpack_from("<f", encoded, pos + 2)[0]
            events.append({"kind": "scale", "offset": pos, "bytes": encoded[pos : pos + 6].hex(), "value": value})
            pos += 6
            continue
        if second < 0x80:
            code = (first & 0x7F) | (second << 7)
            index = code - 1
            if not 0 <= index < glyph_count:
                raise ValueError(f"glyph index {index} is out of range")
            events.append({"kind": "glyph", "offset": pos, "bytes": encoded[pos : pos + 2].hex(), "index": index})
            pos += 2
            continue
        raise ValueError(f"unknown high/high control {first:02x}{second:02x} at {pos:#x}")
    return events


def render_events(data: bytes, header: Header, widths: bytes, events: list[dict[str, object]]) -> Image.Image:
    lines: list[list[tuple[int, float]]] = [[]]
    scale = 1.0
    for event in events:
        if event["kind"] == "glyph":
            lines[-1].append((int(event["index"]), scale))
        elif event["kind"] == "newline":
            lines.append([])
        elif event["kind"] == "scale":
            scale = float(event["value"])
    line_height = header.glyph_height + 4
    line_widths = [sum(max(1, round(widths[index] * scale)) for index, scale in line) for line in lines]
    canvas = Image.new("L", (max(1, max(line_widths, default=1)), max(1, len(lines) * line_height - 4)), 0)
    for line_no, line in enumerate(lines):
        x = 0
        baseline_bottom = line_no * line_height + header.glyph_height
        for index, scale in line:
            glyph = glyph_image(data, header, index)
            scaled_w = max(1, round(header.glyph_width * scale))
            scaled_h = max(1, round(header.glyph_height * scale))
            if (scaled_w, scaled_h) != glyph.size:
                glyph = glyph.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
            canvas.paste(255, (x, baseline_bottom - scaled_h), glyph)
            x += max(1, round(widths[index] * scale))
    return canvas


def nibble_comparison(data: bytes, header: Header, output: Path) -> None:
    count, columns = min(64, header.glyph_count), 16
    rows = math.ceil(count / columns)
    cell_w, cell_h = header.glyph_width, header.glyph_height
    sheet = Image.new("L", (columns * cell_w, rows * cell_h * 2 + 24), 0)
    glyph_bytes = header.glyph_stride * header.glyph_height // 2
    for index in range(count):
        begin = header.bitmap_start + index * glyph_bytes
        raw = data[begin : begin + glyph_bytes]
        x, y = (index % columns) * cell_w, (index // columns) * cell_h
        sheet.paste(decode_bitmap(raw, cell_w, cell_h, True), (x, y))
        sheet.paste(decode_bitmap(raw, cell_w, cell_h, False), (x, rows * cell_h + 24 + y))
    draw = ImageDraw.Draw(sheet)
    draw.text((2, rows * cell_h + 5), "top: low nibble first   bottom: high nibble first", fill=255)
    sheet.resize((sheet.width * 2, sheet.height * 2), Image.Resampling.NEAREST).save(output)


def validate_file(path: Path) -> tuple[bytes, Header, dict[str, object]]:
    data = path.read_bytes()
    h = parse_header(data)
    glyph_bytes = h.glyph_stride * h.glyph_height // 2
    bitmap_end = h.bitmap_start + h.glyph_count * glyph_bytes
    widths = data[h.width_start : h.width_start + h.glyph_count]
    width_padding = data[h.width_start + h.glyph_count : h.bitmap_start]
    table_payload_end = h.table_start + h.mapping_count * 8 if h.mapping_count else 0
    checks = {
        "path": str(path.resolve()),
        "header": asdict(h),
        "actual_file_size": len(data),
        "glyph_bytes": glyph_bytes,
        "bitmap_end": bitmap_end,
        "file_size_field_matches": h.file_size == len(data),
        "bitmap_ends_at_file_size": bitmap_end == len(data),
        "width_count": len(widths),
        "width_min": min(widths),
        "width_max": max(widths),
        "width_padding_bytes": len(width_padding),
        "width_padding_all_zero": not any(width_padding),
        "table_payload_end": table_payload_end,
        "table_padding_bytes": h.table_end - table_payload_end if h.mapping_count else 0,
        "table_padding_all_zero": not any(data[table_payload_end : h.table_end]) if h.mapping_count else True,
    }
    return data, h, checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mclib_172", type=Path)
    parser.add_argument("mclib_175", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    validations = []
    for path in (args.mclib_172, args.mclib_175):
        data, header, checks = validate_file(path)
        validations.append(checks)
        nibble_comparison(data, header, args.output / f"{header.version.replace(' ', '_')}_nibbles.png")

    data, header, _ = validate_file(args.mclib_175)
    widths = data[header.width_start : header.width_start + header.glyph_count]
    messages = message_segments(data, header)
    manifest = []
    all_control_counts = {"newline": 0, "scale": 0}
    all_extended, all_direct = 0, 0
    for message_id, encoded in messages.items():
        events = decode_events(encoded, header.glyph_count)
        all_control_counts["newline"] += sum(e["kind"] == "newline" for e in events)
        all_control_counts["scale"] += sum(e["kind"] == "scale" for e in events)
        all_direct += sum(e["kind"] == "glyph" and len(str(e["bytes"])) == 2 for e in events)
        all_extended += sum(e["kind"] == "glyph" and len(str(e["bytes"])) == 4 for e in events)

    selected = [10, 20, 151, 633, 20002]
    for message_id in selected:
        encoded = messages[message_id]
        events = decode_events(encoded, header.glyph_count)
        image = render_events(data, header, widths, events)
        filename = f"message_{message_id:05d}.png"
        image.resize((image.width * 3, image.height * 3), Image.Resampling.NEAREST).save(args.output / filename)
        glyphs = [e for e in events if e["kind"] == "glyph"]
        controls = [e for e in events if e["kind"] in ("newline", "scale")]
        manifest.append({
            "message_id": message_id,
            "encoded_hex": encoded.hex(),
            "glyph_indices_hex": " ".join(f"{int(e['index']):03X}" for e in glyphs),
            "glyph_byte_forms": " ".join(str(e["bytes"]) for e in glyphs),
            "controls": json.dumps(controls, ensure_ascii=False),
            "output": filename,
        })

    with (args.output / "message_manifest.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)

    summary = {
        "files": validations,
        "message_table": {
            "entries": len(messages),
            "all_entries_decode_without_unknown_controls": True,
            "direct_glyph_operands": all_direct,
            "extended_glyph_operands": all_extended,
            "control_counts": all_control_counts,
        },
    }
    (args.output / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

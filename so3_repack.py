#!/usr/bin/env python3
"""Minimal, bounds-checked SO3 mclib/SLZ/ISO repacker.

The tool deliberately does not relocate archive entries.  It rebuilds one
so3mclib member, recompresses it as SLZ mode 2, and accepts the result only if
it fits in the member's existing allocation.  The original ISO is never
opened for writing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SECTOR = 0x800
INDEX_OFFSET = 0x200000
ENTRY_COUNT = 0x1800
SEED = 0x13578642
SIGNATURE = 0x27D51556


def align(value: int, boundary: int = 0x80) -> int:
    return (value + boundary - 1) & -boundary


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def p32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def decode_index(encoded: bytes) -> list[int]:
    if len(encoded) != ENTRY_COUNT * 3 * 4:
        raise ValueError("short SO3 index")
    table = list(struct.unpack(f"<{ENTRY_COUNT * 3}I", encoded))
    if table[0] != SIGNATURE:
        raise ValueError(f"SO3 index signature missing: 0x{table[0]:08X}")
    key = SEED
    mask = 0xFFFFFFFF
    for i in range(ENTRY_COUNT):
        table[i] ^= key
        key = (key ^ ((key << 1) & mask)) & mask
        table[ENTRY_COUNT + i] ^= key
        key = (key ^ ((~SEED) & mask)) & mask
        table[ENTRY_COUNT * 2 + i] ^= key
        key = (key ^ ((key << 2) & mask) ^ SEED) & mask
    table[0] = INDEX_OFFSET // SECTOR
    return table


def read_index(iso: Path) -> list[int]:
    with iso.open("rb") as f:
        f.seek(INDEX_OFFSET)
        return decode_index(f.read(ENTRY_COUNT * 3 * 4))


def decompress_slz_payload(payload: bytes, mode: int, output_size: int) -> bytes:
    if mode == 0:
        if len(payload) < output_size:
            raise ValueError("short mode-0 SLZ payload")
        return payload[:output_size]
    if mode not in (1, 2, 3):
        raise ValueError(f"unsupported SLZ mode {mode}")
    src = 0
    out = bytearray()
    flags = 0
    while len(out) < output_size:
        flags >>= 1
        if flags <= 0xFFFF:
            if src >= len(payload):
                raise ValueError("SLZ flags truncated")
            flags = 0x00FF0000 | payload[src]
            src += 1
            if mode == 3:
                if src >= len(payload):
                    raise ValueError("SLZ16 flags truncated")
                flags |= 0xFF000000 | (payload[src] << 8)
                src += 1
        if flags & 1:
            unit = 2 if mode == 3 else 1
            if src + unit > len(payload):
                raise ValueError("SLZ literal truncated")
            out.extend(payload[src : src + unit])
            src += unit
        else:
            if src + 2 > len(payload):
                raise ValueError("SLZ match truncated")
            pos, count = payload[src], payload[src + 1]
            src += 2
            if mode == 2 and count >= 0xF0:
                if count > 0xF0:
                    count = (count & 0x0F) + 3
                else:
                    count = pos + 0x13
                    if src >= len(payload):
                        raise ValueError("SLZ RLE truncated")
                    pos = payload[src]
                    src += 1
                out.extend(bytes((pos,)) * min(count, output_size - len(out)))
            else:
                pos |= (count & 0x0F) << 8
                count = (count >> 4) + 3
                if mode == 3:
                    pos <<= 1
                    count = (count - 1) << 1
                if pos == 0 or pos > len(out):
                    raise ValueError(f"invalid SLZ distance {pos} at output {len(out)}")
                for _ in range(min(count, output_size - len(out))):
                    out.append(out[-pos])
    return bytes(out)


def read_slz_member(iso: Path, offset: int) -> tuple[bytes, dict[str, int]]:
    with iso.open("rb") as f:
        f.seek(offset)
        header = f.read(16)
        if len(header) != 16 or header[:3] != b"SLZ":
            raise ValueError(f"SLZ header missing at ISO offset 0x{offset:X}")
        mode, compressed, unpacked, next_rel = header[3], u32(header, 4), u32(header, 8), u32(header, 12)
        payload = f.read(compressed)
    decoded = decompress_slz_payload(payload, mode, unpacked)
    return decoded, {
        "mode": mode,
        "compressed": compressed,
        "unpacked": unpacked,
        "next_rel": next_rel,
    }


def _longest_match(data: bytes, pos: int, chains: dict[bytes, list[int]], max_len: int) -> tuple[int, int]:
    if pos + 3 > len(data):
        return 0, 0
    key = data[pos : pos + 3]
    candidates = chains.get(key, ())
    best_distance = best_len = 0
    # Recent candidates normally compress bitmaps best.  The cap keeps the
    # encoder deterministic and fast even for large zero-heavy mclibs.
    for previous in reversed(candidates[-4096:]):
        distance = pos - previous
        if distance > 0xFFF:
            break
        length = 3
        limit = min(max_len, len(data) - pos)
        while length < limit and data[previous + length] == data[pos + length]:
            length += 1
        if length > best_len:
            best_distance, best_len = distance, length
            if length == limit:
                break
    return best_distance, best_len


def compress_slz_mode2(data: bytes) -> bytes:
    """Greedy tri-Ace SLZ mode-2 encoder (LZSS + the mode-2 RLE forms)."""
    out = bytearray()
    chains: dict[bytes, list[int]] = {}
    pos = 0
    while pos < len(data):
        flag_offset = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if pos >= len(data):
                break
            run = 1
            max_run = min(274, len(data) - pos)
            while run < max_run and data[pos + run] == data[pos]:
                run += 1
            distance, match_len = _longest_match(data, pos, chains, 17)

            if run >= 4 and run >= match_len:
                if run >= 19:
                    use = run
                    out.extend((use - 0x13, 0xF0, data[pos]))
                else:
                    use = min(run, 18)
                    out.extend((data[pos], 0xF0 | (use - 3)))
            elif match_len >= 3:
                use = match_len
                out.extend((distance & 0xFF, ((use - 3) << 4) | (distance >> 8)))
            else:
                use = 1
                flags |= 1 << bit
                out.append(data[pos])

            end = pos + use
            for p in range(pos, end):
                if p + 3 <= len(data):
                    key = data[p : p + 3]
                    chain = chains.setdefault(key, [])
                    chain.append(p)
                    # Discard positions outside the 12-bit window in batches.
                    if len(chain) > 256 and p - chain[128] > 0xFFF:
                        del chain[:128]
            pos = end
        out[flag_offset] = flags
    return bytes(out)


def encode_glyph_code(code: int) -> bytes:
    if not 1 <= code < 0x4000:
        raise ValueError(f"glyph code outside tested two-byte range: {code}")
    if code < 0x80:
        return bytes((code,))
    return bytes(((code & 0x7F) | 0x80, code >> 7))


@dataclass
class Mclib:
    original: bytes
    table_start: int
    text_start: int
    width_start: int
    bitmap_start: int
    glyph_count: int
    glyph_width: int
    glyph_height: int
    glyph_stride: int
    local_base: int
    mapping_count: int
    rows: list[tuple[int, int]]
    segments: dict[int, bytes]
    widths: bytes
    bitmaps: bytes

    @classmethod
    def parse(cls, data: bytes) -> "Mclib":
        if len(data) < 0x80 or not data.startswith(b"so3mclib "):
            raise ValueError("not an so3mclib")
        words = struct.unpack_from("<13I", data, 0x10)
        table_start, text_start, width_start, bitmap_start = words[:4]
        glyph_count = words[4]
        glyph_width, glyph_height, glyph_stride = words[7:10]
        local_base, mapping_count, file_size = words[10:13]
        if file_size != len(data):
            raise ValueError(f"mclib file_size mismatch: {file_size} != {len(data)}")
        if not (0x80 <= table_start <= text_start <= len(data)):
            raise ValueError("invalid mclib section offsets")
        rows = [struct.unpack_from("<II", data, table_start + i * 8) for i in range(mapping_count)]
        offsets = sorted(offset for _, offset in rows)
        if len(offsets) != len(set(offsets)):
            raise ValueError("duplicate message offsets are not supported")
        text_end = width_start if glyph_count else len(data)
        segments: dict[int, bytes] = {}
        for i, offset in enumerate(offsets):
            end = offsets[i + 1] if i + 1 < len(offsets) else text_end - text_start
            if not 0 <= offset < end <= text_end - text_start:
                raise ValueError("invalid message segment boundary")
            segment = data[text_start + offset : text_start + end]
            if not segment.endswith(b"\0"):
                raise ValueError("message segment lacks NUL terminator/padding")
            segments[offset] = segment
        glyph_bytes = (glyph_stride * glyph_height + 1) // 2
        bitmap_end = bitmap_start + glyph_count * glyph_bytes
        if glyph_count:
            if bitmap_end > len(data) or any(data[bitmap_end:]):
                raise ValueError("invalid glyph bitmap extent/padding")
            widths = data[width_start : width_start + glyph_count]
            bitmaps = data[bitmap_start:bitmap_end]
        else:
            widths = bitmaps = b""
        return cls(data, table_start, text_start, width_start, bitmap_start, glyph_count,
                   glyph_width, glyph_height, glyph_stride, local_base, mapping_count,
                   rows, segments, widths, bitmaps)

    def replace_message_and_append_glyphs(
        self, message_id: int, glyphs: list[tuple[int, bytes]],
        layout: list[int | None] | None = None, space_code: int = 5,
    ) -> tuple[bytes, dict[str, object]]:
        matching = [(i, off) for i, (mid, off) in enumerate(self.rows) if mid == message_id]
        if len(matching) != 1:
            raise ValueError(f"message id {message_id} occurs {len(matching)} times; expected exactly one")
        row_index, target_offset = matching[0]
        glyph_bytes = (self.glyph_stride * self.glyph_height + 1) // 2
        for width, bitmap in glyphs:
            if not 1 <= width <= self.glyph_width:
                raise ValueError(f"invalid glyph advance {width}")
            if len(bitmap) != glyph_bytes:
                raise ValueError(f"glyph bitmap is {len(bitmap)} bytes, expected {glyph_bytes}")

        codes = [self.local_base + self.glyph_count + i for i in range(len(glyphs))]
        message_codes = codes if layout is None else [
            space_code if item is None else codes[item] for item in layout
        ]
        replacement = b"".join(encode_glyph_code(code) for code in message_codes) + b"\0"
        ordered_offsets = sorted(self.segments)
        new_text = bytearray()
        offset_remap: dict[int, int] = {}
        for old_offset in ordered_offsets:
            offset_remap[old_offset] = len(new_text)
            if old_offset == target_offset:
                new_text.extend(replacement)
            else:
                new_text.extend(self.segments[old_offset])

        new_table_start = 0x80
        new_text_start = align(new_table_start + self.mapping_count * 8)
        new_width_start = align(new_text_start + len(new_text))
        new_glyph_count = self.glyph_count + len(glyphs)
        new_bitmap_start = align(new_width_start + new_glyph_count)
        new_bitmap_end = new_bitmap_start + new_glyph_count * glyph_bytes
        new_file_size = align(new_bitmap_end)
        rebuilt = bytearray(new_file_size)
        rebuilt[:0x80] = self.original[:0x80]
        p32(rebuilt, 0x10, new_table_start)
        p32(rebuilt, 0x14, new_text_start)
        p32(rebuilt, 0x18, new_width_start)
        p32(rebuilt, 0x1C, new_bitmap_start)
        p32(rebuilt, 0x20, new_glyph_count)
        p32(rebuilt, 0x40, new_file_size)
        for i, (mid, old_offset) in enumerate(self.rows):
            struct.pack_into("<II", rebuilt, new_table_start + i * 8, mid, offset_remap[old_offset])
        rebuilt[new_text_start : new_text_start + len(new_text)] = new_text
        rebuilt[new_width_start : new_width_start + self.glyph_count] = self.widths
        rebuilt[new_width_start + self.glyph_count : new_width_start + new_glyph_count] = bytes(
            width for width, _ in glyphs
        )
        rebuilt[new_bitmap_start : new_bitmap_start + len(self.bitmaps)] = self.bitmaps
        cursor = new_bitmap_start + len(self.bitmaps)
        for _, bitmap in glyphs:
            rebuilt[cursor : cursor + glyph_bytes] = bitmap
            cursor += glyph_bytes

        # Reparse before returning: this catches all section and table errors.
        check = Mclib.parse(bytes(rebuilt))
        if check.rows[row_index] != (message_id, offset_remap[target_offset]):
            raise AssertionError("rebuilt target mapping mismatch")
        return bytes(rebuilt), {
            "strategy": "append_local_glyphs",
            "message_id": message_id,
            "old_message_hex": self.segments[target_offset].hex(),
            "new_message_hex": replacement.hex(),
            "new_codes": codes,
            "new_message_codes": message_codes,
            "old_glyph_count": self.glyph_count,
            "new_glyph_count": new_glyph_count,
            "old_mclib_size": len(self.original),
            "new_mclib_size": len(rebuilt),
        }

    def replace_message_reusing_glyphs(
        self, message_id: int, glyphs: list[tuple[int, bytes]],
        layout: list[int | None] | None = None, space_code: int = 5,
        reuse_codes: list[int] | None = None,
    ) -> tuple[bytes, dict[str, object]]:
        """Size-neutral fallback: replace the last local slots and one segment.

        This can affect other messages which reference those slots, so callers
        should prefer append mode.  It is useful for the first boot proof when
        a tightly chained stream has no room to grow.
        """
        if len(glyphs) > self.glyph_count:
            raise ValueError("not enough existing local glyph slots to reuse")
        matching = [(i, off) for i, (mid, off) in enumerate(self.rows) if mid == message_id]
        if len(matching) != 1:
            raise ValueError(f"message id {message_id} occurs {len(matching)} times; expected exactly one")
        _, target_offset = matching[0]
        target_segment = self.segments[target_offset]
        if reuse_codes is None:
            first_index = self.glyph_count - len(glyphs)
            codes = [self.local_base + first_index + i for i in range(len(glyphs))]
        else:
            if len(reuse_codes) != len(glyphs) or len(set(reuse_codes)) != len(reuse_codes):
                raise ValueError("reuse code list must have one unique code per new glyph")
            codes = reuse_codes
            for code in codes:
                if not self.local_base <= code < self.local_base + self.glyph_count:
                    raise ValueError(f"reuse code {code} is outside this local atlas")
        indices = [code - self.local_base for code in codes]
        message_codes = codes if layout is None else [
            space_code if item is None else codes[item] for item in layout
        ]
        replacement = b"".join(encode_glyph_code(code) for code in message_codes) + b"\0"
        if len(replacement) > len(target_segment):
            raise ValueError(
                f"replacement bytecode ({len(replacement)}) does not fit target segment ({len(target_segment)})"
            )
        rebuilt = bytearray(self.original)
        segment_start = self.text_start + target_offset
        rebuilt[segment_start : segment_start + len(target_segment)] = replacement + b"\0" * (
            len(target_segment) - len(replacement)
        )
        glyph_bytes = (self.glyph_stride * self.glyph_height + 1) // 2
        for index, (width, bitmap) in zip(indices, glyphs):
            rebuilt[self.width_start + index] = width
            start = self.bitmap_start + index * glyph_bytes
            rebuilt[start : start + glyph_bytes] = bitmap
        check = Mclib.parse(bytes(rebuilt))
        if len(check.original) != len(self.original):
            raise AssertionError("size-neutral rebuild changed size")
        return bytes(rebuilt), {
            "strategy": (
                "reuse_selected_local_glyphs" if reuse_codes is not None
                else "reuse_last_local_glyphs"
            ),
            "message_id": message_id,
            "old_message_hex": target_segment.hex(),
            "new_message_hex": replacement.hex(),
            "new_codes": codes,
            "new_message_codes": message_codes,
            "reused_local_indices": indices,
            "old_glyph_count": self.glyph_count,
            "new_glyph_count": self.glyph_count,
            "old_mclib_size": len(self.original),
            "new_mclib_size": len(rebuilt),
        }


def render_glyphs(
    text: str,
    font_path: Path,
    cell_width: int,
    cell_height: int,
    stride: int,
    gray_levels: int = 16,
) -> list[tuple[int, bytes]]:
    if not text or "\0" in text or "\n" in text:
        raise ValueError("test text must be a non-empty, single line")
    if gray_levels < 2 or gray_levels > 16:
        raise ValueError("gray_levels must be between 2 and 16")
    font_size = max(8, cell_height - 2)
    font = ImageFont.truetype(str(font_path), font_size)
    rendered: list[tuple[int, bytes]] = []
    for character in text:
        bbox = font.getbbox(character)
        if bbox is None:
            raise ValueError(f"font has no drawable glyph for {character!r}")
        advance = max(1, min(cell_width, round(font.getlength(character))))
        image = Image.new("L", (stride, cell_height), 0)
        draw = ImageDraw.Draw(image)
        ink_w, ink_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = max(0, (cell_width - ink_w) // 2 - bbox[0])
        y = max(0, (cell_height - ink_h) // 2 - bbox[1])
        draw.text((x, y), character, font=font, fill=255)
        pixels = [
            round(value / 255 * (gray_levels - 1)) * 15 // (gray_levels - 1)
            for value in image.getdata()
        ]
        packed = bytearray()
        for i in range(0, len(pixels), 2):
            lo = pixels[i]
            hi = pixels[i + 1] if i + 1 < len(pixels) else 0
            packed.append(lo | (hi << 4))
        rendered.append((advance, bytes(packed)))
    return rendered


def allocation_for_member(
    table: list[int], member_offset: int, next_rel: int, original_compressed: int,
    known_next_offset: int | None = None, trust_archive_tail: bool = False,
) -> tuple[int, int]:
    archive_id = -1
    archive_start = archive_size = 0
    for i in range(ENTRY_COUNT):
        start = table[i] * SECTOR
        size = table[ENTRY_COUNT + i] * SECTOR
        if size and start <= member_offset < start + size:
            archive_id, archive_start, archive_size = i, start, size
            break
    if archive_id < 0:
        raise ValueError(f"member 0x{member_offset:X} is outside decoded archive entries")
    relative = member_offset - archive_start
    ends: list[int] = []
    if next_rel:
        ends.append(member_offset + next_rel)
    if known_next_offset is not None:
        ends.append(known_next_offset)
    if trust_archive_tail:
        ends.append(archive_start + archive_size)
    # Without a complete manifest, never infer that all bytes to archive EOF
    # are padding: unchained SLZ roots can follow the selected member.
    if not ends:
        ends.append(member_offset + 16 + original_compressed)
    allocation = min(ends) - member_offset - 16
    if allocation <= 0:
        raise ValueError("invalid/non-positive SLZ allocation")
    return archive_id, allocation


def writable_allocation(iso: Path, member_offset: int, original_compressed: int, structural: int) -> int:
    """Return original payload plus only verified contiguous zero padding."""
    if structural < original_compressed:
        raise ValueError(
            f"manifest/chain boundary cuts original payload: {structural} < {original_compressed}"
        )
    extension = structural - original_compressed
    if extension == 0:
        return original_compressed
    with iso.open("rb") as f:
        f.seek(member_offset + 16 + original_compressed)
        tail = f.read(extension)
    zeros = 0
    for value in tail:
        if value:
            break
        zeros += 1
    return original_compressed + zeros


def patch_iso(
    input_iso: Path,
    output_iso: Path,
    member_offset: int,
    message_id: int,
    text: str,
    font_path: Path,
    preview_path: Path | None,
    strategy: str = "auto",
    manifest_path: Path | None = None,
    space_code: int = 5,
    reuse_codes: list[int] | None = None,
) -> dict[str, object]:
    if input_iso.resolve() == output_iso.resolve():
        raise ValueError("input and output ISO must be different")
    if output_iso.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_iso}")
    decoded, slz = read_slz_member(input_iso, member_offset)
    if slz["mode"] != 2:
        raise ValueError(f"only mode-2 mclib patching is enabled; member uses mode {slz['mode']}")
    mclib = Mclib.parse(decoded)
    if mclib.glyph_width not in (24, 32) or mclib.glyph_height != mclib.glyph_width:
        raise ValueError(f"first-stage patch expects 24x24 or 32x32 glyphs, got {mclib.glyph_width}x{mclib.glyph_height}")
    unique_characters: list[str] = []
    character_indices: dict[str, int] = {}
    layout: list[int | None] = []
    for character in text:
        if character == " ":
            layout.append(None)
        else:
            if character not in character_indices:
                character_indices[character] = len(unique_characters)
                unique_characters.append(character)
            layout.append(character_indices[character])
    glyphs = render_glyphs(
        "".join(unique_characters), font_path, mclib.glyph_width,
        mclib.glyph_height, mclib.glyph_stride
    )
    table = read_index(input_iso)
    known_next_offset = None
    trust_archive_tail = False
    if manifest_path is not None:
        manifest_info = manifest_member_info(manifest_path, member_offset)
        known_next_offset = manifest_info["next_iso_offset"]
        trust_archive_tail = manifest_info["complete_archive_tail"]
    archive_id, structural_allocation = allocation_for_member(
        table, member_offset, slz["next_rel"], slz["compressed"], known_next_offset,
        trust_archive_tail,
    )
    allocation = writable_allocation(
        input_iso, member_offset, slz["compressed"], structural_allocation
    )
    attempts: list[dict[str, object]] = []
    candidates = [strategy] if strategy != "auto" else ["append", "reuse"]
    rebuilt = compressed = b""
    details: dict[str, object] = {}
    for candidate in candidates:
        try:
            if candidate == "append":
                candidate_rebuilt, candidate_details = mclib.replace_message_and_append_glyphs(
                    message_id, glyphs, layout, space_code
                )
            elif candidate == "reuse":
                candidate_rebuilt, candidate_details = mclib.replace_message_reusing_glyphs(
                    message_id, glyphs, layout, space_code, reuse_codes
                )
            else:
                raise ValueError(f"unknown patch strategy: {candidate}")
            candidate_compressed = compress_slz_mode2(candidate_rebuilt)
            if decompress_slz_payload(candidate_compressed, 2, len(candidate_rebuilt)) != candidate_rebuilt:
                raise AssertionError("SLZ round-trip failed")
            attempts.append({
                "strategy": candidate,
                "unpacked": len(candidate_rebuilt),
                "compressed": len(candidate_compressed),
                "fits": len(candidate_compressed) <= allocation,
            })
            if len(candidate_compressed) <= allocation:
                rebuilt, compressed, details = candidate_rebuilt, candidate_compressed, candidate_details
                break
        except ValueError as error:
            attempts.append({"strategy": candidate, "error": str(error), "fits": False})
    if not rebuilt:
        raise ValueError(
            f"no safe strategy fit allocation {allocation}: " + json.dumps(attempts, ensure_ascii=False)
        )

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(input_iso, output_iso)
    header = b"SLZ\x02" + struct.pack("<III", len(compressed), len(rebuilt), slz["next_rel"])
    with output_iso.open("r+b") as f:
        f.seek(member_offset)
        f.write(header)
        f.write(compressed)
        # Bytes after the new compressed payload are ignored by the decoder.
        # Leave them untouched to minimize the ISO diff.

    verified, verified_slz = read_slz_member(output_iso, member_offset)
    if verified != rebuilt:
        raise AssertionError("output ISO verification mismatch")
    if output_iso.stat().st_size != input_iso.stat().st_size:
        raise AssertionError("output ISO size changed")

    if preview_path is not None:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        cell_w, cell_h = mclib.glyph_width, mclib.glyph_height
        preview = Image.new("L", (len(glyphs) * cell_w, cell_h), 0)
        for i, (_, bitmap) in enumerate(glyphs):
            values: list[int] = []
            for value in bitmap:
                values.extend(((value & 15) * 17, (value >> 4) * 17))
            glyph = Image.new("L", (mclib.glyph_stride, cell_h))
            glyph.putdata(values[: mclib.glyph_stride * cell_h])
            preview.paste(glyph.crop((0, 0, cell_w, cell_h)), (i * cell_w, 0))
        preview.resize((preview.width * 8, preview.height * 8), Image.Resampling.NEAREST).save(preview_path)

    result: dict[str, object] = {
        "input_iso": str(input_iso),
        "output_iso": str(output_iso),
        "archive_id": archive_id,
        "member_offset": member_offset,
        "message_id": message_id,
        "text": text,
        "font": str(font_path),
        "unique_new_characters": "".join(unique_characters),
        "space_code": space_code,
        "old_slz_compressed": slz["compressed"],
        "new_slz_compressed": len(compressed),
        "slz_allocation": allocation,
        "slz_structural_allocation": structural_allocation,
        "next_rel": slz["next_rel"],
        "old_unpacked": slz["unpacked"],
        "new_unpacked": len(rebuilt),
        "rebuilt_sha256": sha256(rebuilt),
        "verified_mode": verified_slz["mode"],
        "attempts": attempts,
        **details,
    }
    return result


def _iso_backed_manifest_rows(manifest: Path) -> list[dict[str, str]]:
    with manifest.open("r", encoding="utf-8", newline="") as f:
        return [
            r for r in csv.DictReader(f)
            if r["iso_offset"] and int(r["iso_offset"]) >= 0 and int(r["depth"]) == 0
        ]


def manifest_member_info(manifest: Path, member_offset: int) -> dict[str, object]:
    rows = _iso_backed_manifest_rows(manifest)
    matches = [r for r in rows if int(r["iso_offset"]) == member_offset]
    if len(matches) != 1:
        raise ValueError(f"ISO offset 0x{member_offset:X} was not one unique manifest stream")
    row = matches[0]
    archive_id = int(row["archive_id"])
    later = sorted(
        int(r["iso_offset"]) for r in rows
        if int(r["archive_id"]) == archive_id and int(r["iso_offset"]) > member_offset
    )
    return {
        "archive_id": archive_id,
        "next_iso_offset": later[0] if later else None,
        # The complete extractor catalogued the whole raw entry.  If this is
        # the last level-0 stream, archive EOF is the verified slot boundary.
        "complete_archive_tail": not later,
    }


def lookup_manifest(manifest: Path, stream_id: int) -> int:
    rows = _iso_backed_manifest_rows(manifest)
    matches = [r for r in rows if int(r["stream_id"]) == stream_id]
    if len(matches) != 1 or not matches[0]["iso_offset"]:
        raise ValueError(f"stream id {stream_id} was not one unique ISO-backed stream")
    offset = int(matches[0]["iso_offset"])
    if offset < 0:
        raise ValueError("nested decoded streams cannot be patched directly")
    return offset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_iso", type=Path)
    ap.add_argument("output_iso", type=Path)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--member-offset", type=lambda v: int(v, 0))
    target.add_argument("--stream-id", type=int)
    ap.add_argument(
        "--manifest",
        type=Path,
        help="complete extraction manifest (required only with --stream-id)",
    )
    ap.add_argument("--message-id", type=int, required=True)
    ap.add_argument("--text", default="한글")
    ap.add_argument(
        "--font",
        type=Path,
        required=True,
        help="font with redistribution terms appropriate for the generated patch",
    )
    ap.add_argument("--strategy", choices=("auto", "append", "reuse"), default="auto")
    ap.add_argument("--space-code", type=lambda v: int(v, 0), default=5)
    ap.add_argument(
        "--reuse-codes",
        help="comma-separated existing local codes for the unique non-space characters",
    )
    ap.add_argument("--preview", type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    if args.member_offset is not None:
        offset = args.member_offset
    else:
        if args.manifest is None:
            ap.error("--manifest is required with --stream-id")
        offset = lookup_manifest(args.manifest, args.stream_id)
    reuse_codes = [int(v, 0) for v in args.reuse_codes.split(",")] if args.reuse_codes else None
    result = patch_iso(
        args.input_iso, args.output_iso, offset, args.message_id, args.text, args.font,
        args.preview, args.strategy, args.manifest, args.space_code, reuse_codes
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

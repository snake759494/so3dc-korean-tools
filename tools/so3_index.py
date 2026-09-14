#!/usr/bin/env python3
"""Decode and compare the hidden tri-Ace PS2 archive index.

Star Ocean 3 stores 6144 parallel uint32 arrays (LBA, sector count, aux)
at byte offset 0x200000.  The XOR state machine below is the one documented
by CUE's triAce-PS2 extractor.
"""

from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path


SECTOR = 0x800
TABLE_OFFSET = 0x200000
ENTRY_COUNT = 0x1800
SEED = 0x13578642


def u32(value: int) -> int:
    return value & 0xFFFFFFFF


def decode_index(path: Path) -> list[tuple[int, int, int]]:
    with path.open("rb") as source:
        source.seek(TABLE_OFFSET)
        raw = source.read(ENTRY_COUNT * 3 * 4)
    expected = ENTRY_COUNT * 3 * 4
    if len(raw) != expected:
        raise ValueError(f"short index: got {len(raw)} bytes, expected {expected}")

    words = list(struct.unpack(f"<{ENTRY_COUNT * 3}I", raw))
    key = SEED
    for index in range(ENTRY_COUNT):
        words[index] ^= key
        key = u32(key ^ u32(key << 1))
        words[ENTRY_COUNT + index] ^= key
        key = u32(key ^ u32(~SEED))
        words[ENTRY_COUNT * 2 + index] ^= key
        key = u32(key ^ u32(key << 2) ^ SEED)

    words[0] = TABLE_OFFSET // SECTOR
    return [
        (
            words[index],
            words[ENTRY_COUNT + index],
            words[ENTRY_COUNT * 2 + index],
        )
        for index in range(ENTRY_COUNT)
    ]


def write_csv(path: Path, rows: list[tuple[int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("id", "lba", "sectors", "bytes", "aux"))
        for index, (lba, sectors, aux) in enumerate(rows):
            writer.writerow((index, lba, sectors, sectors * SECTOR, aux))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iso", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()

    rows = decode_index(args.iso)
    if args.output:
        write_csv(args.output, rows)

    nonempty = sum(sectors != 0 for _, sectors, _ in rows)
    print(f"entries={len(rows)} nonempty={nonempty}")

    if args.compare:
        other = decode_index(args.compare)
        print("id,lba,sectors,aux,other_lba,other_sectors,other_aux")
        changes = 0
        for index, (left, right) in enumerate(zip(rows, other)):
            if left != right:
                changes += 1
                print(f"{index},{left[0]},{left[1]},{left[2]},"
                      f"{right[0]},{right[1]},{right[2]}")
        print(f"changed={changes}")


if __name__ == "__main__":
    main()

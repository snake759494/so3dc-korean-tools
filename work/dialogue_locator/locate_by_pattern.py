#!/usr/bin/env python3
"""Locate the issue #1 dialogue by glyph-equality and line-shape constraints.

This is deliberately Unicode-agnostic: each mclib message is tokenized into
glyph operands and control tokens, then the highly distinctive repeated-glyph
pattern of the supplied Japanese text is matched within each rendered line.
"""

from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UNIQUE = ROOT / "work/mclib_all_decode/unique_messages.csv"


def tokenize(data: bytes):
    """Return glyph codes, NL markers, and generic control markers.

    Generic controls are retained as separators.  Parameter bytes after an
    unknown control are not guessed; line matching is instead done on every
    contiguous glyph run so false parameter glyphs cannot bridge controls.
    """
    out = []
    pos = 0
    while pos < len(data):
        a = data[pos]
        if a == 0:
            out.append(("END", 0))
            break
        if a < 0x80:
            out.append(("G", a))
            pos += 1
            continue
        if pos + 1 >= len(data):
            out.append(("BAD", a))
            break
        b = data[pos + 1]
        if b < 0x80:
            out.append(("G", (a & 0x7f) | (b << 7)))
            pos += 2
            continue
        if (a, b) == (0x80, 0x80):
            out.append(("NL", 0))
            pos += 2
            continue
        if (a, b) == (0x8a, 0x80) and pos + 6 <= len(data):
            out.append(("CTRL", 0x800a))
            pos += 6
            continue
        out.append(("CTRL", a | (b << 8)))
        pos += 2
    return out


def glyph_lines(tokens):
    lines, cur = [], []
    for kind, value in tokens:
        if kind == "G":
            cur.append(value)
        elif kind == "NL":
            lines.append(cur)
            cur = []
        elif kind in ("END", "BAD"):
            lines.append(cur)
            cur = []
        # Other controls do not split the visual line.  Their parameters are
        # conservatively consumed only when established above.
    if cur:
        lines.append(cur)
    return lines


def glyph_runs(tokens):
    """Contiguous glyph runs split at every control/newline/end token."""
    runs, cur = [], []
    for kind, value in tokens:
        if kind == "G":
            cur.append(value)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def distinctive_windows(tokens):
    """Find the ココ...･･･ line's equality pattern in glyph runs."""
    found = []
    for ri, run in enumerate(glyph_runs(tokens)):
        # Accept surrounding glyphs because a control-free run can contain
        # multiple visual lines when wrapping is automatic.
        for start in range(max(0, len(run) - 13)):
            w = run[start:start+14]
            if len(w) < 14:
                continue
            score, why = 0, []
            if w[0] == w[1]:
                score += 10; why.append("00=01")
            if w[10] == w[11] == w[12]:
                score += 10; why.append("10=11=12")
            # Expected non-equalities eliminate padding/art patterns.
            if len(set(w[:10])) >= 7:
                score += 3; why.append("head-diverse")
            if w[9] != w[10] and w[12] != w[13]:
                score += 2; why.append("triple-bounded")
            if score >= 20:
                found.append((score, ri, start, w, why))
    return found


def body_score(lines):
    """Score equality/length traits of the four supplied dialogue lines."""
    best = []
    for i in range(max(0, len(lines) - 3)):
        a, b, c, d = lines[i:i+4]
        score, why = 0, []

        # 「ねぇ、フェイト。 : 9 glyphs (quote may be 9th/10th by font)
        if 8 <= len(a) <= 13:
            score += 2; why.append(f"L1={len(a)}")

        # ココのホテルってさぁ･･･。 : 15 glyphs, starts with ココ,
        # and normally contains three identical middle-dot glyphs.
        if 12 <= len(b) <= 19:
            score += 2; why.append(f"L2={len(b)}")
        if len(b) >= 2 and b[0] == b[1]:
            score += 8; why.append("L2[0]=L2[1]")
        if any(b[j] == b[j+1] == b[j+2] for j in range(max(0, len(b)-2))):
            score += 7; why.append("L2 triple")

        # 104号室が無いよね。 : 12 glyphs.
        if 10 <= len(c) <= 15:
            score += 2; why.append(f"L3={len(c)}")

        # これって何でなのかな? : 11 glyphs, な occurs twice.
        if 9 <= len(d) <= 14:
            score += 2; why.append(f"L4={len(d)}")
        if len(d) >= 4 and len(b) >= 9:
            # っ,て recur at L2 positions 6,7 (offset can vary if punctuation).
            if d[2] in b and d[3] in b:
                score += 3; why.append("L4 って recur")
        if len(d) >= 10 and d[-2] in d[:-2]:
            score += 3; why.append("L4 な repeat")

        # ね recurs between L1 and the end of L3; の between L2/L4.
        if a and c and any(x in c[-4:] for x in a):
            score += 2; why.append("L1/L3 recur")
        if b and d and set(b) & set(d):
            score += 1; why.append("L2/L4 recur")
        best.append((score, i, why, [len(x) for x in (a,b,c,d)]))
    return max(best, default=(0, -1, [], []))


def main():
    hits = []
    windows = []
    with UNIQUE.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["representative_hex_truncated"] != "0":
                continue
            data = bytes.fromhex(row["representative_hex"])
            tokens = tokenize(data)
            for item in distinctive_windows(tokens):
                windows.append((item[0], row, *item[1:]))
            lines = glyph_lines(tokens)
            if len(lines) < 4:
                continue
            score, start, why, lens = body_score(lines)
            if score >= 14:
                hits.append((score, row, start, why, lens, lines, tokens))

    hits.sort(key=lambda x: (-x[0], int(x[1]["representative_archive_id"])))
    windows.sort(key=lambda x: (-x[0], int(x[1]["representative_archive_id"])))
    print("=== DISTINCTIVE GLYPH WINDOWS ===")
    for score, row, run_index, start, window, why in windows[:200]:
        print(
            f"score={score} archive={row['representative_archive_id']} "
            f"stream={row['representative_stream_id']} mid={row['representative_message_id']} "
            f"off={row['representative_offset']} bytes={row['representative_segment_bytes']} "
            f"run={run_index} start={start} why={','.join(why)}"
        )
        print(f"  window={' '.join(map(str,window))}")
        print(f"  hex={row['representative_hex']}")
        print(f"  path={row['representative_path']}")

    print("=== FOUR-LINE SHAPE HITS ===")
    for score, row, start, why, lens, lines, tokens in hits[:100]:
        print(
            f"score={score:2d} archive={row['representative_archive_id']} "
            f"stream={row['representative_stream_id']} mid={row['representative_message_id']} "
            f"off={row['representative_offset']} bytes={row['representative_segment_bytes']} "
            f"line={start} lens={lens} why={','.join(why)}"
        )
        for n, line in enumerate(lines[max(0,start):start+4], 1):
            print(f"  {n}: {' '.join(map(str,line))}")
        print(f"  hex={row['representative_hex']}")
        print(f"  path={row['representative_path']}")


if __name__ == "__main__":
    main()

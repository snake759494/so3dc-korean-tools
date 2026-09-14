#!/usr/bin/env python3
"""Empirical consistency solver for SO3 DC message control opcodes.

Grammar (verified upstream):
  - glyph codes: little-endian 7-bit varints, 1 byte if <0x80 else
    (b0&0x7F)|(b1<<7) with b1<0x80. 0x00 terminates the message.
  - controls: byte pairs where BOTH bytes >=0x80. Family = b0&0x7F
    (second byte is expected to be 0x80; asserted/recorded otherwise).

Strict control grammar (confirmed by ELF disassembly of the decrypted
resident engine module 0002.sle member_01 @ VA 0x003E7C80, see
work/executable_mapping/decoded/):
  The engine reads b0; if b0 == 0 the message ends; if b0 < 0x80 it is a
  1-byte glyph; otherwise it reads b1 and forms code = (b0&0x7F)|(b1<<7).
  If code & 0x4000 == 0 (i.e. b1 < 0x80) it is a 2-byte glyph.  Otherwise a
  CONTROL with index = code - 0x4000; index < 0x24 dispatches through a
  36-entry jump table; larger indexes are silently skipped with NO operand.
  In practice every control on disc is an `XX 80` pair (b1 == 0x80,
  family = b0 & 0x7F in 0x00..0x23).  Engine dispatch sites (all agree):
    - tokenizer       0x00461DD0, jump table @ VA 0x005077A0
    - measuring scan  0x004627F0, jump table @ VA 0x00507830
    - direct renderer 0x004C8A20, jump table @ VA 0x00508DB0
  For the pure solver we reject b1 not in {glyph range, 0x80} at token
  boundaries: pairs like CD CC only ever occur *inside* operands
  (e.g. float 0.1 = CD CC CC 3D), never as real controls.

For every control family we hypothesize an operand shape:
  ('F', L)  : fixed, L extra operand bytes after the pair (total 2+L)
  ('Z',)    : zero-terminated operand (consume until and including 0x00)
  ('V',)    : a single glyph-varint operand (1-2 bytes)

A hypothesis set is CONSISTENT if every message in the corpus tokenizes
cleanly: it reaches a 0x00 terminator exactly at a token boundary and
everything after it (up to the boundary-derived segment end) is NUL padding,
and no high/high pair is ever consumed as a glyph.

Solving is joint: a per-message DFS enumerates all locally-consistent
assignments for the families appearing in that message; global candidate
sets are the intersection over all messages.  Families are locked eagerly
when their intersection becomes a singleton; rounds repeat until stable.
A final audit re-derives, for every family, the set of hypotheses that
remain corpus-consistent with all other families fixed (ambiguity report).

Outputs (work/full_ko/):
  solver_candidates.json  : per-family surviving hypotheses + stats
  solver_run.log          : run log incl. final verification pass
"""

from __future__ import annotations

import csv
import json
import os
import struct
import time
from collections import Counter, defaultdict
from pathlib import Path

WS = Path(os.environ.get("SO3_WS", str(Path(__file__).resolve().parents[2])))
# Disc-1 defaults; disc-2 runs override with --catalog / --out-dir (the catalog
# rows carry absolute decoded paths, so no separate decoded-root is needed).
CATALOG = WS / "work" / "mclib_all_decode" / "container_catalog.csv"
OUTDIR = WS / "work" / "full_ko"
MAX_FIXED = 16  # max extra operand bytes considered

# ---------------------------------------------------------------- corpus


def parse_mclib_segments(data: bytes) -> list[bytes]:
    """Return message segments (boundary-derived, incl. NUL padding)."""
    if len(data) < 0x80 or not data.startswith(b"so3mclib "):
        raise ValueError("not an so3mclib")
    words = struct.unpack_from("<13I", data, 0x10)
    table_start, text_start, width_start, _bitmap_start = words[:4]
    glyph_count = words[4]
    mapping_count = words[11]
    file_size = words[12]
    if file_size != len(data):
        raise ValueError("size mismatch")
    rows = [struct.unpack_from("<II", data, table_start + i * 8) for i in range(mapping_count)]
    offsets = sorted({off for _, off in rows})
    text_end = width_start if glyph_count else file_size
    segs = []
    for i, off in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < len(offsets) else text_end - text_start
        segs.append(data[text_start + off: text_start + end])
    return segs


def load_corpus(catalog: Path = None) -> tuple[list[bytes], Counter, int]:
    """Unique segments across unique container files + multiplicity."""
    catalog = catalog or CATALOG
    seen_sha: set[str] = set()
    seg_mult: Counter = Counter()  # segment bytes -> messages over unique files
    total_msgs = 0
    with open(catalog, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sha = row["file_sha256"]
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
            data = Path(row["path"]).read_bytes()
            for seg in parse_mclib_segments(data):
                total_msgs += 1
                seg_mult[seg] += 1
    return list(seg_mult.keys()), seg_mult, total_msgs


# ---------------------------------------------------------------- tokenizer

HYP_FIXED = [("F", n) for n in range(MAX_FIXED + 1)]
ALL_HYPS = HYP_FIXED + [("Z",), ("V",)]


def consume_operand(seg: bytes, pos: int, hyp: tuple) -> int:
    """Return new pos after operand, or -1 if it does not fit."""
    n = len(seg)
    if hyp[0] == "F":
        newpos = pos + hyp[1]
        return newpos if newpos <= n else -1
    if hyp[0] == "Z":
        z = seg.find(b"\x00", pos)
        return z + 1 if z != -1 else -1
    # 'V': one glyph varint
    if pos >= n:
        return -1
    b0 = seg[pos]
    if b0 < 0x80:
        return pos + 1
    if pos + 1 < n and seg[pos + 1] < 0x80:
        return pos + 2
    return -1


def parse_message(seg: bytes, assign: dict, branch: bool, free: set | None = None):
    """DFS-parse one segment.

    assign: family -> hyp for locked families.
    free:   families to branch over even if present in assign (audit mode).
    Returns list of frozenset({(fam, hyp), ...}) local assignments that give
    a clean parse (empty frozenset if all families were locked).
    """
    n = len(seg)
    results = []
    free = free or set()

    def step(pos: int, local: dict):
        while True:
            if pos >= n:
                return  # ran off the end without terminator
            b0 = seg[pos]
            if b0 == 0:
                if not any(seg[pos + 1:]):  # only NUL padding may follow
                    results.append(frozenset(local.items()))
                return
            if b0 < 0x80:
                pos += 1
                continue
            if pos + 1 >= n:
                return  # lone trailing high byte: invalid
            b1 = seg[pos + 1]
            if b1 < 0x80:
                pos += 2  # 2-byte glyph
                continue
            if b1 != 0x80:
                return  # b0>=0x80 & b1>=0x81: impossible at a valid boundary
            fam = (b0, b1)
            hyp = local.get(fam)
            if hyp is None and fam not in free:
                hyp = assign.get(fam)
            if hyp is not None:
                pos2 = consume_operand(seg, pos + 2, hyp)
                if pos2 < 0:
                    return
                pos = pos2
                continue
            if not branch:
                return
            for h in ALL_HYPS:
                pos2 = consume_operand(seg, pos + 2, h)
                if pos2 >= 0:
                    local[fam] = h
                    step(pos2, local)
                    del local[fam]
            return

    step(0, {})
    return results


def scan_families(seg: bytes, assign: dict) -> tuple[set, set, bool]:
    """(known_families_seen, unknown_families_possibly_present, exact).

    Walks with current knowns; on the first unknown family alignment is lost,
    so the tail is scanned for raw high/high pairs (superset). exact=True
    means we stayed aligned to the terminator.
    """
    known, unknown = set(), set()
    pos, n = 0, len(seg)
    while pos < n:
        b0 = seg[pos]
        if b0 == 0:
            return known, unknown, True
        if b0 < 0x80:
            pos += 1
            continue
        if pos + 1 >= n:
            return known, unknown, True
        b1 = seg[pos + 1]
        if b1 < 0x80:
            pos += 2
            continue
        if b1 != 0x80:
            return known, unknown, False
        fam = (b0, b1)
        if fam in assign:
            pos2 = consume_operand(seg, pos + 2, assign[fam])
            if pos2 < 0:
                return known, unknown, False
            known.add(fam)
            pos = pos2
            continue
        unknown.add(fam)
        # alignment lost past an unknown fixed-length op: superset scan the
        # tail for further XX80 controls so they are considered this round.
        for i in range(pos + 2, n - 1):
            if seg[i] >= 0x80 and seg[i + 1] == 0x80 and (seg[i], seg[i + 1]) not in assign:
                unknown.add((seg[i], seg[i + 1]))
        return known, unknown, False
    return known, unknown, True


# ---------------------------------------------------------------- solving


def solve(segments: list[bytes], log):
    assign: dict = {}
    hopeless: list = []
    round_no = 0
    while True:
        round_no += 1
        t0 = time.time()
        work = []
        for seg in segments:
            _k, unk, _exact = scan_families(seg, assign)
            if unk:
                work.append((len(unk), seg))
        work.sort(key=lambda t: t[0])
        if not work:
            log(f"round {round_no}: nothing left to solve")
            break

        cand: dict = {}
        n_locked_this_round = 0
        n_deferred = 0
        for k, seg in work:
            if k > 5:
                n_deferred += 1
                continue
            locals_ = parse_message(seg, assign, branch=True)
            if not locals_:
                hopeless.append(seg)
                continue
            per_fam = defaultdict(set)
            for fs in locals_:
                for fam, h in fs:
                    per_fam[fam].add(h)
            for fam, hs in per_fam.items():
                if fam in assign:
                    continue
                if fam in cand:
                    cand[fam] &= hs
                else:
                    cand[fam] = set(hs)
                if len(cand[fam]) == 1:
                    assign[fam] = next(iter(cand[fam]))
                    n_locked_this_round += 1
                    log(f"  locked {fam[0]:02x}{fam[1]:02x} -> {assign[fam]}")
        log(f"round {round_no}: {len(work)} msgs w/ unknowns, deferred {n_deferred}, "
            f"locked {n_locked_this_round}, {time.time()-t0:.1f}s")
        if n_locked_this_round == 0:
            # report whatever is still ambiguous
            for fam, hs in sorted(cand.items()):
                log(f"  UNRESOLVED {fam[0]:02x}{fam[1]:02x}: {len(hs)} candidates "
                    f"{sorted(hs) if len(hs) <= 6 else ''}")
            return assign, cand, hopeless
    return assign, {}, hopeless


def audit(segments: list[bytes], assign: dict, log):
    """Per-family final ambiguity: hyps consistent with everything else fixed."""
    by_fam_msgs = defaultdict(list)
    for seg in segments:
        known, unk, _ = scan_families(seg, assign)
        for fam in known | unk:
            by_fam_msgs[fam].append(seg)
    audit_result = {}
    for fam in sorted(by_fam_msgs):
        surviving = set(ALL_HYPS)
        msgs = by_fam_msgs[fam]
        for seg in msgs:
            locals_ = parse_message(seg, assign, branch=True, free={fam})
            hs = {h for fs in locals_ for f, h in fs if f == fam}
            surviving &= hs
            if len(surviving) <= 1:
                break
        audit_result[fam] = (sorted(surviving), len(msgs))
        mark = "" if assign.get(fam) in surviving else "  **LOCKED VALUE NOT IN AUDIT SET**"
        log(f"audit {fam[0]:02x}{fam[1]:02x}: locked={assign.get(fam)} "
            f"survivors={sorted(surviving)} over {len(msgs)} msgs{mark}")
    return audit_result


def float_evidence(segments: list[bytes], assign: dict, log):
    """For families with 4-byte operands, how often does the operand parse
    as a plausible float32 (0.001..1000 or 0.0)?"""
    stats = defaultdict(lambda: [0, 0, Counter()])
    for seg in segments:
        pos, n = 0, len(seg)
        while pos < n:
            b0 = seg[pos]
            if b0 == 0:
                break
            if b0 < 0x80:
                pos += 1
                continue
            if pos + 1 >= n:
                break
            b1 = seg[pos + 1]
            if b1 < 0x80:
                pos += 2
                continue
            fam = (b0, b1)
            hyp = assign.get(fam)
            if hyp is None:
                break
            if hyp == ("F", 4) and pos + 6 <= n:
                val = struct.unpack_from("<f", seg, pos + 2)[0]
                st = stats[fam]
                st[1] += 1
                if val == 0.0 or 0.001 <= abs(val) <= 1000.0:
                    st[0] += 1
                    st[2][round(val, 4)] += 1
            pos2 = consume_operand(seg, pos + 2, hyp)
            if pos2 < 0:
                break
            pos = pos2
    for fam, (good, tot, vals) in sorted(stats.items()):
        log(f"float-check {fam[0]:02x}{fam[1]:02x}: {good}/{tot} plausible "
            f"float32; top values {vals.most_common(6)}")
    return {f"{f[0]:02x}{f[1]:02x}": {"plausible": g, "total": t,
                                      "top": vals.most_common(8)}
            for f, (g, t, vals) in stats.items()}


def control_census(segments: list[bytes], seg_mult: Counter, assign: dict):
    """Count aligned control occurrences under the final table."""
    census = Counter()
    census_msgs = Counter()
    for seg in segments:
        mult = seg_mult[seg]
        seen = set()
        pos, n = 0, len(seg)
        while pos < n:
            b0 = seg[pos]
            if b0 == 0:
                break
            if b0 < 0x80:
                pos += 1
                continue
            if pos + 1 >= n:
                break
            b1 = seg[pos + 1]
            if b1 < 0x80:
                pos += 2
                continue
            fam = (b0, b1)
            hyp = assign.get(fam)
            if hyp is None:
                break
            census[fam] += mult
            seen.add(fam)
            pos = consume_operand(seg, pos + 2, hyp)
            if pos < 0:
                break
        for fam in seen:
            census_msgs[fam] += mult
    return census, census_msgs


def verify(seg_mult: Counter, assign: dict, log):
    ok = bad = 0
    bad_examples = []
    for seg, mult in seg_mult.items():
        res = parse_message(seg, assign, branch=False)
        if res:
            ok += mult
        else:
            bad += mult
            if len(bad_examples) < 10:
                bad_examples.append(seg.hex())
    log(f"verification: {ok} messages tokenize cleanly, {bad} fail "
        f"({ok}/{ok+bad})")
    for ex in bad_examples:
        log(f"  FAIL example: {ex[:200]}")
    return ok, bad, bad_examples


def main(catalog: Path = None, outdir: Path = None):
    catalog = catalog or CATALOG
    outdir = outdir or OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    logf = open(outdir / "solver_run.log", "w", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    t0 = time.time()
    segments, seg_mult, total_msgs = load_corpus(catalog)
    log(f"corpus: {total_msgs} messages over unique container files, "
        f"{len(segments)} unique segments ({time.time()-t0:.1f}s)")

    # sanity: do any aligned controls have a second byte != 0x80?
    assign, cand, hopeless = solve(segments, log)

    weird_second = sorted({f"{f[0]:02x}{f[1]:02x}" for f in assign if f[1] != 0x80})
    if weird_second:
        log(f"NOTE: control pairs with second byte != 0x80: {weird_second}")

    log("\n=== solver locked table ===")
    for fam, hyp in sorted(assign.items()):
        log(f"{fam[0]:02x}{fam[1]:02x} -> {hyp}")
    if hopeless:
        log(f"\n{len(hopeless)} segments had NO consistent assignment:")
        for seg in hopeless[:8]:
            log("  " + seg.hex()[:200])

    log("\n=== per-family ambiguity audit ===")
    audit_result = audit(segments, assign, log)

    log("\n=== float32 plausibility for 4-byte operands ===")
    floats = float_evidence(segments, assign, log)

    census, census_msgs = control_census(segments, seg_mult, assign)
    log("\n=== aligned control census (occurrences / messages) ===")
    for fam in sorted(census):
        log(f"{fam[0]:02x}{fam[1]:02x}: {census[fam]} occurrences in "
            f"{census_msgs[fam]} messages")

    log("\n=== full-corpus verification ===")
    ok, bad, bad_ex = verify(seg_mult, assign, log)

    out = {
        "locked": {f"{f[0]:02x}{f[1]:02x}": list(h) for f, h in sorted(assign.items())},
        "ambiguous": {f"{f[0]:02x}{f[1]:02x}": [list(h) for h in sorted(hs)]
                      for f, hs in sorted(cand.items()) if len(hs) != 1},
        "audit": {f"{f[0]:02x}{f[1]:02x}": {"survivors": [list(h) for h in surv],
                                            "messages": nm}
                  for f, (surv, nm) in sorted(audit_result.items())},
        "float_evidence": floats,
        "census_occurrences": {f"{f[0]:02x}{f[1]:02x}": census[f] for f in sorted(census)},
        "census_messages": {f"{f[0]:02x}{f[1]:02x}": census_msgs[f] for f in sorted(census_msgs)},
        "hopeless_examples": [s.hex() for s in hopeless[:20]],
        "hopeless_count": len(hopeless),
        "verify_ok": ok,
        "verify_bad": bad,
        "verify_bad_examples": bad_ex,
        "total_unique_file_messages": total_msgs,
        "unique_segments": len(segments),
    }
    (outdir / "solver_candidates.json").write_text(json.dumps(out, indent=2))
    log(f"\nwrote solver_candidates.json; total {time.time()-t0:.1f}s")


# ------------------------------------------------------- ELF-derived table
#
# Authoritative operand table extracted from the resident engine module
# (0002.sle member_01, VA 0x003E7C80, sha256 E10283983CB167AE...).  Three
# independent dispatch jump tables were decoded and all agree:
#   tokenizer       dispatch 0x00461DD0, table @ 0x005077A0
#   measuring scan  dispatch 0x004627F0, table @ 0x00507830
#   direct renderer dispatch 0x004C8A20, table @ 0x00508DB0
# op: "F0" no operand / "F1" one byte / "F4" four bytes / "Z" NUL-terminated
# (terminating NUL is consumed and belongs to the operand, not the message).
ENGINE_TABLE = {
    0x00: ("F0", "newline", "line counter += 1 (scan 0x462808)"),
    0x01: ("F0", "page break / wait: tokenizer emits 0x4001 stop token with resume pointer (0x461E70); measuring scan stops (0x462810)", ""),
    0x02: ("F1", "set 1-byte text param (direct renderer stores at +0x9C; text speed/wait)", "0x4C8A58"),
    0x03: ("F0", "reset the 8280 param to default", "0x4C8A68"),
    0x04: ("F0", "flag control, token-only", "0x462230"),
    0x05: ("F4", "4-byte operand (float32 in corpus); tokenized, skipped by renderers", "0x461E18"),
    0x06: ("F4", "4-byte operand (float32 in corpus); tokenized, skipped by renderers", "0x461E18"),
    0x07: ("F0", "flag control, token-only", "0x462230"),
    0x08: ("F1", "set text color: 1-byte palette index -> RGBA via 0x465D40", "0x4C8A80"),
    0x09: ("F0", "reset text color (tokenizer emits 0x00808080)", "0x461EA8"),
    0x0A: ("F4", "set glyph scale, float32 multiplier for x and y", "0x4C8AA0 lwc1/mul.s"),
    0x0B: ("F0", "reset glyph scale (tokenizer emits 1.0f)", "0x461EC0"),
    0x0C: ("F4", "set float param at +0x88 (glyph advance/spacing)", "0x4C8AF8"),
    0x0D: ("F0", "reset the 8C80 param (tokenizer emits 0.0)", "0x461ED0"),
    0x0E: ("F4", "set float param at +0x8C (line spacing)", "0x4C8B28"),
    0x0F: ("F0", "reset the 8E80 param (tokenizer emits 2.0f)", "0x461EE0"),
    0x10: ("F0", "ruby base start", "0x4C8B58 -> 0x465AA0"),
    0x11: ("Z", "ruby text, zero-terminated", "0x461EF0 / 0x4C8B68"),
    0x12: ("F1", "insert runtime registry string #n (gp+0x568 table)", "0x4C8BB0"),
    0x13: ("F1", "character name by 1-byte id (0x4657F0 lookup)", "0x4C8BD8"),
    0x14: ("F4", "4-byte operand; tokenized, skipped by renderers", "0x461E18"),
    0x15: ("F4", "4-byte operand; tokenized, skipped by renderers", "0x461E18"),
    0x16: ("F0", "flag control, token-only", "0x462230"),
    0x17: ("F0", "flag control, token-only", "0x462230"),
    0x18: ("F0", "flag control, token-only (emphasis-dot marker per corpus contexts)", "0x462230"),
    0x19: ("F0", "flag control, token-only", "0x462230"),
    0x1A: ("F1", "set 1-byte param (tokenized as family 0x1A value)", "0x461E08"),
    0x1B: ("F0", "reset the 9A80 param from state +0x554", "0x4621C8"),
    0x1C: ("Z", "zero-terminated payload drawn via 0x465970", "0x461EF0 / 0x4C8D38"),
    0x1D: ("F1", "1-byte param, pointer captured (handler 0x4C8720)", "0x4621D8"),
    0x1E: ("F4", "event cue, 4-byte operand (+derived halfword via 0x461440)", "0x4621E8"),
    0x1F: ("F0", "flag control, token-only", "0x462230"),
    0x20: ("F1", "1-byte param (tokenized as family 0x20 value)", "0x461E08"),
    0x21: ("F1", "character name via registry indirection (1-byte registry slot)", "0x461FA8"),
    0x22: ("F1", "item name by registry slot, raw", "0x462058"),
    0x23: ("F1", "item name by registry slot, validated (checksum 0x83CF)", "0x462058"),
}

OP_TO_HYP = {"F0": ("F", 0), "F1": ("F", 1), "F4": ("F", 4), "Z": ("Z",)}

# Final candidate sets from the pure corpus solver (see solver_run.log):
# families the solver locked uniquely, and the surviving candidate sets for
# the ones it could not separate on corpus evidence alone.
SOLVER_LOCKED = {
    0x00: ("F", 0), 0x01: ("F", 0), 0x03: ("F", 0), 0x04: ("F", 0),
    0x05: ("F", 4), 0x06: ("F", 4), 0x07: ("F", 0), 0x09: ("F", 0),
    0x0B: ("F", 0), 0x0D: ("F", 0), 0x11: ("Z",), 0x14: ("F", 4),
    0x15: ("F", 4), 0x18: ("F", 0), 0x19: ("F", 0), 0x1E: ("F", 4),
    0x1F: ("F", 0),
}
SOLVER_AMBIGUOUS = {
    0x02: [("F", 1), ("V",)],
    0x08: [("F", 1), ("V",)],
    0x0A: [("F", 3), ("F", 4)],
    0x0C: [("F", 3), ("F", 4), ("F", 5)],
    0x0E: [("F", 4), ("F", 8), ("F", 9), ("F", 10)],
    0x0F: None,  # 14 candidates (only 2 aligned occurrences reachable)
    0x10: [("F", 0), ("F", 1), ("Z",)],
    0x12: [("F", 1), ("V",)],
    0x13: [("F", 0), ("F", 1), ("V",)],
    0x1C: [("F", 11), ("Z",)],
    0x1D: [("F", 1), ("V",)],
    0x21: [("F", 0), ("F", 1), ("F", 3), ("F", 5), ("V",)],
    0x23: [("F", 0), ("F", 1), ("V",)],
}
# Priors from the shipped Hyda patcher (in-game verified on the Hyda banks).
# NOTE 0x1C (9C80): patcher used total size 5 (= 3 fixed operand bytes); the
# engine actually NUL-terminates this operand.  All Hyda occurrences carry a
# 2-byte payload + 0x00, for which both readings consume identical bytes, so
# the patcher was behaviourally correct on its banks but the general rule is
# zero-terminated.
PATCHER_PRIORS = {0x00: 2, 0x01: 2, 0x02: 3, 0x04: 2, 0x05: 6, 0x06: 6,
                  0x07: 2, 0x08: 3, 0x09: 2, 0x0A: 6, 0x0B: 2, 0x10: 2,
                  0x12: 3, 0x13: 3, 0x14: 6, 0x15: 6, 0x1C: 5, 0x1D: 3,
                  0x1E: 6, 0x1F: 2, 0x11: "zero_terminated"}


def engine_tokenize(seg: bytes):
    """Tokenize exactly like the engine.  Returns (ok, controls_seen, err).

    controls_seen: list of (family, operand_bytes_consumed, operand_start).
    Unknown control indexes (>= 0x24, incl. any pair with b1 > 0x80) consume
    only the two control bytes, exactly like the engine's bounds check.
    """
    pos, n = 0, len(seg)
    seen = []
    while True:
        if pos >= n:
            return False, seen, "ran off end without NUL"
        b0 = seg[pos]
        if b0 == 0:
            if any(seg[pos + 1:]):
                return False, seen, "non-NUL padding after terminator"
            return True, seen, None
        if b0 < 0x80:
            pos += 1
            continue
        if pos + 1 >= n:
            return False, seen, "truncated 2-byte code"
        b1 = seg[pos + 1]
        pos += 2
        if b1 < 0x80:
            continue  # 2-byte glyph
        idx = ((b0 & 0x7F) | (b1 << 7)) - 0x4000
        if idx >= 0x24:
            seen.append((idx, 0, pos))
            continue  # engine skips unknown controls with no operand
        op = ENGINE_TABLE[idx][0]
        if op == "F0":
            seen.append((idx, 0, pos))
        elif op == "F1":
            if pos + 1 > n:
                return False, seen, f"fam {idx:#x} operand truncated"
            seen.append((idx, 1, pos))
            pos += 1
        elif op == "F4":
            if pos + 4 > n:
                return False, seen, f"fam {idx:#x} operand truncated"
            seen.append((idx, 4, pos))
            pos += 4
        else:  # Z
            z = seg.find(b"\x00", pos)
            if z == -1:
                return False, seen, f"fam {idx:#x} unterminated Z operand"
            seen.append((idx, z + 1 - pos, pos))
            pos = z + 1


def finalize(catalog: Path = None, outdir: Path = None):
    """Verify the ELF table against the whole corpus and emit
    control_sizes_full.json."""
    catalog = catalog or CATALOG
    outdir = outdir or OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    logf = open(outdir / "solver_run.log", "a", encoding="utf-8")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")
        logf.flush()

    log("\n" + "=" * 70)
    log("=== FINALIZE: engine(ELF)-exact verification over the corpus ===")
    t0 = time.time()
    segments, seg_mult, total_msgs = load_corpus(catalog)
    log(f"corpus: {total_msgs} messages over unique container files, "
        f"{len(segments)} unique segments")

    ok = bad = 0
    bad_examples = []
    census = Counter()          # family -> occurrences (unique-file messages)
    census_msgs = Counter()
    float_stats = defaultdict(lambda: [0, 0, Counter()])
    zlen_stats = defaultdict(Counter)
    f1_vals = defaultdict(Counter)
    unknown_idx = Counter()
    for seg in seg_mult:
        mult = seg_mult[seg]
        okk, seen, err = engine_tokenize(seg)
        if okk:
            ok += mult
        else:
            bad += mult
            if len(bad_examples) < 10:
                bad_examples.append((err, seg.hex()[:220]))
        fams = set()
        for idx, oplen, oppos in seen:
            census[idx] += mult
            fams.add(idx)
            if idx >= 0x24:
                unknown_idx[idx] += mult
                continue
            op = ENGINE_TABLE[idx][0]
            if op == "F4" and oppos + 4 <= len(seg):
                val = struct.unpack_from("<f", seg, oppos)[0]
                st = float_stats[idx]
                st[1] += 1
                if val == 0.0 or 0.001 <= abs(val) <= 1000.0:
                    st[0] += 1
                    st[2][round(val, 4)] += 1
            elif op == "Z":
                zlen_stats[idx][oplen] += 1
            elif op == "F1":
                f1_vals[idx][seg[oppos]] += 1
        for f in fams:
            census_msgs[f] += mult
    log(f"engine-exact verification: {ok}/{ok+bad} messages tokenize cleanly"
        f" ({bad} fail)")
    for err, ex in bad_examples:
        log(f"  FAIL {err}: {ex}")
    if unknown_idx:
        log(f"controls with index >= 0x24 seen (engine skips them): "
            f"{ {hex(k): v for k, v in unknown_idx.items()} }")

    log("\nfamily census (occurrences / messages, unique-file corpus):")
    for idx in sorted(census):
        pair = f"{0x80 + (idx & 0x7f):02x}80" if idx < 0x24 else f"idx_{idx:x}"
        log(f"  {pair} fam {idx:#04x}: {census[idx]:>7} occ in "
            f"{census_msgs[idx]:>6} msgs")
    log("\nfloat32 plausibility of F4 operands:")
    for idx, (good, tot, vals) in sorted(float_stats.items()):
        log(f"  {0x80+idx:02x}80: {good}/{tot} plausible; top {vals.most_common(5)}")
    log("\nZ operand length distribution (incl. NUL):")
    for idx, c in sorted(zlen_stats.items()):
        log(f"  {0x80+idx:02x}80: {dict(sorted(c.items())[:12])}")

    # --------- build control_sizes_full.json
    result = {}
    for idx in range(0x24):
        op, meaning, anchor = ENGINE_TABLE[idx]
        hyp = OP_TO_HYP[op]
        pair = f"{0x80+idx:02x}80"
        solver_state = ("unique" if SOLVER_LOCKED.get(idx) == hyp else
                        "compatible" if (idx in SOLVER_AMBIGUOUS and
                                         (SOLVER_AMBIGUOUS[idx] is None or
                                          hyp in SOLVER_AMBIGUOUS[idx])) else
                        "unobserved" if census.get(idx, 0) == 0 else
                        "compatible-by-verification")
        confidence = ("elf+solver" if solver_state in ("unique", "compatible",
                                                       "compatible-by-verification")
                      and census.get(idx, 0) else "elf_only")
        evidence = [
            "elf: dispatch 0x00461DD0 (jump table 0x005077A0), "
            "0x004627F0 (0x00507830), 0x004C8A20 (0x00508DB0) all agree",
        ]
        if anchor:
            evidence.append(f"elf handler: {anchor}")
        if idx in SOLVER_LOCKED:
            evidence.append("solver: uniquely determined from corpus")
        elif idx in SOLVER_AMBIGUOUS:
            cands = SOLVER_AMBIGUOUS[idx]
            evidence.append("solver: candidate set "
                            f"{cands if cands else '(>6 candidates)'} contains the ELF value")
        if census.get(idx, 0):
            evidence.append(f"corpus: {census[idx]} occurrences in "
                            f"{census_msgs[idx]} unique-file messages verify cleanly")
        else:
            evidence.append("corpus: never observed on disc 1")
        if idx in float_stats:
            g, t, vals = float_stats[idx]
            evidence.append(f"float32 operand plausibility {g}/{t}, top "
                            f"{vals.most_common(3)}")
        if idx in PATCHER_PRIORS:
            prior = PATCHER_PRIORS[idx]
            if idx == 0x1C:
                evidence.append("prior CONTROL_SIZES total 5 was a special case "
                                "of zero-termination (2-byte payload + NUL); "
                                "general rule corrected to zero_terminated")
            elif prior == "zero_terminated":
                evidence.append("prior: patcher agrees (zero-terminated)")
            else:
                evidence.append(f"prior: shipped patcher total size {prior} agrees")
        entry = {
            "family": idx,
            "kind": "zero_terminated" if op == "Z" else "fixed",
            "operand_bytes": (None if op == "Z" else int(op[1])),
            "total_size": (None if op == "Z" else 2 + int(op[1])),
            "meaning": meaning,
            "observed_occurrences_unique_files": census.get(idx, 0),
            "observed_messages_unique_files": census_msgs.get(idx, 0),
            "evidence": evidence,
            "confidence": confidence,
        }
        if op == "Z":
            entry["note"] = ("operand runs to and includes the first 0x00; "
                             "that NUL does NOT terminate the message")
        result[pair] = entry
    meta = {
        "_meta": {
            "grammar": {
                "terminator": "0x00 at token boundary ends the message",
                "glyph": "b<0x80: 1-byte code; b0>=0x80,b1<0x80: code=(b0&0x7F)|(b1<<7)",
                "control": "b0>=0x80,b1>=0x80: index=((b0&0x7F)|(b1<<7))-0x4000; "
                           "index<0x24 dispatches, otherwise the pair is skipped "
                           "with no operand (engine bounds check)",
                "on_disc": "all real controls use b1==0x80 (family=b0&0x7F)",
            },
            "elf_module": {
                "source": "raw/0002.sle member_01 (decrypted, "
                          "work/executable_mapping/decoded/member_01_off_00011DC4_va_003E7C80.bin)",
                "sha256": "E10283983CB167AE0E430320360401A5971CACEE7154BD512A9A967206114271",
                "load_address": "0x003E7C80",
                "dispatch_sites": {
                    "tokenizer": {"code": "0x00461DD0", "table": "0x005077A0"},
                    "measuring_scan": {"code": "0x004627F0", "table": "0x00507830"},
                    "direct_renderer": {"code": "0x004C8A20", "table": "0x00508DB0"},
                },
            },
            "verification": {
                "messages_total_unique_files": total_msgs,
                "messages_clean": ok,
                "messages_failed": bad,
                "unique_segments": len(segments),
            },
        }
    }
    meta.update(result)
    (outdir / "control_sizes_full.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\nwrote control_sizes_full.json; finalize took {time.time()-t0:.1f}s")
    return ok, bad


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="SO3 control-opcode solver (defaults = disc 1 corpus)")
    ap.add_argument("--finalize", action="store_true",
                    help="only run the ELF-exact verification + emit control_sizes_full.json")
    ap.add_argument("--catalog", type=Path, default=CATALOG,
                    help="container_catalog.csv defining the corpus "
                         "(disc-2: work\\full_ko_d2\\container_catalog.csv)")
    ap.add_argument("--out-dir", type=Path, default=OUTDIR,
                    help="output dir for solver_run.log / solver_candidates.json / "
                         "control_sizes_full.json (disc-2: work\\full_ko_d2)")
    args = ap.parse_args()
    if args.finalize:
        finalize(args.catalog, args.out_dir)
    else:
        main(args.catalog, args.out_dir)
        finalize(args.catalog, args.out_dir)

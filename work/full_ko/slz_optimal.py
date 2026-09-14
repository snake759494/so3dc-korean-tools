#!/usr/bin/env python3
"""Optimal-parse compressor for the tri-Ace SLZ mode-2 payload (Star Ocean 3 PS2).

Drop-in, ratio-improved replacement for so3_repack.compress_slz_mode2 (greedy).

Token grammar (derived from decompress_slz_payload, the ground-truth decoder)
-----------------------------------------------------------------------------
A flag byte is emitted before every group of 8 tokens; bit k (LSB first)
describes the k-th token of the group (1 = literal, 0 = match/RLE).

  literal    flag=1  1 payload byte    raw byte
  match      flag=0  2 payload bytes   (dist & 0xFF, ((len-3)<<4) | (dist>>8))
                                       len in 3..17, dist in 1..0xFFF, dist <= pos.
                                       Overlap (dist < len) is legal: the decoder
                                       copies byte-by-byte via out.append(out[-dist]).
  RLE short  flag=0  2 payload bytes   (byte, 0xF0 | (run-3))     run in 4..18
  RLE long   flag=0  3 payload bytes   (run-0x13, 0xF0, byte)     run in 19..274

Decoder facts that bound the token set (mode 2):
  * A match length nibble of 0xF is impossible: any second byte >= 0xF0 is
    consumed by the RLE escape, so the max match length is 0xE + 3 = 17
    (modes 1/3 can express 18; mode 2 cannot).
  * RLE short cannot express run == 3: 0xF0 | (3-3) == 0xF0 is the long-form
    escape.  A 3-byte run needs a match (e.g. dist 1 if the previous byte
    equals the run byte) or literals.
  * The decoder clamps match/RLE expansion at output_size
    (min(count, output_size - len(out))), so an over-long token at the very
    end of the stream would still round-trip through the Python decoder.
    This encoder deliberately does NOT exploit that (the gain would be at
    most ~1 byte per file and the real PS2 decompressor's tolerance for it
    is unverified).

Cost model and optimality
-------------------------
Cost is counted in bits: every token costs 1 flag bit plus 8 bits per payload
byte (literal 9, match 17, RLE short 17, RLE long 25).  The final stream size
in bytes is ceil(T/8) + P where T = token count and P = payload bytes, while
the bit sum is B = T + 8P.  For a bit-minimal parse A and any parse X:

    bytes(A) <= (B_A + 7)/8 < B_A/8 + 1 <= B_X/8 + 1 <= bytes(X) + 1

and since sizes are integers, bytes(A) <= bytes(X).  So minimizing the bit
sum minimizes the byte size exactly -- flag-byte rounding cannot flip the
result.

The dynamic program walks right-to-left: cost[i] = min bits to encode
data[i:].  Because a match costs the same 17 bits regardless of length and
distance, only the *maximum* feasible match length per position is needed
(any shorter length at the same distance is feasible at equal cost):

  literal                cost[i+1] + 9
  match  L in 3..M[i]    min(cost[i+3..i+M[i]]) + 17
  RLE-S  L in 4..r       min(cost[i+4..i+min(r,18)]) + 17     (r = run length)
  RLE-L  L in 19..r      min(cost[i+19..i+min(r,274)]) + 25

The match and RLE-short windows cost the same and are contiguous, so they are
scanned as one window.  The RLE-long window is up to 256 wide, so inside each
maximal byte-run it is evaluated with an amortized-O(1) monotonic-deque
sliding-window minimum (crucial for zero-heavy glyph bitmaps).

Match finding (exact, numpy): for each L = 3..17 ascending, positions are
stably sorted by their packed L-gram key; within a group of equal grams the
stable sort leaves positions ascending, so the sorted predecessor is the
nearest previous occurrence, giving dist(i) = min distance to an identical
L-gram.  maxlen[i] is the largest L with dist <= 0xFFF.  Two exact prunings
keep the sorts small:

  * candidate shrinking: a pair of equal (L+1)-grams within the window
    implies a pair of equal L-grams within the window, and every consecutive
    link of an occurrence chain between them is itself within the window, so
    only positions that were a member of an in-window pair at level L can
    participate at level L+1;
  * deep run interiors (data[i-1] == data[i] and run[i] >= 17) are assigned
    maxlen 17 / dist 1 directly and excluded from the sorts.  This is
    lossless: an all-equal match from a run interior is reproduced by
    distance 1; a run-crossing match has an equivalent source at the run
    tail (the last <17 positions of a run stay candidates); and an all-equal
    17-match at a run *start* costs exactly the same 17 bits as the always
    available RLE-short token there.

Hence with numpy the parse is truly optimal over the decoder's whole token
set.  Without numpy a hash-chain fallback matcher is used whose chain walk is
capped at `max_chain` candidates per position (near-optimal; pass
max_chain=0 for unlimited/exact).

Greedy-encoder observations (vs. the decoder ground truth): the greedy
encoder's token emission is bitstream-correct; its losses are purely
parse-order ones -- it always takes the longest match / longest run at the
current position (with the "run >= 4 and run >= match_len" RLE preference)
and never shortens a token so that the next token starts at a better
boundary, paying literals where a shortened match would let a later RLE-long
token swallow more bytes.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is present in the build env
    _np = None

_WINDOW = 0xFFF
_MATCH_MAX = 17


# ---------------------------------------------------------------------------
# run lengths
# ---------------------------------------------------------------------------

def _run_lengths_py(data: bytes, n: int) -> list:
    """run[i] = length of the maximal run of data[i] starting at i."""
    run = [1] * n
    nxt = 1
    for i in range(n - 2, -1, -1):
        nxt = nxt + 1 if data[i] == data[i + 1] else 1
        run[i] = nxt
    return run


def _run_lengths_np(data: bytes, n: int):
    a = _np.frombuffer(data, dtype=_np.uint8)
    if n == 1:
        return _np.ones(1, dtype=_np.int64)
    ends = _np.flatnonzero(a[1:] != a[:-1])         # last index of each run but the final
    ends = _np.append(ends, n - 1)
    idx = _np.arange(n, dtype=_np.int64)
    return ends[_np.searchsorted(ends, idx)] - idx + 1


# ---------------------------------------------------------------------------
# match finding
# ---------------------------------------------------------------------------

def _find_matches_np(data: bytes, n: int, run_arr):
    """Exact maxlen[i] (longest match <= 17 within the 0xFFF window) + a distance."""
    a8 = _np.frombuffer(data, dtype=_np.uint8)
    maxlen = _np.zeros(n, dtype=_np.int32)
    mdist = _np.zeros(n, dtype=_np.int32)
    if n < 3:
        return maxlen.tolist(), mdist.tolist()

    interior = _np.empty(n, dtype=bool)
    interior[0] = False
    _np.equal(a8[1:], a8[:-1], out=interior[1:])
    deep = interior & (run_arr >= _MATCH_MAX)       # dist-1 match of length 17, exact
    maxlen[deep] = _MATCH_MAX
    mdist[deep] = 1
    cand = _np.flatnonzero(~deep)

    # ids[k][i] = data[i:i+k] packed big-endian into a uint64 (exact key)
    a = a8.astype(_np.uint64)
    ids = [None] * 9
    ids[1] = a
    c256 = _np.uint64(256)
    for k in range(2, 9):
        ids[k] = ids[k - 1][: n - k + 1] * c256 + a[k - 1:]

    for L in range(3, _MATCH_MAX + 1):
        m = n - L + 1                               # number of L-grams
        cand = cand[cand < m]
        if cand.size < 2:
            break
        if L <= 8:
            k1 = ids[L][cand]
            order = _np.argsort(k1, kind="stable")
            s1 = k1[order]
            same = s1[1:] == s1[:-1]
        elif L <= 16:
            k1 = ids[8][cand]
            k2 = ids[L - 8][cand + 8]
            order = _np.lexsort((k2, k1))           # stable: ties stay position-ascending
            s1 = k1[order]
            s2 = k2[order]
            same = (s1[1:] == s1[:-1]) & (s2[1:] == s2[:-1])
        else:                                       # L == 17
            k1 = ids[8][cand]
            k2 = ids[8][cand + 8]
            k3 = a[cand + 16]
            order = _np.lexsort((k3, k2, k1))
            s1 = k1[order]
            s2 = k2[order]
            s3 = k3[order]
            same = (s1[1:] == s1[:-1]) & (s2[1:] == s2[:-1]) & (s3[1:] == s3[:-1])
        p = cand[order]
        d = p[1:] - p[:-1]                          # >0 inside a group (stable ties)
        ok = same & (d <= _WINDOW)
        tgt = p[1:][ok]
        maxlen[tgt] = L
        mdist[tgt] = d[ok]
        keep = _np.zeros(p.size, dtype=bool)        # in-window pair members survive
        keep[1:] = ok
        keep[:-1] |= ok
        cand = _np.sort(p[keep])
    return maxlen.tolist(), mdist.tolist()


def _find_matches_chains(data: bytes, n: int, run: list, max_chain: int):
    """Hash-chain fallback matcher (no numpy).  Near-optimal when capped.

    Uses the LZ carry property (a match of length L at i-1 implies one of
    length L-1 at i at the same distance) to start each search with a high
    lower bound, so most chain candidates die on a single byte compare.
    """
    maxlen = [0] * n
    mdist = [0] * n
    if n < 3:
        return maxlen, mdist
    head = {}
    prev = [0] * n
    hget = head.get
    unlimited = max_chain <= 0
    carry_len = 0                                   # feasible match at current i,
    carry_dist = 0                                  # carried from the previous position
    for i in range(n - 2):
        r = run[i]
        if i and run[i - 1] > r:                    # run interior (data[i-1] == data[i])
            if r >= _MATCH_MAX:                     # deep interior: dist-1, skip chains
                maxlen[i] = _MATCH_MAX
                mdist[i] = 1
                carry_len = _MATCH_MAX - 1
                carry_dist = 1
                continue
            if r >= 3 and r > carry_len:            # dist-1 baseline
                carry_len = r
                carry_dist = 1
        if carry_len >= 3:
            best = carry_len
            bd = carry_dist
        else:
            best = 2
            bd = 0
        key = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
        j = hget(key, -1)
        prev[i] = j
        head[key] = i
        ml = n - i
        if ml > _MATCH_MAX:
            ml = _MATCH_MAX
        if j >= 0 and best < ml:
            lim = i - _WINDOW
            if lim < 0:
                lim = 0
            chain = max_chain
            while j >= lim:
                # cheap reject, then slice (memcmp) verify: only candidates
                # that BEAT the current best survive both.
                if data[j + best] == data[i + best] and data[j:j + best] == data[i:i + best]:
                    l = best + 1
                    while l < ml and data[j + l] == data[i + l]:
                        l += 1
                    best = l
                    bd = i - j
                    if l >= ml:
                        break
                if not unlimited:
                    chain -= 1
                    if not chain:
                        break
                j = prev[j]
        if best >= 3:
            maxlen[i] = best
            mdist[i] = bd
            carry_len = best - 1
            carry_dist = bd
        else:
            carry_len = 0
    return maxlen, mdist


# ---------------------------------------------------------------------------
# compressor
# ---------------------------------------------------------------------------

def compress_slz_mode2_optimal(data: bytes, *, max_chain: int = 128,
                               engine: str = "auto") -> bytes:
    """Bit-optimal (hence byte-optimal) parse for SLZ mode 2.

    Round-trips through decompress_slz_payload(payload, 2, len(data)).
    engine: "auto" (numpy if available), "numpy", or "chains" (pure Python;
    max_chain caps its match search, 0 = unlimited/exact).
    """
    data = bytes(data)
    n = len(data)
    if n == 0:
        return b""

    use_np = _np is not None and engine != "chains"
    if engine == "numpy" and _np is None:
        raise RuntimeError("numpy engine requested but numpy is unavailable")
    if use_np:
        run_arr = _run_lengths_np(data, n)
        maxlen, mdist = _find_matches_np(data, n, run_arr)
        run = run_arr.tolist()
    else:
        run = _run_lengths_py(data, n)
        maxlen, mdist = _find_matches_chains(data, n, run, max_chain)

    # ---- backward DP over bit costs ----
    cost = [0] * (n + 1)
    kind = bytearray(n)          # 0 literal, 1 match, 2 RLE short, 3 RLE long
    tlen = [1] * n
    dq = deque()
    dq_e = -1
    dq_append, dq_pop, dq_popleft = dq.append, dq.pop, dq.popleft
    for i in range(n - 1, -1, -1):
        best = cost[i + 1] + 9
        k = 0
        L = 1
        m = maxlen[i]
        r = run[i]
        # match (len 3..m) and RLE short (len 4..min(r,18)) both cost 17 bits;
        # their length windows are contiguous, so scan them as one window.
        rr = (18 if r > 18 else r) if r >= 4 else 0
        hi = m if m >= rr else rr
        if hi:
            lo = 3 if m else 4
            seg = cost[i + lo:i + hi + 1]
            c = min(seg) + 17
            if c < best:
                best = c
                L = seg.index(c - 17) + lo
                k = 1 if L <= m else 2
        if r >= 19:
            e = i + r
            if e != dq_e:                           # entering a new maximal run
                dq.clear()
                dq_e = e
            j = i + 19                              # exactly one index enters per step
            cj = cost[j]
            while dq and cost[dq[-1]] >= cj:
                dq_pop()
            dq_append(j)
            hi = i + 274
            if hi > e:
                hi = e
            while dq[0] > hi:
                dq_popleft()
            c = cost[dq[0]] + 25
            if c < best:
                best = c
                k = 3
                L = dq[0] - i
        cost[i] = best
        kind[i] = k
        tlen[i] = L

    # ---- emit ----
    out = bytearray()
    i = 0
    bit = 8
    flag_pos = 0
    while i < n:
        if bit == 8:
            flag_pos = len(out)
            out.append(0)
            bit = 0
        k = kind[i]
        L = tlen[i]
        if k == 0:
            out[flag_pos] |= 1 << bit
            out.append(data[i])
        elif k == 1:
            d = mdist[i]
            out.append(d & 0xFF)
            out.append(((L - 3) << 4) | (d >> 8))
        elif k == 2:
            out.append(data[i])
            out.append(0xF0 | (L - 3))
        else:
            out.append(L - 0x13)
            out.append(0xF0)
            out.append(data[i])
        bit += 1
        i += L
    return bytes(out)


# ---------------------------------------------------------------------------
# self-test / benchmark
# ---------------------------------------------------------------------------

def _load_repack():
    here = Path(__file__).resolve()
    for cand in (
        here.parents[2] / "publish" / "so3dc-korean-tools",
        Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\publish\so3dc-korean-tools"),
    ):
        if (cand / "so3_repack.py").exists():
            sys.path.insert(0, str(cand))
            import so3_repack
            return so3_repack
    raise FileNotFoundError("so3_repack.py not found")


def _synthetic_cases():
    import random
    rng = random.Random(0x503C)
    cases = [
        ("empty", b""),
        ("one", b"A"),
        ("two", b"AB"),
        ("run3", b"\x00" * 3),
        ("run4", b"\x00" * 4),
        ("run18", b"\x07" * 18),
        ("run19", b"\x07" * 19),
        ("run274", b"\xff" * 274),
        ("run275", b"\xff" * 275),
        ("run10k", b"\x00" * 10000),
        ("mixed_runs", b"".join(bytes([i & 0xFF]) * (i % 300 + 1) for i in range(64))),
        ("period2", b"AB" * 5000),
        ("period3", b"xyz" * 4000),
        ("text", (b"All work and no play makes Jack a dull boy. " * 400)),
        ("random_1k", rng.randbytes(1024)),
        ("random_64k", rng.randbytes(65536)),
        ("almost_rand", bytes(rng.randrange(0, 4) for _ in range(20000))),
        ("run_then_text", b"\x00" * 5000 + b"Star Ocean 3" * 100 + b"\x00" * 5000),
        ("tail_pair", b"Q" * 100 + b"ZZ"),
        ("window_edge", (b"MAGICSTR" + bytes(rng.randbytes(4087))) * 3),
    ]
    return cases


def _pick_real_files(count: int) -> list:
    import csv
    catalog = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\work\mclib_all_decode\container_catalog.csv")
    seen = set()
    rows = []
    with catalog.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sha = row["file_sha256"]
            if sha in seen:
                continue
            seen.add(sha)
            p = Path(row["path"])
            if p.exists():
                rows.append((int(row["actual_file_size"]), p))
    rows.sort()
    if len(rows) <= count:
        return rows
    step = (len(rows) - 1) / (count - 1)
    picked = []
    used = set()
    for k in range(count):
        idx = round(k * step)
        while idx in used:
            idx += 1
        used.add(idx)
        picked.append(rows[idx])
    return picked


def _main() -> int:
    import json
    import time
    repack = _load_repack()
    dec = repack.decompress_slz_payload
    greedy = repack.compress_slz_mode2

    print("== synthetic round-trips ==")
    synth_results = []
    for name, blob in _synthetic_cases():
        comp = compress_slz_mode2_optimal(blob)
        back = dec(comp, 2, len(blob))
        assert back == blob, f"round-trip FAILED: {name}"
        comp_ch = compress_slz_mode2_optimal(blob, engine="chains")
        assert dec(comp_ch, 2, len(blob)) == blob, f"chains round-trip FAILED: {name}"
        g = len(greedy(blob))
        o = len(comp)
        print(f"  {name:<14} raw={len(blob):>7}  greedy={g:>7}  optimal={o:>7}  chains={len(comp_ch):>7}")
        assert o <= g, f"optimal larger than greedy on {name}: {o} > {g}"
        assert o <= len(comp_ch), f"numpy parse worse than chains on {name}"
        synth_results.append({"name": name, "raw": len(blob), "greedy": g,
                              "optimal": o, "chains_fallback": len(comp_ch)})
    print("  all synthetic round-trips OK")

    print("\n== real mclib payloads ==")
    files = _pick_real_files(36)
    tot_raw = tot_g = tot_o = 0
    t_opt_total = 0.0
    worst = None
    chain_checked = 0
    real_results = []
    print(f"  {'file':<42}{'raw':>9}{'greedy':>9}{'optimal':>9}{'gain%':>7}{'sec':>7}")
    for size, path in files:
        blob = path.read_bytes()
        t0 = time.perf_counter()
        comp = compress_slz_mode2_optimal(blob)
        t1 = time.perf_counter()
        back = dec(comp, 2, len(blob))
        assert back == blob, f"round-trip FAILED: {path}"
        gblob = greedy(blob)
        assert dec(gblob, 2, len(blob)) == blob, f"greedy round-trip FAILED: {path}"
        if len(blob) <= 40000 and chain_checked < 25:   # exercise the fallback engine too
            comp_ch = compress_slz_mode2_optimal(blob, engine="chains")
            assert dec(comp_ch, 2, len(blob)) == blob, f"chains round-trip FAILED: {path}"
            chain_checked += 1
        g, o = len(gblob), len(comp)
        gain = (g - o) / g * 100.0 if g else 0.0
        tot_raw += len(blob)
        tot_g += g
        tot_o += o
        t_opt_total += t1 - t0
        if worst is None or gain < worst[0]:
            worst = (gain, path.name)
        print(f"  {path.name:<42}{len(blob):>9}{g:>9}{o:>9}{gain:>7.2f}{t1 - t0:>7.2f}")
        assert o <= g, f"optimal larger than greedy on {path}"
        real_results.append({"file": path.name, "path": str(path), "raw": len(blob),
                             "greedy": g, "optimal": o, "gain_pct": round(gain, 4),
                             "seconds": round(t1 - t0, 3)})
    agg = (tot_g - tot_o) / tot_g * 100.0
    print(f"  {'TOTAL':<42}{tot_raw:>9}{tot_g:>9}{tot_o:>9}{agg:>7.2f}{t_opt_total:>7.2f}")
    print(f"  aggregate gain {agg:.2f}%  worst per-file gain {worst[0]:.2f}% ({worst[1]})")
    print(f"  fallback-engine round-trips checked on {chain_checked} real files")
    print(f"  throughput {tot_raw / t_opt_total / 1024:.0f} KiB/s")

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "numpy" if _np is not None else "chains",
        "real_files": real_results,
        "aggregate": {
            "files": len(real_results),
            "raw_bytes": tot_raw,
            "greedy_bytes": tot_g,
            "optimal_bytes": tot_o,
            "gain_pct": round(agg, 4),
            "worst_gain_pct": round(worst[0], 4),
            "worst_gain_file": worst[1],
            "optimal_seconds_total": round(t_opt_total, 3),
            "throughput_kib_per_s": round(tot_raw / t_opt_total / 1024, 1),
        },
        "synthetic": synth_results,
        "all_roundtrips_passed": True,
    }
    out_json = Path(__file__).resolve().parent / "slz_benchmark.json"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"  benchmark written to {out_json}")
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

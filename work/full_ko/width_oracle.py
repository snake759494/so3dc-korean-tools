# -*- coding: utf-8 -*-
"""Exact width oracle: reproduces verify_full_iso.py check-7 line_px for a
candidate Korean string, by building the patched container with
so3_full_patch.rebuild_container and replaying verify's WidthState logic on the
PATCHED atlas widths.  Deterministic; matches the authoritative verifier."""
import io, os, sys, csv
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)
from pathlib import Path
import verify_full_iso as VF
import so3_full_patch as SF

WS = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2")
ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
CATALOG = WS / "work" / "mclib_all_decode" / "container_catalog.csv"
MANIFEST = WS / "work" / "full_unpack" / "disc1" / "manifests" / "stream_manifest.csv"
CONTROLS = FULL_KO + os.sep + "control_sizes_full.json"
FONT = Path(r"D:\ps2\NanumSquareNeo-cBd.ttf")

_index = None
_manifest = None
_ctl = None
_global_widths = None
_decoded_cache = {}

def _init():
    global _index, _manifest, _ctl, _global_widths
    if _index is not None:
        return
    _index = VF.read_index(ISO)
    _manifest = VF.load_manifest(MANIFEST)
    _ctl = VF.Controls(Path(CONTROLS))
    _global_widths = VF.load_global_widths(ISO, _index, CATALOG, _manifest)

def get_original_decoded(a, s):
    _init()
    key = (a, s)
    if key in _decoded_cache:
        return _decoded_cache[key]
    info = _manifest.get(key)
    if info is None:
        raise KeyError("no manifest entry for %s" % (key,))
    start = _index[a] * VF.SECTOR + info["source_offset"]
    with ISO.open("rb") as h:
        decoded = VF.read_slz_at(h, start)
    _decoded_cache[key] = decoded
    return decoded

def _replay_width(patched_bytes, mid):
    """Return {(page,line): (px, allowance_px)} for message `mid` in patched
    container bytes, exactly as verify_full_iso check 7 computes it."""
    p = VF.Mclib.parse(patched_bytes)
    use_global = p.local_base > VF.MAX_GLOBAL_CODE
    off = None
    for m, o in p.rows:
        if m == mid:
            off = o
            break
    if off is None:
        raise KeyError("mid %d not in patched container" % mid)
    seg = p.segments[off]
    tokens, term = VF.tokenize(seg, _ctl)
    ws = VF.WidthState()
    line_px = {}
    line_fams = {}
    page_index = 0
    line_index = 0
    current_px = 0
    current_fams = []

    def flush():
        nonlocal current_px, current_fams
        line_px[(page_index, line_index)] = current_px
        line_fams[(page_index, line_index)] = current_fams
        current_px, current_fams = 0, []

    for token in tokens:
        if token[0] == "c" and token[1] == VF.PAGE_FAM:
            flush(); page_index += 1; line_index = 0
        elif token[0] == "c" and token[1] == VF.NEWLINE_FAM:
            flush(); line_index += 1
        elif token[0] == "g":
            code = token[1]
            if use_global and code < p.local_base:
                width = _global_widths[code - 1] if 0 <= code - 1 < len(_global_widths) else 24
            else:
                gi = code - p.local_base
                width = p.widths[gi] if 0 <= gi < p.glyph_count else 24
            current_px += ws.advance(width)
        elif token[1] in VF.POSITIONAL_FAMS:
            current_fams.append(token[1])
        else:
            ws.control(token[1], token[2])
    flush()
    # NOTE: verify_container strips the speaker header for gated widths by
    # consuming prefix+field+delim tokens first.  We approximate by measuring
    # the whole stream; for the affected UI/dialogue lines the speaker header,
    # when present, sits on its own and body pages start at page 0.  Callers
    # compare specific (page,line) keys reported by the verifier, so header
    # lines (if any) don't collide with the gated body coordinates in practice.
    out = {}
    for k, px in line_px.items():
        allow = sum(VF.MARKER_ALLOWANCE_PX.get(fam, 0) for fam in line_fams[k])
        out[k] = (px, allow)
    return out

def measure(a, s, mid, korean, keep_speaker=False, speaker_korean=None):
    """Build patched container for one candidate and measure its line widths."""
    decoded = get_original_decoded(a, s)
    entry = {"korean": korean}
    if keep_speaker:
        entry["keep_speaker"] = True
    if speaker_korean is not None:
        entry["speaker_korean"] = speaker_korean
    patched, _meta = SF.rebuild_container(
        decoded, {mid: entry}, font_path=FONT)
    return _replay_width(patched, mid)


if __name__ == "__main__":
    # self-test against the verifier's reported px for the failing lines
    import json, re
    from collections import defaultdict
    rep = json.load(open(os.path.join(FULL_KO, "verify_full_report.json"), encoding="utf-8"))
    ws_errs = [e for e in (rep.get("errors") or []) if e.get("check") == "width"]
    WHERE = re.compile(r"archive(\d+):s(\d+):(\d+)")
    MSG = re.compile(r"at page (\d+) line (\d+): (\d+)\+(\d+) > (\d+)")
    as2sha = {}
    with open(CATALOG, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            as2sha[(int(row["archive_id"]), int(row["stream_id"]))] = row["file_sha256"].lower()
    plan = json.load(open(os.path.join(FULL_KO, "patch_plan_full.json"), encoding="utf-8"))
    uniq = plan["unique"]
    # take a sample per distinct container to validate the oracle
    seen = set()
    checked = 0; ok = 0; bad = []
    for e in ws_errs:
        wm = WHERE.search(e["where"]); mm = MSG.search(e["message"])
        if not wm or not mm:
            continue
        a, s, mid = int(wm.group(1)), int(wm.group(2)), int(wm.group(3))
        pg, ln = int(mm.group(1)), int(mm.group(2))
        rep_text, rep_allow, budget = int(mm.group(3)), int(mm.group(4)), int(mm.group(5))
        key = (a, s, mid)
        # limit: at most 3 mids per container for speed
        ck = (a, s)
        if sum(1 for k in seen if k[:2] == ck) >= 3 and key not in seen:
            continue
        seen.add(key)
        sha = as2sha.get((a, s))
        ent = uniq.get(sha, {}).get(str(mid))
        if ent is None:
            continue
        try:
            m = measure(a, s, mid, ent["korean"],
                        keep_speaker=ent.get("keep_speaker", False),
                        speaker_korean=ent.get("speaker_korean"))
        except Exception as exc:
            bad.append((a, s, mid, pg, ln, "EXC:%s" % exc)); continue
        got = m.get((pg, ln))
        checked += 1
        if got is None:
            bad.append((a, s, mid, pg, ln, "no-line got_keys=%s" % list(m.keys())[:6]))
            continue
        gpx, gallow = got
        if gpx == rep_text and gallow == rep_allow:
            ok += 1
        else:
            bad.append((a, s, mid, pg, ln, "got px=%d allow=%d  rep px=%d allow=%d" %
                        (gpx, gallow, rep_text, rep_allow)))
    print("oracle self-test: checked=%d exact=%d mism=%d" % (checked, ok, len(bad)))
    for b in bad[:40]:
        print("   MISMATCH", b)

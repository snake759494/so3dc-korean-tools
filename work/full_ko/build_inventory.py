# -*- coding: utf-8 -*-
"""build_inventory.py — SO3 DC full-KO translation inventory builder (INVENTORY_SPEC.md v1).

Inputs (WS = C:\\Users\\Jay\\Documents\\Codex\\2026-07-13\\d-3-ps2):
  - work\\mclib_all_decode\\container_catalog.csv        (dedupe by file_sha256, first occurrence wins)
  - work\\font_ocr\\glyph_mapping_ordered_24.json        (bitmap sha256 -> unicode)
  - work\\full_ko\\decode_mclib_text.py                  (global 292-slot table G)
  - work\\full_ko\\control_sizes_full.json               (authoritative control table; provisional fallback)
  - work\\dialogue_locator\\spoken_dialogue_index.csv    (speaker cross-check)
  - work\\event_text_classifier\\event_bank_catalog.csv  (JP event banks)
  - work\\full_unpack\\disc1\\manifests\\stream_manifest.csv (fit data + paired .bin detection)
  - work\\mclib_all_decode\\unique_exact_segments.csv    (exact_sha256 validation)

Outputs (work\\full_ko\\):
  - inventory_containers.json   per in-scope unique container: summary + all messages (lightweight)
  - translation_units.jsonl     one line per unique (speaker,body) text with Japanese in the body
  - inventory_stats.json        aggregates, validation results, per-container fit data

Message-offset convention: mapping-table offsets are TEXT-BLOB-RELATIVE (blob = data[u32(0x14):
width_start if glyph_count else file_size]); exact_sha256 = sha256 over the raw segment bytes
INCLUDING trailing NUL padding up to the next sorted offset boundary (mclib_all_decode convention;
verified 13,184/13,184 on an 8-container probe).

Control semantics (per control_sizes_full.json + ELF dispatch analysis):
  8080 newline '\\n' | 8180 page break -> '\\n⟦P⟧' (line boundary, pages_count+=1)
  numbered positional markers ⟦1⟧⟦2⟧... in body reading order, family recorded per marker:
      8880 set color, 8980 color reset, 9280 registry string insert, 9c80 zero-terminated drawn
      payload (verbatim-preserve), a180 name via registry slot, a280/a380 item name inserts
  9380 character name by id -> ⟦이름:<한글이름>⟧ (NOT numbered; literalized at re-encode)
  9080/9180 ruby base start / ruby text (Z): controls+ruby reading dropped, has_ruby=true
  8a80 glyph scale float32 (x=y) -> width scale (applied when 0<s<=4); 8b80 reset scale=1.0
  8c80 glyph advance/spacing float32 -> per-glyph additive px (assumption: additive, default 0);
      8d80 reset spacing=0.0
  everything else INVISIBLE (omitted from text; proportionally re-anchored at patch time)
  speaker separator: [8980]? 8780 8080 — tokens before it (single-line only) = speaker field
Engine grammar: control pair = b0>=0x80 & b1>=0x80; only b1==0x80 dispatches (family=b0&0x7F<0x24);
any other pair is skipped with no operand (engine bounds check).
"""
import argparse, csv, hashlib, io, json, os, re, struct, sys, time
from collections import Counter, defaultdict

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WS = os.environ.get(
    "SO3_WS",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)
from decode_mclib_text import G  # global 292-slot transcription table

# Disc-1 defaults (kept as module constants for importers, e.g.
# prepare_translation_batches uses bi.CATALOG).  Disc-2 runs override them via
# CLI args -- never edit these in place.
CATALOG = os.path.join(WS, r"work\mclib_all_decode\container_catalog.csv")
UES_CSV = os.path.join(WS, r"work\mclib_all_decode\unique_exact_segments.csv")
GLYPH24 = os.path.join(WS, r"work\font_ocr\glyph_mapping_ordered_24.json")
CTL_JSON = os.path.join(FULL_KO, "control_sizes_full.json")
SPOKEN_CSV = os.path.join(WS, r"work\dialogue_locator\spoken_dialogue_index.csv")
EBC_CSV = os.path.join(WS, r"work\event_text_classifier\event_bank_catalog.csv")
STREAM_MANIFEST = os.path.join(WS, r"work\full_unpack\disc1\manifests\stream_manifest.csv")
GLYPH_EXTRA_DEFAULT = os.path.join(FULL_KO, "glyph_labels_extra.json")

OUT_CONTAINERS = os.path.join(FULL_KO, "inventory_containers.json")
OUT_UNITS = os.path.join(FULL_KO, "translation_units.jsonl")
OUT_STATS = os.path.join(FULL_KO, "inventory_stats.json")

JP_RE = re.compile("[ぁ-ゟァ-ヿ一-鿿]")  # [ぁ-ゟァ-ヿ一-鿿]

NAME_ID_MAP = {1: "페이트", 2: "소피아", 3: "마리아", 4: "클리프", 5: "넬",
               6: "알벨", 7: "로저", 8: "스프레", 9: "아드레이", 10: "미라쥬"}

# control classification config (family byte b0). Update alongside control_sizes_full.json.
NEWLINE_FAM = 0x80
PAGEBREAK_FAM = 0x81
SPEAKER_FLAG_FAM = 0x87
COLOR_RESET_FAM = 0x89
NAME_REF_FAM = 0x93
RUBY_BASE_FAM = 0x90
RUBY_TEXT_FAM = 0x91
SCALE_FAM, SCALE_RESET_FAM = 0x8A, 0x8B
ADVANCE_FAM, ADVANCE_RESET_FAM = 0x8C, 0x8D
NUMBERED_MARKER_FAMS = {0x88, 0x89, 0x92, 0x9C, 0xA1, 0xA2, 0xA3}
DYNAMIC_FAMS = {0x92, 0x9C, 0xA1, 0xA2, 0xA3}  # runtime-substituted content

# provisional fallback (solver-locked 17 + Hyda priors) — used only when CTL_JSON is absent
PROVISIONAL = {
    0x80: ("F", 0), 0x81: ("F", 0), 0x83: ("F", 0), 0x84: ("F", 0), 0x85: ("F", 4),
    0x86: ("F", 4), 0x87: ("F", 0), 0x89: ("F", 0), 0x8B: ("F", 0), 0x8D: ("F", 0),
    0x91: ("Z",), 0x94: ("F", 4), 0x95: ("F", 4), 0x98: ("F", 0), 0x99: ("F", 0),
    0x9E: ("F", 4), 0x9F: ("F", 0),
    # Hyda priors
    0x82: ("F", 1), 0x88: ("F", 1), 0x8A: ("F", 4), 0x93: ("F", 1), 0x9C: ("F", 3),
    0x90: ("Z",),
}


def load_control_table(path=None):
    path = path or CTL_JSON
    if os.path.exists(path):
        raw = json.load(open(path, encoding="utf-8"))
        table = {}
        for key, spec in raw.items():
            if key.startswith("_"):
                continue
            fam = int(key[:2], 16)
            if spec["kind"] == "zero_terminated":
                table[fam] = ("Z",)
            else:
                table[fam] = ("F", int(spec["operand_bytes"]))
        return table, os.path.basename(path), raw.get("_meta", {})
    return dict(PROVISIONAL), "provisional (solver 17 + Hyda priors)", {}


def load_glyph_map(path):
    d = json.load(open(path, encoding="utf-8"))
    m = {}
    for g in d["glyphs"]:
        u = g.get("unicode")
        if u:
            m[g["bitmap_sha256"]] = u
    return m


def parse_mclib(data):
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    hdr = {
        "table_start": u32(0x10), "text_start": u32(0x14), "width_start": u32(0x18),
        "bitmap_start": u32(0x1C), "glyph_count": u32(0x20),
        "glyph_width": u32(0x2C), "glyph_height": u32(0x30), "glyph_stride": u32(0x34),
        "local_base": u32(0x38), "mapping_count": u32(0x3C), "file_size": u32(0x40),
    }
    rows = sorted({struct.unpack_from("<II", data, hdr["table_start"] + i * 8)
                   for i in range(hdr["mapping_count"])})
    text_end = hdr["width_start"] if hdr["glyph_count"] else hdr["file_size"]
    blob = data[hdr["text_start"]:text_end]
    offs = sorted({o for _, o in rows})
    nxt = {o: (offs[j + 1] if j + 1 < len(offs) else len(blob)) for j, o in enumerate(offs)}
    segs = [(mid, off, blob[off:nxt[off]]) for mid, off in rows]
    widths = data[hdr["width_start"]:hdr["width_start"] + hdr["glyph_count"]] if hdr["glyph_count"] else b""
    return hdr, segs, widths


def build_local_map(data, hdr, m24):
    """local glyph code -> unicode char.
    Unmapped glyphs become preserve-bitmap tokens ⟦G:sha8⟧ (patcher copies the
    original bitmap into the rebuilt atlas and emits its code at that position)."""
    gw, gh, n = hdr["glyph_width"], hdr["glyph_height"], hdr["glyph_count"]
    bpg = gw * gh // 2
    base, start = hdr["local_base"], hdr["bitmap_start"]
    local, unmapped = {}, 0
    for i in range(n):
        h = hashlib.sha256(data[start + i * bpg:start + (i + 1) * bpg]).hexdigest()
        ch = m24.get(h)
        if ch is None:
            unmapped += 1
            ch = "⟦G:" + h[:8] + "⟧"  # ⟦G:sha8⟧
        local[base + i] = ch
    return local, unmapped


def tokenize(seg, ctl):
    """-> (tokens, error). token = ('g', code) | ('c', family, operand_bytes) | ('cskip', b0, b1).
    A NUL at token boundary ends the message; Z-control operands run to and include their NUL."""
    tokens, i, n = [], 0, len(seg)
    while i < n:
        b0 = seg[i]
        if b0 == 0:
            break
        if b0 < 0x80:
            tokens.append(("g", b0)); i += 1; continue
        if i + 1 >= n:
            return tokens, ("truncated", seg[max(0, i - 8):].hex())
        b1 = seg[i + 1]
        if b1 < 0x80:
            tokens.append(("g", (b0 & 0x7F) | (b1 << 7))); i += 2; continue
        if b1 != 0x80:  # non-dispatch pair: engine skips with no operand
            tokens.append(("cskip", b0, b1)); i += 2; continue
        spec = ctl.get(b0)
        if spec is None:  # family index >= 0x24: engine bounds-check skip
            tokens.append(("cskip", b0, b1)); i += 2; continue
        if spec[0] == "Z":
            j = seg.find(b"\x00", i + 2)
            if j < 0:
                return tokens, (f"{b0:02x}80-unterminated", seg[i:i + 24].hex())
            tokens.append(("c", b0, seg[i + 2:j])); i = j + 1; continue
        nop = spec[1]
        if i + 2 + nop > n:
            return tokens, (f"{b0:02x}80-truncated", seg[i:].hex())
        tokens.append(("c", b0, seg[i + 2:i + 2 + nop])); i += 2 + nop
    return tokens, None


def glyph_char(code, base, local, counters):
    if base != 1 and code < base:
        if 1 <= code <= 292:
            return G[code - 1]
        counters["global_unmapped"] += 1
        return "¿"  # ¿
    ch = local.get(code)
    if ch is None:
        counters["local_unknown"] += 1
        return "〓"  # 〓
    return ch


def glyph_width(code, base, local_w, global_w, counters):
    if base != 1 and code < base:
        gi = code - 1
        if 0 <= gi < len(global_w):
            return global_w[gi]
    else:
        gi = code - base
        if 0 <= gi < len(local_w):
            return local_w[gi]
    counters["width_miss"] += 1
    return 24


class WidthState:
    __slots__ = ("scale", "spacing")

    def __init__(self):
        self.scale, self.spacing = 1.0, 0.0

    def control(self, fam, op):
        if fam == SCALE_FAM and len(op) == 4:
            s = struct.unpack("<f", op)[0]
            if s != 0.0:
                self.scale = s
        elif fam == SCALE_RESET_FAM:
            self.scale = 1.0
        elif fam == ADVANCE_FAM and len(op) == 4:
            self.spacing = struct.unpack("<f", op)[0]
        elif fam == ADVANCE_RESET_FAM:
            self.spacing = 0.0

    def advance(self, w):
        mult = self.scale if 0.0 < self.scale <= 4.0 else 1.0
        sp = self.spacing if 0.0 <= self.spacing <= 128.0 else 0.0
        return int(round(w * mult + sp))


def find_speaker_split(tokens, anomalies):
    """returns (speaker_tokens or None, body_tokens, n_extra_separators).

    Primary pattern (spec): [8980]? 8780 8080 — tokens before it = speaker field.
    Secondary pattern (observed variant, 211 index-verified cases): a control-only line before
    the first newline holding exactly one name-source control (9380/a180), optionally wrapped in
    color set/reset (8880/8980) — no literal glyphs, so genuine body lines are never captured."""
    sep = None
    n_sep = 0
    for i in range(len(tokens) - 1):
        if tokens[i][:2] == ("c", SPEAKER_FLAG_FAM) and tokens[i + 1][:2] == ("c", NEWLINE_FAM):
            n_sep += 1
            if sep is None:
                sep = i
    if sep is None:
        for k, t in enumerate(tokens):
            if t[0] == "g":
                break
            if t[:2] == ("c", NEWLINE_FAM):
                prefix = tokens[:k]
                fams = [p[1] for p in prefix if p[0] == "c"]
                if (prefix and len(prefix) == len(fams)
                        and sum(1 for f in fams if f in (NAME_REF_FAM, 0xA1)) == 1
                        and all(f in (NAME_REF_FAM, 0xA1, 0x88, COLOR_RESET_FAM) for f in fams)):
                    anomalies["speaker_without_8780_flag"] += 1
                    spk = prefix[:-1] if prefix[-1][:2] == ("c", COLOR_RESET_FAM) else prefix
                    return spk, tokens[k + 1:], 0
                break
        return None, tokens, n_sep
    spk = tokens[:sep]
    if spk and spk[-1][:2] == ("c", COLOR_RESET_FAM):
        spk = spk[:-1]
    if any(t[0] == "c" and t[1] in (NEWLINE_FAM, PAGEBREAK_FAM) for t in spk):
        anomalies["separator_mid_text_rejected"] += 1
        return None, tokens, n_sep
    return spk, tokens[sep + 2:], n_sep - 1


def render_speaker(tokens, base, local, local_w, global_w, ws, counters):
    parts, ref_ids, dyn, color_ops, px = [], [], [], [], 0
    for t in tokens:
        if t[0] == "g":
            parts.append(glyph_char(t[1], base, local, counters))
            px += ws.advance(glyph_width(t[1], base, local_w, global_w, counters))
            continue
        if t[0] == "cskip":
            counters["cskip"] += 1
            continue
        fam, op = t[1], t[2]
        ws.control(fam, op)
        if fam == NAME_REF_FAM:
            rid = op[0] if op else -1
            ref_ids.append(rid)
            parts.append(f"⟦이름:{NAME_ID_MAP.get(rid, 'id%d' % rid)}⟧")
        elif fam == 0x88:
            color_ops.append(op.hex())
        elif fam in DYNAMIC_FAMS:
            dyn.append(f"{fam:02x}80")
            label = {0x92: "문자열", 0xA1: "이름슬롯", 0xA2: "아이템", 0xA3: "아이템"}.get(fam, "동적")
            parts.append(f"⟦{label}#{op.hex()}⟧")
        elif fam in (RUBY_BASE_FAM, RUBY_TEXT_FAM):
            counters["ruby"] += 1
        # everything else invisible
    text = "".join(parts)
    if ref_ids:
        mode = "character_reference"
    elif dyn:
        mode = "control_expression"
    elif any(t[0] == "g" for t in tokens):
        mode = "literal_glyphs"
    else:
        mode = "control_expression"
    return text, mode, ref_ids, color_ops, px


def render_body(tokens, base, local, local_w, global_w, ws, counters):
    parts, markers, ops, name_refs = [], [], [], []
    lines = [0]
    pages, n_marker, has_ruby = 1, 0, False
    for t in tokens:
        if t[0] == "g":
            parts.append(glyph_char(t[1], base, local, counters))
            lines[-1] += ws.advance(glyph_width(t[1], base, local_w, global_w, counters))
            continue
        if t[0] == "cskip":
            counters["cskip"] += 1
            continue
        fam, op = t[1], t[2]
        ws.control(fam, op)
        if fam == NEWLINE_FAM:
            parts.append("\n"); lines.append(0)
        elif fam == PAGEBREAK_FAM:
            parts.append("\n⟦P⟧"); lines.append(0); pages += 1
        elif fam == NAME_REF_FAM:
            rid = op[0] if op else -1
            name_refs.append(rid)
            parts.append(f"⟦이름:{NAME_ID_MAP.get(rid, 'id%d' % rid)}⟧")
            ops.append({"family": "9380", "op": op.hex(), "name": NAME_ID_MAP.get(rid, "id%d" % rid)})
        elif fam in NUMBERED_MARKER_FAMS:
            n_marker += 1
            parts.append(f"⟦{n_marker}⟧")
            markers.append({"n": n_marker, "family": f"{fam:02x}80"})
            ops.append({"n": n_marker, "family": f"{fam:02x}80", "op": op.hex()})
        elif fam in (RUBY_BASE_FAM, RUBY_TEXT_FAM):
            has_ruby = True  # base text stays inline; 9180 reading payload dropped
        # everything else invisible
    return ("".join(parts), markers, ops, name_refs, lines, pages, has_ruby)


def normalize(text):
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def occ_category(archive, mid, is_bank, has_spk):
    if 76 <= archive <= 135:
        if 50000 <= mid <= 54999: return "menu_item_name"
        if 55000 <= mid <= 59999: return "menu_item_desc"
        if 70000 <= mid <= 74999: return "menu_effect"
        if 75000 <= mid <= 76999: return "menu_valuable"
        if 5052 <= mid <= 5061: return "menu_surname"
        if 5000 <= mid <= 6999: return "menu_symbology"
        if 0 <= mid <= 1350: return "menu_place"
        return "menu_misc"
    if archive == 3454: return "battle_db"
    if archive == 1775: return "ic"
    if archive == 6068: return "item_flat"
    if archive == 38: return "system"
    if is_bank: return "spoken" if has_spk else "event_text"
    return "other_spoken" if has_spk else "other"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="SO3 DC full-KO inventory builder (defaults = disc 1)")
    ap.add_argument("--catalog", default=CATALOG,
                    help="container_catalog.csv (disc-2: full_ko_d2 catalog)")
    ap.add_argument("--stream-manifest", default=STREAM_MANIFEST,
                    help="stream_manifest.csv (fit data + paired .bin detection)")
    ap.add_argument("--out-dir", default=FULL_KO,
                    help="output dir for inventory_containers.json / "
                         "translation_units.jsonl / inventory_stats.json")
    ap.add_argument("--spoken-csv", default=SPOKEN_CSV,
                    help="spoken_dialogue_index.csv; missing file => speaker "
                         "cross-check skipped (disc 2 has none)")
    ap.add_argument("--event-bank-catalog", default=EBC_CSV,
                    help="event_bank_catalog.csv; missing file => event banks "
                         "classified by paired .bin presence only (disc 2)")
    ap.add_argument("--ues-csv", default=UES_CSV,
                    help="unique_exact_segments.csv; missing file => exact-sha "
                         "cross-check skipped (disc 2 has none)")
    ap.add_argument("--glyph-map", default=GLYPH24,
                    help="glyph_mapping_ordered_24.json (bitmap sha -> unicode)")
    ap.add_argument("--glyph-extra", action="append", default=None,
                    help="glyph_labels_extra.json (repeatable; default: "
                         "full_ko\\glyph_labels_extra.json)")
    ap.add_argument("--controls", default=CTL_JSON,
                    help="control_sizes_full.json (authoritative control table)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    catalog_path = args.catalog
    stream_manifest_path = args.stream_manifest
    spoken_csv = args.spoken_csv
    ebc_csv = args.event_bank_catalog
    ues_csv = args.ues_csv
    glyph_extra_paths = args.glyph_extra if args.glyph_extra else [GLYPH_EXTRA_DEFAULT]
    os.makedirs(args.out_dir, exist_ok=True)
    out_containers = os.path.join(args.out_dir, "inventory_containers.json")
    out_units = os.path.join(args.out_dir, "translation_units.jsonl")
    out_stats = os.path.join(args.out_dir, "inventory_stats.json")

    t0 = time.time()
    ctl, ctl_source, ctl_meta = load_control_table(args.controls)
    print(f"control table: {ctl_source} ({len(ctl)} families)")
    m24 = load_glyph_map(args.glyph_map)
    print(f"glyph mapping 24px: {len(m24)} bitmap shas")
    # vision-labeled extras (so3-glyph-labeling workflow). Single-char high/medium
    # labels join the map; {BTN:...}/{SYM:...}/'?' stay unmapped -> preserve-bitmap token.
    for extra_path in glyph_extra_paths:
        if not os.path.exists(extra_path):
            continue
        extra = json.load(open(extra_path, encoding="utf-8")).get("labels", {})
        added = 0
        for sha, rec in extra.items():
            ch = rec.get("char") or ""
            if (len(ch) == 1 and ch != "?" and sha not in m24
                    and rec.get("confidence") in ("high", "medium")):
                m24[sha] = ch
                added += 1
        print(f"glyph_labels_extra: +{added} labels merged "
              f"({len(extra) - added} left as preserve-bitmap tokens)")

    # --- catalog: dedupe by file_sha256, first occurrence wins; keep ALL occurrences as refs
    uniq_rows, occ_by_sha, sha_by_occ = [], defaultdict(list), {}
    with open(catalog_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sha = row["file_sha256"]
            a, s = int(row["archive_id"]), int(row["stream_id"])
            if sha not in occ_by_sha:
                uniq_rows.append(row)
            occ_by_sha[sha].append((a, s))
            sha_by_occ[(a, s)] = sha
    print(f"catalog: {sum(len(v) for v in occ_by_sha.values())} occurrences, {len(uniq_rows)} unique containers")

    # --- global 1.72 width table
    global_w = b""
    for row in uniq_rows:
        if row["version"].strip().endswith("1.72"):
            data = open(row["path"], "rb").read()
            hdr, _, global_w = parse_mclib(data)
            break
    assert len(global_w) >= 292, f"global width table too short: {len(global_w)}"
    print(f"global 1.72 widths: {len(global_w)} entries")

    # --- event bank catalog + stream manifest (fit + paired .bin)
    # EBC is disc-1-only enrichment: when absent, bank classification falls
    # back to the paired-.bin rule alone (already part of the is_bank test).
    ebc = set()
    have_ebc = ebc_csv and os.path.exists(ebc_csv)
    if have_ebc:
        with open(ebc_csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ebc.add((int(row["archive_id"]), int(row["event_stream_id"])))
    else:
        print(f"event bank catalog missing ({ebc_csv}) -> paired-.bin classification only")
    bin_set, fit = set(), {}
    occ_keys = set(sha_by_occ)
    with open(stream_manifest_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            a, s = int(row["archive_id"]), int(row["stream_id"])
            if row["extension"] == ".bin":
                bin_set.add((a, s))
            if (a, s) in occ_keys:
                fit[(a, s)] = {"mode": int(row["mode"]), "compressed": int(row["compressed"]),
                               "unpacked": int(row["unpacked"]), "next_rel": int(row["next_rel"])}
    print(f"event banks: {len(ebc)}; manifest fit rows for containers: {len(fit)}; .bin streams: {len(bin_set)}")

    # --- main pass over unique containers
    containers_out, oos_out = [], []
    units = {}
    my_shas = set()
    spot_samples = []  # (sha, sha12, archive, stream, mid)
    msg_speaker_lookup = {}  # (a,s,mid) -> (mode, norm_speaker_text, first_ref_id)
    anomalies = Counter()
    counters_total = Counter()
    untok = Counter()
    untok_examples = {}
    msg_cat_counter = Counter()
    total_msgs = in_scope_msgs = 0
    n_speaker_mode = Counter()
    n_ruby_msgs = n_unk_msgs = 0
    marker_unit_conflicts = 0

    for ci, row in enumerate(uniq_rows):
        sha = row["file_sha256"]
        occs = occ_by_sha[sha]
        data = open(row["path"], "rb").read()
        hdr, segs, local_w = parse_mclib(data)
        total_msgs += len(segs)
        is_bank = any((a, s) in ebc or (a, s - 1) in bin_set for a, s in occs)

        if hdr["glyph_width"] != 24:
            for mid, off, seg in segs:
                my_shas.add(hashlib.sha256(seg).hexdigest())
            oos_out.append({"file_sha256": sha, "path": row["path"], "reason": "32px",
                            "n_messages": len(segs), "refs": [{"archive": a, "stream": s} for a, s in occs]})
            continue

        local, n_unmapped_glyphs = build_local_map(data, hdr, m24)
        base = hdr["local_base"]
        msgs_out = []
        n_jp = 0
        has_any_jp = False

        for mid, off, seg in segs:
            exact = hashlib.sha256(seg).hexdigest()
            my_shas.add(exact)
            tokens, err = tokenize(seg, ctl)
            c = Counter()
            if err is not None:
                fam, exhex = err
                untok[fam] += 1
                untok_examples.setdefault(fam, []).append(
                    {"archive": occs[0][0], "stream": occs[0][1], "msgid": mid, "hex": exhex})
                msgs_out.append({"id": mid, "offset": off, "sha": exact, "text_key": None,
                                 "category": None, "max_line_px": None, "speaker": None,
                                 "status": "untokenizable", "untok_family": fam})
                continue

            ws = WidthState()
            spk_tokens, body_tokens, extra_sep = find_speaker_split(tokens, anomalies)
            if extra_sep > 0:
                anomalies["multi_separator_messages"] += 1
            spk_text = spk_mode = None
            spk_refs, spk_color, spk_px = [], [], None
            if spk_tokens is not None:
                spk_text, spk_mode, spk_refs, spk_color, spk_px = render_speaker(
                    spk_tokens, base, local, local_w, global_w, ws, c)
            body, markers, ops, name_refs, line_px, pages, has_ruby = render_body(
                body_tokens, base, local, local_w, global_w, ws, c)

            norm_body = normalize(body)
            norm_spk = normalize(spk_text) if spk_text is not None else None
            key = hashlib.sha1(((norm_spk or "") + "\x1f" + norm_body).encode("utf-8")).hexdigest()[:12]
            jp_body = bool(JP_RE.search(norm_body))
            jp_any = jp_body or (norm_spk is not None and bool(JP_RE.search(norm_spk)))
            if jp_any:
                n_jp += 1
                has_any_jp = True
            max_px = max(line_px) if line_px else 0
            n_speaker_mode[spk_mode or "none"] += 1
            counters_total.update(c)
            if has_ruby:
                n_ruby_msgs += 1
            if c["local_unknown"] or c["global_unmapped"]:
                n_unk_msgs += 1

            has_spk = spk_tokens is not None
            cat_votes = Counter(occ_category(a, mid, is_bank, has_spk) for a, s in occs)
            cat = min(cat_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]

            m = {"id": mid, "offset": off, "sha": exact, "text_key": key, "category": cat,
                 "max_line_px": max_px, "lines": len(norm_body.split("\n")) if norm_body else 0,
                 "pages": pages, "speaker": spk_mode, "status": "ok"}
            if ops: m["ops"] = ops
            if spk_px is not None: m["speaker_px"] = spk_px
            if spk_color: m["speaker_color_ops"] = spk_color
            if has_ruby: m["has_ruby"] = True
            if c["local_unknown"]: m["unknown_glyphs"] = c["local_unknown"]
            if c["global_unmapped"]: m["global_unmapped"] = c["global_unmapped"]
            msgs_out.append(m)

            for a, s in occs:
                msg_speaker_lookup[(a, s, mid)] = (spk_mode or "none", norm_spk,
                                                   spk_refs[0] if spk_refs else None)

            # --- translation unit accumulation (JP body only)
            if jp_body:
                u = units.get(key)
                if u is None:
                    u = units[key] = {
                        "key": key, "jp_body": norm_body, "jp_speaker": norm_spk,
                        "speaker_mode": spk_mode, "name_refs": sorted(set(name_refs + spk_refs)),
                        "line_count": len(norm_body.split("\n")),
                        "markers": markers, "insert_markers": len(markers),
                        "has_ruby": has_ruby,
                        "has_unknown_glyph": bool(c["local_unknown"] or c["global_unmapped"]),
                        "own_max_px": 0, "cats": Counter(), "n_occurrences": 0, "refs": [],
                    }
                else:
                    if [mk["family"] for mk in u["markers"]] != [mk["family"] for mk in markers]:
                        marker_unit_conflicts += 1
                    u["has_ruby"] = u["has_ruby"] or has_ruby
                    u["has_unknown_glyph"] = u["has_unknown_glyph"] or bool(
                        c["local_unknown"] or c["global_unmapped"])
                    for rid in name_refs + spk_refs:
                        if rid not in u["name_refs"]:
                            u["name_refs"].append(rid)
                u["own_max_px"] = max(u["own_max_px"], max_px)
                for a, s in occs:
                    u["cats"][occ_category(a, mid, is_bank, has_spk)] += 1
                    u["n_occurrences"] += 1
                    if len(u["refs"]) < 20:
                        u["refs"].append({"file_sha": sha[:12], "archive": a, "stream": s, "msgid": mid})

        in_scope = has_any_jp  # 24px + contains Japanese ([D10]); glyph_count==0 included
        if in_scope:
            in_scope_msgs += len(segs)
            for m in msgs_out:
                if m["status"] == "ok":
                    msg_cat_counter[m["category"]] += 1
            containers_out.append({
                "file_sha256": sha, "path": row["path"], "version": row["version"],
                "glyph_width": hdr["glyph_width"], "glyph_height": hdr["glyph_height"],
                "local_base": base, "glyph_count": hdr["glyph_count"],
                "mapping_count": hdr["mapping_count"],
                "n_messages": len(segs), "n_jp_messages": n_jp,
                "n_unmapped_local_glyphs": n_unmapped_glyphs,
                "is_event_bank": is_bank,
                "refs": [{"archive": a, "stream": s} for a, s in occs],
                "messages": sorted(msgs_out, key=lambda m: (m["id"], m["offset"])),
            })
        else:
            oos_out.append({"file_sha256": sha, "path": row["path"], "reason": "no_japanese",
                            "n_messages": len(segs),
                            "refs": [{"archive": a, "stream": s} for a, s in occs]})
        # deterministic spot-check samples: first message of every 60th container
        if ci % 60 == 0 and segs:
            mid, off, seg = segs[0]
            spot_samples.append((hashlib.sha256(seg).hexdigest(), sha[:12], occs[0][0], occs[0][1], mid))
        if (ci + 1) % 200 == 0:
            print(f"  ...{ci + 1}/{len(uniq_rows)} containers, {total_msgs} msgs, {time.time() - t0:.1f}s")

    print(f"parse done: {len(uniq_rows)} unique containers, {total_msgs} messages, {time.time() - t0:.1f}s")

    # --- finalize units
    unit_cat = Counter()
    unit_wclass = Counter()
    unit_marker_fams = Counter()
    n_units_markers = n_units_name = n_units_ruby = n_units_unk = 0
    with open(out_units, "w", encoding="utf-8", newline="\n") as f:
        for key in sorted(units):
            u = units[key]
            cat = min(u["cats"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
            wclass = "dialogue" if cat in ("spoken", "other_spoken") else "ui"
            own = u["own_max_px"]
            budget = min(max(own, 432), 576) if wclass == "dialogue" else (own if own >= 48 else 48)
            rec = {"key": key}
            if u["jp_speaker"] is not None:
                rec["jp_speaker"] = u["jp_speaker"]
                rec["speaker_mode"] = u["speaker_mode"]
            rec.update({"jp_body": u["jp_body"], "line_count": u["line_count"],
                        "insert_markers": u["insert_markers"]})
            if u["markers"]: rec["markers"] = u["markers"]
            if u["name_refs"]: rec["name_refs"] = u["name_refs"]
            if u["has_ruby"]: rec["has_ruby"] = True
            if u["has_unknown_glyph"]: rec["has_unknown_glyph"] = True
            rec.update({"width_budget_px": budget, "width_class": wclass,
                        "own_max_line_px": own, "category": cat,
                        "n_occurrences": u["n_occurrences"], "refs": u["refs"]})
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            unit_cat[cat] += 1
            unit_wclass[wclass] += 1
            fams = {mk["family"] for mk in u["markers"]}
            for fam in fams:
                unit_marker_fams[fam] += 1
            if u["markers"]: n_units_markers += 1
            if u["name_refs"] or "⟦이름:" in u["jp_body"] or (
                    u["jp_speaker"] and "⟦이름:" in u["jp_speaker"]): n_units_name += 1
            if u["has_ruby"]: n_units_ruby += 1
            if u["has_unknown_glyph"]: n_units_unk += 1
    print(f"translation units: {len(units)} -> {out_units}")

    # --- exact_sha256 spot-check vs unique_exact_segments.csv (skipped when absent)
    have_ues = ues_csv and os.path.exists(ues_csv)
    ues = set()
    if have_ues:
        with open(ues_csv, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ues.add(r["exact_sha256"])
        inter = my_shas & ues
        spot_detail = [{"exact_sha256": h, "file_sha": s12, "archive": a, "stream": s, "msgid": mid,
                        "in_ues": h in ues} for h, s12, a, s, mid in spot_samples[:26]]
        spot_ok = sum(1 for d in spot_detail if d["in_ues"])
        print(f"sha cross-check: mine={len(my_shas)} ues={len(ues)} intersection={len(inter)}; "
              f"spot {spot_ok}/{len(spot_detail)}")
    else:
        inter, spot_detail, spot_ok = set(), [], 0
        print(f"unique_exact_segments missing ({ues_csv}) -> sha cross-check skipped")

    # --- speaker cross-check vs spoken_dialogue_index.csv (skipped when absent;
    # disc 2 relies on the internal 8780-construct speaker detection alone)
    have_spoken = spoken_csv and os.path.exists(spoken_csv)
    xc = Counter()
    literal_mismatch_examples = []
    if have_spoken:
        with open(spoken_csv, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                a, s, mid = int(r["archive_id"]), int(r["stream_id"]), int(r["message_id"])
                mine = msg_speaker_lookup.get((a, s, mid))
                if mine is None:
                    xc["not_found"] += 1
                    continue
                mode, text, rid = mine
                imode = r["speaker_mode"]
                if imode == "implicit_or_continuation":
                    xc["match_none" if mode == "none" else "mismatch_index_none_detected_" + mode] += 1
                elif imode == "literal_glyphs":
                    if mode != "literal_glyphs":
                        xc["mismatch_index_literal_detected_" + mode] += 1
                    elif normalize(r["speaker_japanese"]) == (text or ""):
                        xc["match_literal_text"] += 1
                    else:
                        xc["match_literal_mode_text_diff"] += 1
                        if len(literal_mismatch_examples) < 10:
                            literal_mismatch_examples.append(
                                {"archive": a, "stream": s, "msgid": mid,
                                 "index": r["speaker_japanese"], "mine": text})
                elif imode == "character_reference":
                    if mode != "character_reference":
                        xc["mismatch_index_ref_detected_" + mode] += 1
                    elif str(rid) == r["speaker_reference_id"]:
                        xc["match_reference_id"] += 1
                    else:
                        xc["match_reference_mode_id_diff"] += 1
                elif imode == "control_expression":
                    xc["match_control_expression" if mode == "control_expression"
                       else "mismatch_index_ctlexpr_detected_" + mode] += 1
                else:
                    xc["index_mode_unknown_" + imode] += 1
        n_match = sum(v for k, v in xc.items() if k.startswith("match_"))
        n_total_xc = sum(xc.values())
        print(f"speaker cross-check: {n_match}/{n_total_xc} matched; detail={dict(xc)}")
    else:
        n_match = n_total_xc = 0
        print(f"spoken dialogue index missing ({spoken_csv}) -> speaker cross-check skipped")

    # --- outputs
    untok_total = sum(untok.values())
    stats = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "control_table_source": ctl_source,
        "control_table_verification": ctl_meta.get("verification"),
        "totals": {
            "unique_containers": len(uniq_rows),
            "containers_in_scope": len(containers_out),
            "containers_out_of_scope": {"32px": sum(1 for o in oos_out if o["reason"] == "32px"),
                                        "no_japanese": sum(1 for o in oos_out if o["reason"] == "no_japanese")},
            "messages_parsed_all_unique": total_msgs,
            "messages_in_scope": in_scope_msgs,
            "messages_untokenizable": untok_total,
            "untokenizable_pct_of_all": round(100.0 * untok_total / max(total_msgs, 1), 4),
            "translation_units": len(units),
        },
        "untokenizable_by_family": dict(untok),
        "untokenizable_examples": {k: v[:5] for k, v in untok_examples.items()},
        "messages_by_category_in_scope": dict(msg_cat_counter),
        "units_by_category": dict(unit_cat),
        "units_by_width_class": dict(unit_wclass),
        "speaker_detection_message_modes": dict(n_speaker_mode),
        "speaker_cross_check": ({"total_index_rows": n_total_xc, "matched": n_match,
                                 "detail": dict(sorted(xc.items())),
                                 "literal_text_diff_examples": literal_mismatch_examples,
                                 "notes": [
                                     "match_literal_mode_text_diff: mode agrees; text differs only where the "
                                     "index kept unresolved placeholders (⟦G:n⟧/⟦L:n⟧) or fullwidth spaces",
                                     "mismatch_index_ctlexpr_detected_*: rows the index could not decode "
                                     "(control_expression) that this build resolves with the full control "
                                     "table + OCR glyph map",
                                 ]} if have_spoken else
                                {"skipped": True,
                                 "reason": f"spoken dialogue index not found: {spoken_csv}"}),
        "sha_cross_check": ({"mine_unique": len(my_shas), "ues_unique": len(ues),
                             "intersection": len(inter),
                             "mine_not_in_ues": len(my_shas - ues), "ues_not_in_mine": len(ues - my_shas),
                             "spot_samples": spot_detail,
                             "spot_ok": spot_ok, "spot_total": len(spot_detail)} if have_ues else
                            {"skipped": True,
                             "reason": f"unique_exact_segments not found: {ues_csv}"}),
        "glyph_stats": {"messages_with_unknown_glyph": n_unk_msgs,
                        "unknown_local_glyph_occurrences": counters_total["local_unknown"],
                        "global_unmapped_occurrences": counters_total["global_unmapped"],
                        "width_table_misses": counters_total["width_miss"],
                        "units_with_unknown_glyph": n_units_unk},
        "marker_stats": {"units_with_numbered_markers": n_units_markers,
                         "units_by_marker_family": dict(unit_marker_fams),
                         "units_with_name_ref": n_units_name,
                         "units_with_ruby": n_units_ruby,
                         "messages_with_ruby": n_ruby_msgs,
                         "marker_family_conflicts_across_instances": marker_unit_conflicts},
        "anomalies": {**{k: v for k, v in anomalies.items()},
                      "nondispatch_control_pairs_skipped": counters_total["cskip"]},
        "container_fit": {c["file_sha256"][:12]: [
            {"archive": r["archive"], "stream": r["stream"],
             **(fit.get((r["archive"], r["stream"])) or {"mode": None, "compressed": None,
                                                         "unpacked": None, "next_rel": None})}
            for r in c["refs"]] for c in containers_out},
    }
    with open(out_stats, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"stats -> {out_stats}")

    inv = {"generated_utc": stats["generated_utc"], "control_table_source": ctl_source,
           "offset_convention": "text-blob-relative; exact_sha256 includes trailing NUL padding",
           "n_containers_in_scope": len(containers_out),
           "containers": containers_out,
           "out_of_scope": oos_out}
    with open(out_containers, "w", encoding="utf-8", newline="\n") as f:
        json.dump(inv, f, ensure_ascii=False, separators=(",", ":"))
    print(f"containers -> {out_containers}")
    print(f"total elapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

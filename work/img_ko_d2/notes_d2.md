# SO3 DC Disc 2 — Texture Catalog & Disc-1 Repaint Transfer (notes)

Date: 2026-07-24. Output root: `work\img_ko_d2\`. Inputs: disc-2 unpack
`work\full_unpack\disc2\` (stream_manifest.csv + decoded\), disc-1 image-patch
work `work\img_ko\` (v1.1.0, 8 repainted FIS members).

## 1. Decode results

- **1384 / 1384 top-level FIS decoded OK, 0 failures** (disc 1: 1383).
  Same pipeline: `img_ko\decode_textures.py --disc-root ..\full_unpack\disc2
  --out ..\img_ko_d2` (decoder already parameterized; unchanged).
- `png\` — 1384 RGBA PNGs; `texture_catalog.json` — 1384 entries, each now
  also carrying `src_sha256` (sha256 of the decompressed member) and
  `d2_only` (not byte-identical to any disc-1 FIS member).
- **277 unique images** after content dedup (disc 1: 274) → **12 contact
  sheets** `sheets\sheet_000_strt.png … sheet_011_kami.png`, same
  priority grouping as disc 1.
  - `make_sheets.py` in `img_ko\` was lightly parameterized: added an
    `--out` argument (default remains the disc-1 path, so disc-1 behavior
    is untouched); run with `--out ..\img_ko_d2`.

## 2. Disc-1 ↔ disc-2 content delta (by decompressed sha256)

- **Every disc-1 FIS texture exists byte-identical on disc 2** (disc-1-only
  count: **0**).
- **Disc-2-only members: 3** (all in archive 1584, absent from disc 1):
  see §4.
- Net: disc 2's texture set = disc 1's set + 3 minigame textures. No
  changed/redrawn variants of shared textures exist.

## 3. Disc-1 repaint transfer matrix — **8 / 8 reuse directly**

Matching = sha256 byte-identity of ORIGINAL decompressed member bytes.
Full machine-readable version: `transfer_plan.json` (`reusable`);
human log: `transfer_match_report.txt`.

| disc-1 target | disc-2 match | d2 iso_offset | d2 allocation | patched .slz | fits (slack) | next_rel |
|---|---|---|---|---|---|---|
| 40:25 strt 512x256 (options help) | **40:25** | 0x1B1D3CB8 | 31372 (next_rel) | 23631 | yes (7741) | identical — keep |
| 42:36 strt 512x512 | **42:36** | 0x1B325800 | 28112 (next_rel) | 24824 | yes (3288) | identical — keep |
| 44:41 strt 512x512 | **44:41** | 0x1B33A000 | 54112 (next_rel) | 49406 | yes (4706) | identical — keep |
| 2254:38031 John 512x512 4b (disc-message atlas) | **2254:36462** (+2255:36469, +2256:36476) | 0x9727DF80 (…) | 49104 (next_rel) | 22667 | yes (26437) | identical — keep |
| 47:49 strtb 320x208 24b | **47:49** | 0x1B35E800 | 166368 (next_rel) | 165607 | yes (761) | identical — keep |
| 47:52 strtf 320x208 24b | **47:52** | 0x1B3D1EF4 | 153676 (next_rel) | 151794 | yes (1882) | identical — keep |
| 47:55 strti 320x208 24b | **47:55** | 0x1B43E9EC | 158468 (next_rel) | 156782 | yes (1686) | identical — keep |
| 47:56 strtj 320x208 24b | **47:56** | 0x1B4654F0 | 169492 (chain end, 16+comp) | 168029 | yes (1463) | identical (0) — keep |

- **No .slz needs its next_rel re-stamped**: every disc-2 match has the same
  on-disk next_rel as the disc-1 member the patch was built against
  (47:56 is a chain end, next_rel=0, on both discs). Mode and unpacked size
  match everywhere (`mode_unpacked_ok: true`).
- 7 of 8 even keep the same archive:stream key; only the John atlas moved
  (2254:38031 → 2254:36462 — disc-2 stream renumbering within the archive).
- **John atlas copies**: disc 2 holds 3 byte-identical copies
  (2254:36462, 2255:36469, 2256:36476) — exactly mirroring disc 1, which also
  has 3 (2254:38031, 2255:38038, 2256:38045) of which v1.1.0 patched only
  2254:38031. All 3 disc-2 copies are listed in `reusable` (identical
  allocation 49104, same fit); apply the same single-copy policy as disc 1,
  or patch all 3 if we ever find the 2255/2256 archives referenced.
- The battle-tutorial screenshots (strtb/f/i/j) and title-demo panels all
  exist on disc 2 — nothing from the 8 is missing (`missing_on_d2: []`).
- Consequence: **a disc-2 image patch is a pure re-application** of the 8
  existing `img_ko\patched\*.slz` files at the disc-2 offsets. Only the
  TARGETS table / catalog lookup of `apply_image_patch.py` needs disc-2
  keys (esp. John 2254:36462) and the disc-2 ISO paths.

## 4. Disc-2-only textures — Japanese-text scan

Sheet: `sheets\new_000.png` (native resolution; reviewed visually).
3 unique members, all new in archive 1584 (streams 20957–20959, sandwiched
between so3mclib/RMAC/RTA/FAS and a DMM in what looks like a late-game
minigame resource block; disc 1's archive 1584 has no such streams):

| key | name | size | content | JP text? |
|---|---|---|---|---|
| 1584:20957 | John3 | 256x128 8b | orange "321" countdown numerals | no |
| 1584:20958 | John4 | 256x512 8b | "GO!!" ×5, country labels French / Germany / Spanish / Italy | no |
| 1584:20959 | John5 | 256x512 8b | "GOAL!!" ×5 + numbered pennants 1–4, labels "Frech" (sic, original typo) / Germany / Spanish / Italy | no |

- **No rendered Japanese text on any disc-2-only texture** → `new_jp_targets`
  in `transfer_plan.json` is **empty**. All three are English/numeric
  minigame graphics (some football/goal-themed event — countdown, GO,
  GOAL, team pennants). Nothing to repaint.
- No ending/staff-roll FIS surfaced at top level; if the staff roll draws
  rendered text it is either so3mclib glyph text (already covered by the
  text patch) or embedded FIS inside PACK/TGILP model containers (out of
  scope here, same as disc 1 — see disc-1 decode_notes.md §5).
- Kana font grids / SHI atlases: identical to disc 1 (all shared shas), so
  the disc-1 assessment carries over unchanged; no new review needed.

## 5. Files

- `texture_catalog.json` — 1384 entries (+`src_sha256`, `d2_only`, dedup/sheet fields)
- `png\` (1384), `sheets\sheet_000…011` (shared set), `sheets\new_000.png` (d2-only)
- `transfer_plan.json` — reusable(10 rows: 8 targets, John ×3 copies) /
  missing_on_d2(0) / new_jp_targets(0) / d2_only_reviewed(3)
- `transfer_match_report.txt` — human-readable match log
- `transfer_match.py`, `make_new_sheets.py` — this step's tooling

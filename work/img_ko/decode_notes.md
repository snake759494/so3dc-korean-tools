# SO3 DC Disc 1 — Texture / Image Catalog (decode notes)

Goal: decode every image resource, find which contain **rendered Japanese text**
(pixels, not encoded strings) so they can be repainted in Korean.

Output root: `work\img_ko\`
- `decode_textures.py` — the decoder (all 1383 top-level FIS)
- `make_sheets.py` — dedup + contact-sheet builder
- `png\` — 1383 decoded RGBA PNGs (`a<arch>_s<stream>_<name>.png`)
- `sheets\` — 12 contact sheets (`sheet_NNN_<group>.png`)
- `texture_catalog.json` — machine catalog (1383 entries)
- `test\` — validation renders (SHI, strtX, strt, strtb, spotcheck, embedded_sample)

## 1. FIS format (reverse-engineered and validated)

FIS (`46 49 53 00`) is a tri-Ace **GS/GIF DMA texture packet**: a chain of GIF
A+D register writes that upload a CLUT and an image to PS2 GS memory.

- `u32 @ 0x20` = **CLUT byte size** (0 ⇒ no CLUT / direct-colour image).
- **CLUT** (when present): RGBA8888 entries at file offset **0x100**, stored in PS2
  **CSM1** order → de-swizzle by swapping index bits 3↔4:
  `phys = (L & ~0x18) | ((L&0x08)<<1) | ((L&0x10)>>1)`. Applied when ≥32 entries
  (16-entry CLUTs, if any, are linear). Alpha is **PS2 alpha** (0x80 = opaque) →
  `A_out = min(255, A*2)`.
- **GS registers** parsed from the A+D chain (data qword then reg-addr qword):
  `0x50 BITBLTBUF` (→ DPSM = destination pixel format), `0x52 TRXREG` (→ transfer
  RRW×RRH), `0x53 TRXDIR`. The **image** transfer = the TRXREG with the largest area;
  the tiny 16×16 (or 8×2) TRXREG is the CLUT upload.
- **Pixel data start** = `clut_size + 0x200` for CLUT images, else `last_TRXDIR + 0x28`.
- Pixel data of **native indexed** and **direct** images is **linear raster** order
  (the GS performs the block addressing during a native transfer, so the file is
  unswizzled). The one exception is the disguised-8bpp class below.

### The five classes (image-transfer DPSM, CLUT present?)
| DPSM | CLUT | meaning | decode |
|---|---|---|---|
| 0x14 PSMT4 | yes | 4bpp indexed, native | linear, low-nibble-first, CLUT(16) | 
| 0x13 PSMT8 | yes | 8bpp indexed, native | linear, CLUT(256) |
| 0x00 PSMCT32 | yes | **8bpp DISGUISED as 32bpp** | real dims = **2×TRXREG**, **unswizzle8** + CLUT(256) |
| 0x00 PSMCT32 | no | 32bpp direct RGBA | linear, PS2 alpha |
| 0x01 PSMCT24 | no | 24bpp direct RGB | linear, opaque |
| 0x02/0x0A PSMCT16(S) | no | 16bpp 5551 (none seen) | linear |

The **disguised-8bpp** class is the classic PS2 trick: an 8bpp texture is uploaded
through a PSMCT32 transfer (4 indices per 32-bit word), so the bytes are swizzled
in GS-block order and must be run through the standard PS2 **8-bit unswizzle**
(`unswizzle8`, implemented vectorised & cached per (W,H)). Real width/height are
double the transfer's RRW/RRH.

### Validation (this is the make-or-break check)
- **SHI 8:12** (UI atlas): with CSM1 + PS2-alpha the labels **SELECT / START / L1 /
  R1 / L3 / R3** render as crisp bright-white AA text and the kana rows read cleanly.
  Linear (no-CSM1) palette gives dim/gray, wrong result → CSM1 is required.
- **strtX** (options menu): 32bpp-direct interpretation = pure swizzle noise; the
  8bpp-disguised interpretation (512×512, unswizzle8) renders a perfect
  "STAR OCEAN / Till the End of Time" + "Display/Audio/Event/Voice/Vibration/
  Difficulty Setting" menu → disguised-8bpp swizzle confirmed.
- **strt 38:21** native PSMT8 512×512 → "SQUARE ENIX presents / tri-Ace created /
  STAR OCEAN" logo, clean linear.
- **strtb 47:49** PSMCT24 320×208 → battle tutorial screenshot with JP attack name
  「カーレント・ナックル / SPECIAL ATTACK」, natural colours (byte order R,G,B correct).
- **ANKF** = full ASCII font; **John** = numeric font (0–9,%). All clean.

## 2. Results

- **1383 / 1383 FIS decoded OK, 0 failures** (`texture_catalog.json`).
- bpp: 4bpp ×395, 8bpp ×965, 24bpp ×8, 32bpp ×15. Swizzled (unswizzle8) ×625.
- Dimensions: 128×128 ×637, 256×256 ×419, 512×512 ×96, 128×256 ×70, 64×64 ×52,
  256×80 ×36 (ANKF), 320×208 ×8 (tutorial shots), plus 1024×256 etc.
- **274 unique images** after content-dedup (many font/UI atlases are copied into
  every container). 12 contact sheets built from the unique set.

### Name groups (base tag = first 4 chars; 5th byte is a sub-index)
| tag | what it is | text? |
|---|---|---|
| `strt` | title / logo / options / music-mode / install / **tutorial screenshots** | **YES** (EN + JP) |
| `kit`  | Battle-Collections / versus system labels, **GAME OVER** | **YES** (EN + JP) |
| `SHI`  | UI button+help atlases (SELECT/START, kana, EQUIPMENT/HERALDRY/RESULTS…) **and** Battle-Collections character renders | atlases YES, renders no |
| `ANKF` | ASCII font atlas | YES (glyphs) |
| `John` | numeric font (0–9,%) + a little menu art (feather) | YES (glyphs) |
| `yam1` | icon / item / element / digit atlases, menu window parts | some (numbers/labels) |
| `kami` | character portraits | mostly no |
| `asai` | field / map tilesets | no |

## 3. Where the Japanese text is (best assessment)

Rendered Japanese text to repaint in Korean lives almost entirely in **`strt`, `kit`,
`SHI`, and the fonts** — sheets **000–005**:
- **Title / system screens (sheet_000)** — options-menu help text is duplicated in
  Japanese (e.g.「テレビに関する設定を行います / 音の再生環境について設定します /
  イベントスキップについて設定します / …」), the PS2-BB-Unit install line
  「"PlayStation BB Unit" にインストールします」, and the 8 battle-tutorial
  screenshots (strtb–strtj, 320×208) carry JP attack names & captions.
- **GAME OVER (kit, sheet_001)** — Japanese subtitle under the "GAME OVER" logotype.
- **SHI UI atlases (8:12/8:13 and the 100 variants)** — kana rows for the message
  font plus button labels; mixed EN words. These are the in-battle / menu help bars.
- **Fonts** — ANKF (ASCII) and John (digits) are the glyph sheets themselves.
- Many menu labels are already **English** in this build (Display Setting, aspect
  ratio, subtitle options, Battle Collections terms) — those may or may not need
  Korean depending on scope; the clearly-Japanese items above are the definite targets.

Note the bulk of the disc's story/menu text is **not** pixels — it is the encoded
`so3mclib` glyph text already handled by the shipped text patch. These FIS images are
the *rendered-pixel* text that the text patch cannot reach.

## 4. Automated text flag (hint only)
`likely_text` = name in {strt, kit, ANKF, John} **OR** (indexed 4/8bpp AND horizontal/
vertical edge-fraction > 0.05 AND 0.008 < alpha-coverage < 0.75). Character renders
(SHI/kami) are deliberately excluded from the name rule and fall below the edge
threshold, so they are not flagged. → **499 textures flagged (106 unique)**. Every
catalog row carries `edge_score` and `coverage` so a reviewer can re-sort.

## 5. PACK / TGILP and other magics (scope note)
- **TGILP** = 3D scene/model container (nested transform records) — **not** a
  standalone image, but each holds FIS textures at its tail.
- Scanning all containers found **~17,586 additional embedded FIS** (TGILP 6,646,
  PACK 10,940), overwhelmingly tiny (64×32, 64×64, 128×64) **3D model / environment /
  effect skins** — sampled and confirmed: sky, water, parchment maps, glow/particle
  sprites, UI frame pieces. No menu/UI Japanese text observed in the sample.
- These are **decodable with the same pipeline** (the decoder works on any FIS blob),
  but are out of scope for the text catalog: they are model art with near-zero
  menu-text likelihood, and adding 17.5k tiny textures would balloon review to ~700
  low-signal sheets. Available on demand for a specific container if needed.
- Other magics (FAS, RTA, FPS, DMM, RMAC, so3mclib) are audio / motion / model /
  script / text-glyph streams, not raster images.

## 6. Contact sheets to review (priority order)
`work\img_ko\sheets\`
- **sheet_000_strt** — TITLE / LOGO / MENU / SYSTEM (highest JP-text priority)
- **sheet_001_kit** — GAME OVER / versus system + character renders
- **sheet_002..005_SHI** — UI/help atlases + Battle-Collections renders + ANKF/John
- **sheet_006..007_John** — numeric font + misc
- **sheet_008..010_yam1** — icon / item / element atlases (+kami)
- **sheet_011** — remaining kami / asai

CLUT/swizzle method settled: **CLUT@0x100, CSM1 (bit3↔bit4), PS2 alpha ×2; pixels
linear except the PSMCT32+CLUT class which is 8bpp @2× dims via unswizzle8.**

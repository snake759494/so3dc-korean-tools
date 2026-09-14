# Star Ocean 3 Director's Cut (PS2 JP) unpack/font analysis

Analysis date: 2026-07-13
Input: Japanese Director's Cut Disc 1 (`SLPM-65438`), verified by the SHA-256
listed in the repository README.

## Final result

The disc has been completely unpacked and the Japanese kanji font has been
located. It is **not** the PS2 BIOS `KROM` font. The game stores text and the
glyphs needed by that text together in `so3mclib 1.75` resources.

The game combines two font levels:

1. A global `so3mclib 1.72` atlas with 292 common 24x24 glyphs.
2. Per-message `so3mclib 1.75` atlases containing local 24x24 kanji subsets.

A separate `so3mclib 1.75` used for staff credits contains 624 local 32x32
glyph slots. The extracted examples visibly contain kanji, kana, and Latin
characters and can be reconstructed directly from the message bytecode.

## Source image and boot executable

- Disc 1 volume label: `STAROCEANTETDC`
- Disc 1 size: 4,689,854,464 bytes
- Boot ELF: `SLPM_654.38`, 751,024 bytes
- Boot ELF SHA-256:
  `DEE97E6F182F12F4AEF355AF41EA4294AAA7E38DDAF95DE0F6EE57743A7657CE`
- ELF: 32-bit little-endian MIPS, entry `0x00100008`

The visible ISO9660 tree contains only the loader, boot ELF, and IOP modules.
The bulk game data is stored outside that directory tree.

## Hidden tri-Ace archive

The public CUE `triAce-PS2.c` extractor correctly describes this disc:

- encrypted table byte offset: `0x200000` (LBA 1024)
- signature: `0x27D51556`
- seed: `0x13578642`
- entries: `0x1800` (6,144)
- three parallel little-endian `u32` arrays: LBA, sector count, auxiliary value
- sector size: `0x800` (2,048) bytes

The index XOR state machine was independently reimplemented. All 6,144 decoded
rows match the original CUE algorithm.

### Complete extraction totals

| Item | Verified result |
|---|---:|
| Index entries | 6,144 |
| Non-empty raw entries | 5,882 |
| Raw entry bytes | 4,687,747,072 |
| Root SLZ streams | 61,862 |
| Nested SLZ streams | 102 |
| Total decoded streams | 61,964 |
| Total decoded bytes | 7,130,233,940 |
| Decode/size errors | 0 |
| FIS resources | 1,383 |
| ELF overlays | 12 |

These figures were measured from a local `output/disc1` extraction. Decoded
game resources are intentionally not distributed.

### SLZ

The PS2 header is:

```text
0x00  char[3]  "SLZ"
0x03  u8       mode
0x04  u32      compressed payload size
0x08  u32      decompressed size
0x0C  u32      next SLZ relative offset (0 = none)
0x10  ...      payload
```

Modes 0 through 3 are store, LZSS, LZSS+RLE, and 16-bit LZSS. Every discovered
root, chained, and nested stream was decoded with an exact output-size check.

The 1,558 decoded resources beginning with `PACK` all fail the documented
QuickBMS 16-byte subentry-table bounds invariants. They are SO3 graphics or
animation asset headers, not PACK subarchives; the extractor deliberately does
not invent child files for them.

## Font resources

### Small UI atlases

Archive entry 8 contains two `FIS/SHI` 256x256 4bpp UI atlases:

- `decoded/0008/s000012_d0_o00000010_SHI.fis`, 34,304 bytes
- `decoded/0008/s000013_d0_o00003D90_SHI.fis`, 34,304 bytes

Archive entry 11 contains the `FIS/ANKF` alphanumeric atlas:

- `decoded/0011/s000018_d0_o00000000_ANKF.fis`, 11,776 bytes

These are patchable UI textures, but they are not the main dialogue kanji font.
The public French patch expands archive entry 8 and replaces its first SHI,
which independently confirms that this entry is used for localized UI graphics.

### Global 24px font: `so3mclib 1.72`

- absolute ISO offset: 3,788,176 (inside archive entry 8)
- decompressed size: 84,608 (`0x14A80`)
- SHA-256:
  `8F91FE6C630BF7890E2934D3B302911188C52DDE5732AA70E3ED40EEA325A3BC`
- glyphs: 292
- geometry: 24x24, linear 4bpp, 288 bytes per glyph
- width table: 292 bytes, values 5..24
- message table: none

This atlas contains digits, Latin characters, kana, punctuation, symbols, and
arrows. It supplies the common glyph codes used by normal 24px messages.

### Per-message kanji font: `so3mclib 1.75`

The SLZ scan found 7,785 version-1.75 members plus the one global 1.72 member.
Of all 7,786 members, 5,178 have a validated non-empty glyph array; the other
2,608 are valid message containers with `glyph_count=0` and use only global
glyphs.

Across the disc, bitmap hashing and deduplication produced:

- 4,714 distinct 24x24 glyph bitmaps, including the global atlas
- 291 distinct bitmaps in the 292-slot global atlas (one duplicate blank)
- 4,423 additional distinct local 24x24 bitmaps
- 623 distinct 32x32 glyph bitmaps

The readable union sheets and their source/hash mappings are:

- `work/kanji_deep/mclib_catalog/unique_24px_glyphs.png`
- `work/kanji_deep/mclib_catalog/unique_24px_glyphs.csv`
- `work/kanji_deep/mclib_catalog/unique_32px_glyphs.png`
- `work/kanji_deep/mclib_catalog/unique_32px_glyphs.csv`

A representative normal 24px container begins at ISO offset 489,103,360 and
has 283 local glyphs. Its atlas contains the system/dialogue kanji visibly.

The standalone 32px credits container begins at ISO offset 492,965,888
(LBA 240,706):

- decompressed size: 346,112 (`0x54800`)
- SHA-256:
  `77E595E98AA886F74D8F27695D987642895D08C28A8CE3D5F243518D959ECDE4`
- glyph slots: 624 (623 distinct; two slots are the same blank)
- messages: 1,220
- width range: 7..32

## `so3mclib` layout

The header is 0x80 bytes. Values below begin at file offset 0x10:

| Offset | Type | Meaning |
|---:|---|---|
| `0x10` | `u32` | `(message_id, text_offset)` table start |
| `0x14` | `u32` | message blob start / table end |
| `0x18` | `u32` | glyph advance-width table start |
| `0x1C` | `u32` | glyph bitmap array start |
| `0x20` | `u32` | local glyph count |
| `0x24/0x28` | `u32` | cache/surface dimensions |
| `0x2C/0x30/0x34` | `u32` | glyph width, height, stride |
| `0x38` | `u32` | first local glyph code (`local_glyph_code_base`) |
| `0x3C` | `u32` | message mapping count |
| `0x40` | `u32` | complete file size |

Each message-table row is `<u32 message_id, u32 text_offset>`. Rows are sorted
by ID, not always by offset, so message boundaries must be derived from sorted
unique offsets rather than the next table row.

The advance table has one byte per local glyph and is padded to an alignment
boundary before the bitmap array. Bitmap rows are linear, not GS-swizzled.
Each byte contains the left/earlier pixel in its low nibble and the next pixel
in its high nibble.

### Global/local code selection

For the normal 24px members, `local_glyph_code_base` is 301:

```text
decoded code < 301   -> global 1.72 atlas, index = code - 1
decoded code >= 301  -> local 1.75 atlas, index = code - 301
```

The 32px credits member has base 1, so all positive codes address its local
atlas with `index = code - 1`. A small special 24px member also uses base 1.

This proves that a local glyph index is not a global character code. Different
1.75 files may store different characters at the same local index.

## Message glyph code

Ordinary glyph codes use a little-endian 7-bit variable integer:

```text
01..7F       -> one-byte code
80..FF xx    -> code = (first & 0x7F) | (xx << 7), where xx < 0x80
```

The decoded positive code is then routed through the global/local rule above.
Confirmed controls in the 32px credits member are:

- `80 80`: newline
- `8A 80 <float32-le>`: scale; observed values 0.5, 0.6, 0.8, and 0.9
- `00`: message terminator outside control arguments

Direct reconstruction proves the interpretation:

- `84 02 85 02 05 86 02 87 02` renders `花房 利光`
- message 20002 renders `追加ミュージックスタッフ`
- message 20 renders three lines: `Original Story`, `Technical Programmer`,
  `Director`

All 1,220 messages in the 32px member decode without an unknown control or an
out-of-range glyph.

### Whole-disc message validation

All 7,786 mclib occurrences were parsed, including the 2,608 members whose
local glyph count is zero. The 401,957 mapping rows all have valid sorted
boundaries and a terminating NUL; there are no duplicate offsets inside a
table. Seventy-five container occurrences have table rows whose ID order is
not text-offset order, directly proving the sorted-boundary requirement.

Exact boundary-byte comparison leaves 37,814 distinct message segments and
364,143 duplicate occurrences. A secondary normalization that reduces trailing
NUL runs to one byte yields 37,172 canonical byte strings; it is not used as
the authoritative count. Before any uncharacterized formatting command,
1,718,238 glyph operands were verified:

| Reference class | Count | Code/index range | Range errors |
|---|---:|---|---:|
| base-301 global | 1,147,051 | codes 1..292 / indices 0..291 | 0 |
| base-301 local | 558,048 | codes 301..1893 / indices 0..1592 | 0 |
| base-1 local | 13,139 | codes 1..624 / indices 0..623 | 0 |

Reserved base-301 codes 293..300 are used zero times. This is independent
evidence that 301 is the local-code boundary rather than an arbitrary flag.

Across all messages, `80 80` was verified 20,609 times and `8A 80 + float32`
19,917 times. Twenty additional high/high command pairs remain intentionally
uncharacterized; 188,749 messages stop safely at the first such command rather
than guessing its operand length. Their raw contexts are preserved in
`work/mclib_all_decode/unresolved_controls.csv`. Message/font selection,
container geometry, and message boundaries do not depend on assigning meanings
to those remaining formatting commands.

## Rejected BIOS-font hypothesis

The Japanese BIOS `KROM` was extracted and rendered as a comparison control.
It contains 3,489 double-byte Shift-JIS glyphs at 16x15, 1bpp, 30 bytes each.
No multi-glyph KROM run occurs in the game resources or captured EE RAM, while
the `so3mclib` resources contain exact 24x24/32x32 4bpp Japanese glyphs and the
message operands that select them. SO3 therefore does not use BIOS KROM as its
main text font.

## Reproduction tools and reports

- Full unpacker: `unpacker/So3Unpack.csproj`
- Unpacker usage: `unpacker/README.md`
- Extraction verification: `unpacker/verify_extract.py`
- Hidden-index utility: `tools/so3_index.py`
- First-dialogue patcher: `tools/patch_first_dialogue.py`
- First-dialogue verifier: `tools/verify_first_dialogue_iso.py`

The one-off catalog, renderer, RAM-probe, and whole-disc analysis outputs used
during research remain local because they contain extracted game data.

Run the already-completed extraction verifier:

```powershell
python unpacker/verify_extract.py output/disc1
```

The latest verification result is `errors=0`.

## Korean patch implication

A Korean insertion must rebuild each message and its font resources as one
unit: encode message glyph codes, choose global or local slots, write local
glyph advance widths and 4bpp bitmaps, update mclib offsets/file size, then
recompress SLZ and preserve or correctly rebuild the surrounding sector
layout. QuickBMS's available tri-Ace compressor is a literal/fake compressor
and can expand data, so a size-bounded real SLZ compressor or a complete
archive/offset rebuilder is the next required implementation step.

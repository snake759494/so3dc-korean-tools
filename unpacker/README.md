# Star Ocean 3 PS2 hidden archive extractor

`So3Unpack` reproduces CUE's `triAce-PS2` index decryption and adds complete
SLZ discovery/decompression, chain relationships, nested-SLZ traversal,
validated `PACK` subentry parsing, FIS resource naming, and CSV/JSON manifests.

The Japanese Director's Cut disc stores its hidden archive index at ISO offset
`0x200000`.  It contains 6144 entries in three encrypted `uint32` arrays:
LBA, sector count, and an auxiliary value.  The seed is `0x13578642` and the
decoded entries partition the hidden disc area.

Run the complete extraction:

```powershell
dotnet run -c Release --project unpacker/So3Unpack.csproj -- `
  "SO3_DC_Disc1_original.iso" `
  "output/disc1" --raw-mode full --decoded-mode all --resume
```

Space-saving modes:

- `--raw-mode none`: do not duplicate the 4.7 GiB sector-aligned archive files.
- `--decoded-mode priority`: save only FIS/font, ELF/runtime and likely text
  candidates while still scanning and cataloguing every SLZ stream.
- `--decoded-mode none`: manifest-only scan.
- `--no-json`: omit the large combined JSON; CSV manifests are always written.
- `--max-depth N`: cap nested SLZ traversal (default 3).

Outputs:

- `index_decoded.bin`: three decoded `uint32[6144]` arrays.
- `raw/NNNN.ext`: exact sector-aligned archive entries.
- `decoded/NNNN/`: decompressed SLZ resources. FIS names such as `SHI` and
  `ANKF` are included in filenames.
- `manifests/archive_manifest.csv`: all 6144 index entries.
- `manifests/stream_manifest.csv`: every root, chained, and nested SLZ stream.
- `manifests/pack_manifest.csv`: only PACKs satisfying the documented
  16-byte subentry invariants. SO3 graphics/animation assets that merely use a
  `PACK` magic are explicitly marked `asset_header_not_subarchive`.
- `manifests/priority_candidates.csv`: fonts, overlays, runtime modules, FIS,
  and likely Shift-JIS resources.
- `manifests/manifest.json`: combined machine-readable manifest.

The extractor never infers a PACK table from magic alone.  This matters for
SO3 because many decompressed graphics/animation structures begin with `PACK`
but do not use the newer QuickBMS-style `PACK` subarchive layout.

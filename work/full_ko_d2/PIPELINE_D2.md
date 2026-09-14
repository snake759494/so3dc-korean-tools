# PIPELINE_D2 — Disc 2 Korean-patch pipeline (ready-to-run commands)

WS = `C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2`
D2 = `%WS%\work\full_ko_d2` (this dir) / unpack root = `%WS%\work\full_unpack\disc2`
Original Disc 2 ISO: `D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 2).iso`
  SHA-256 `349FF9443E3ACFD92045C5DFAD6D97EB51D2765B0781BCD59DCD424D81670288`, size `4,685,955,072`.

All tools keep their **disc-1 defaults**; every disc-2 run passes explicit options.
Run everything from the directory shown in each step (relative paths below assume it).
`SO3_WS` env var overrides the workspace root in build_inventory / so3_full_patch /
verify_full_iso / solve_controls / decode_textures / build_container_catalog.

Disc 2 has **no** spoken_dialogue_index / event_bank_catalog / unique_exact_segments —
pass `""` for those options; the tools then skip the cross-checks (speaker detection is
internal via the 8780 construct; event banks classify by paired `.bin` presence).

## 0. Unpack (DONE 2026-07-24)
`work\full_unpack` So3Unpack with `--resume` until `DONE`; manifests written to
`disc2\manifests\` (60,395 streams; 7,578 so3mclib rows, all depth 0).

## 1. Container catalog (DONE 2026-07-24)
```
cd %WS%\work\full_ko_d2
python build_container_catalog.py
```
Outputs `container_catalog.csv` (7,578 occurrence rows / 1,488 unique files; one
`so3mclib 1.72` global-font row) + `container_catalog_report.json` (disc-1 overlap
1,433/1,488 unique files = 7,426/7,578 occurrences; glyph gap 400 unlabeled of which
only **63 are disc-2-new**, listed in `glyph_gap_new_shas.json`).
Sanity: the same builder run against disc 1's manifest reproduces disc 1's catalog on
every consumed column (verified 2026-07-24).

## 2. Control-table verification over the disc-2 corpus (DONE 2026-07-24)
```
cd %WS%\work\full_ko
python solve_controls.py --finalize --catalog ..\full_ko_d2\container_catalog.csv --out-dir ..\full_ko_d2
```
Result: `engine-exact verification: 183403/183403 messages tokenize cleanly (0 fail)`
(38,244 unique segments). Wrote `full_ko_d2\control_sizes_full.json`; its operand table
is IDENTICAL to disc-1's (only the `_meta.verification` / observed-occurrence counts
differ). Either file works as `--controls`.

## 3. Glyph labeling for the 63 new bitmaps
Label the sha256s in `full_ko_d2\glyph_gap_new_shas.json` (so3-glyph-labeling workflow,
glyph sheets from the containers listed under `glyph_coverage.worst_containers` in the
report). Save labels to `full_ko_d2\glyph_labels_extra_d2.json` (same shape as
`full_ko\glyph_labels_extra.json`). Unlabeled leftovers become preserve-bitmap
`⟦G:sha8⟧` tokens automatically — safe, but label anything that appears in JP text.

## 4. Inventory
```
cd %WS%\work\full_ko
python build_inventory.py --catalog ..\full_ko_d2\container_catalog.csv --stream-manifest ..\full_unpack\disc2\manifests\stream_manifest.csv --out-dir ..\full_ko_d2 --spoken-csv "" --event-bank-catalog "" --ues-csv "" --glyph-extra glyph_labels_extra.json --glyph-extra ..\full_ko_d2\glyph_labels_extra_d2.json
```
(`--glyph-extra` is repeatable and REPLACES the default list — always pass the disc-1
file first, then the d2 one. `--controls` defaults to disc-1's table; pass
`--controls ..\full_ko_d2\control_sizes_full.json` after step 2 if preferred.)
Outputs to `full_ko_d2\`: `translation_units.jsonl`, `inventory_containers.json`,
`inventory_stats.json`. Category rules reuse disc-1 archive ranges — review
`messages_by_category_in_scope` in the stats for disc-2 plausibility before batching.

## 5. Translation reuse + batches
Reuse first (new script, to be written): match disc-2 units against disc-1
`full_ko\tr_out` by identical `(jp_speaker, jp_body)` — `text_key` is a sha1 of exactly
that pair, so equal keys = equal text; expect most menu/battle/system units to hit.
Emit pre-filled `full_ko_d2\tr_out\batch_*_ko.json` for reused units; translate the rest.
```
cd %WS%\work\full_ko
python prepare_translation_batches.py --units ..\full_ko_d2\translation_units.jsonl --inventory ..\full_ko_d2\inventory_containers.json --out-dir ..\full_ko_d2\tr_batches --index ..\full_ko_d2\tr_batches_index.json --catalog ..\full_ko_d2\container_catalog.csv
```
(Glossary defaults stay on disc-1's `glossary_full.json` / `glossary_draft.json` — shared.)

## 6. Validate translations
```
cd %WS%\work\full_ko
python validate_translations.py --batches-dir ..\full_ko_d2\tr_batches --out-dir ..\full_ko_d2\tr_out --report ..\full_ko_d2\tr_validation_report.json --catalog ..\full_ko_d2\container_catalog.csv
```
(Width engine then uses the disc-2 global 1.72 row; font default unchanged.)

## 7. Patch plan
```
cd %WS%\work\full_ko
python build_full_plan.py --tr-out-dir ..\full_ko_d2\tr_out --inventory ..\full_ko_d2\inventory_containers.json --out ..\full_ko_d2\patch_plan_full.json
```
NOTE: the disc-1 post-processors `plan_fixups.py` / `fit_repair.py` still hardcode
`full_ko\patch_plan_full.json` — parameterize (or copy+edit) them at this step if the
disc-2 sim shows the same variant-marker / capacity-over cases they fixed on disc 1.

## 8. Simulate, then build
```
cd %WS%\work\full_ko
python so3_full_patch.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 2).iso" --plan ..\full_ko_d2\patch_plan_full.json --simulate --report ..\full_ko_d2\sim_report.json --catalog ..\full_ko_d2\container_catalog.csv --stream-manifest ..\full_unpack\disc2\manifests\stream_manifest.csv
```
Gate: `archives_failed == 0`. Then drop `--simulate`:
```
python so3_full_patch.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 2).iso" "D:\ps2\SO3_DC_Disc2_Korean_Full_msg.iso" --plan ..\full_ko_d2\patch_plan_full.json --report ..\full_ko_d2\patch_report.json --catalog ..\full_ko_d2\container_catalog.csv --stream-manifest ..\full_unpack\disc2\manifests\stream_manifest.csv
```
(so3_full_patch pins no ISO hash; it verifies via the hidden index + re-read. The
name-table step ⑦ (so3_name_patch) and FIS image step ⑧ are disc-1-hardcoded and need
their own d2 ports — run them AFTER the message patch, as on disc 1.)

## 9. Verify
```
cd %WS%\work\full_ko
python verify_full_iso.py --original "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 2).iso" --patched "D:\ps2\SO3_DC_Disc2_Korean_Full_msg.iso" --plan ..\full_ko_d2\patch_plan_full.json --report ..\full_ko_d2\verify_full_report.json --units ..\full_ko_d2\translation_units.jsonl --inventory ..\full_ko_d2\inventory_containers.json --batches-dir ..\full_ko_d2\tr_batches --tr-out-dir ..\full_ko_d2\tr_out --catalog ..\full_ko_d2\container_catalog.csv --manifest ..\full_unpack\disc2\manifests\stream_manifest.csv --controls ..\full_ko_d2\control_sizes_full.json --expected-original-sha256 349FF9443E3ACFD92045C5DFAD6D97EB51D2765B0781BCD59DCD424D81670288 --expected-original-size 4685955072
```
Exit 0 required. Do NOT pass `--name-patch` until a disc-2 name patch exists (its
extents/21-slot audit are disc-1-specific).

## 10. Textures (FIS) cataloging
```
cd %WS%\work\img_ko
python decode_textures.py --disc-root ..\full_unpack\disc2 --out ..\img_ko_d2
```
Outputs `img_ko_d2\png\*.png` + `texture_catalog.json` (+ `decode_failures.txt`).

## 11. Release
xdelta original→patched, package with disc 1 as v1.2.0 (see MASTER_PLAN "Disc 2 확장 계획" ⑩).

## Known issue (pre-existing, noted 2026-07-24)
`full_ko\verify_fixture\` (July 17) predates the line-initial `\n⟦P⟧` text convention:
143/261 of its plan messages carry end-of-line `⟦P⟧`, so the CURRENT verifier reports
`[plan] ⟦P⟧ must appear once, at line start` against the old fixture ISO. This is
fixture staleness, not a verifier bug — regenerate the fixture via
`selftest_verify_full_iso.py` before using it as a regression baseline again.

## Parameterization changes (2026-07-24, disc-1 defaults intact)
- `build_inventory.py`: `--catalog --stream-manifest --out-dir --spoken-csv --event-bank-catalog --ues-csv --glyph-map --glyph-extra(repeatable) --controls`; empty/missing spoken/EBC/UES inputs skip those cross-checks cleanly.
- `prepare_translation_batches.py`: added `--catalog --controls`.
- `validate_translations.py`: already fully parameterized (no change).
- `build_full_plan.py`: added `--tr-out-dir --inventory --out`.
- `so3_full_patch.py`: added `--catalog --stream-manifest` (threaded through `_plan_from_json` → `expand_unique_to_occurrences` / `build_plan`); no ISO SHA pin exists in this tool.
- `verify_full_iso.py`: added `--expected-original-sha256 --expected-original-size` (size also drives the tail diff-scope scan); everything else already had CLI args.
- `solve_controls.py`: added `--catalog --out-dir` (main + `--finalize`).
- `img_ko\decode_textures.py`: added `--disc-root --manifest --out`.
- `full_ko_d2\build_container_catalog.py`: NEW — disc-2 catalog straight from the stream manifest (columns = everything consumers read, disc-1 names/order; one row per occurrence).

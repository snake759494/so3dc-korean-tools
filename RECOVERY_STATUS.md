# 저장소 복원 상태 (2026-09-14)

이 저장소는 이전 GitHub 계정(`snake7594`)이 삭제되면서 원격 저장소·릴리스가
함께 사라지고, 로컬 작업 폴더도 남아 있지 않은 상태에서 **작업 세션 기록
(Claude Code 트랜스크립트 262MB, Codex 세션 로그)에 남은 파일 쓰기·편집·읽기
이력을 재생(replay)해 복원**한 것입니다. 릴리스 산출물(xdelta 패치)은 남아 있던
v1.2.1 최종 ISO에서 다시 만들어 **원본 릴리스와 바이트 단위로 동일**함을
확인했습니다.

## 복원 신뢰도

| 등급 | 의미 |
|---|---|
| **A 정확** | 복원 결과가 2026-08-05 시점의 원본 파일 크기와 바이트 단위로 일치(당시 기록된 크기 대조). 또는 세션 기록의 마지막 Write 이후 편집이 전부 재생됨 |
| **B 근사** | 마지막 확인 시점 이후 셸 스크립트 등으로 수정된 이력이 있어 최종본과 소폭 차이 가능 |
| **C 미복원** | 기록에 내용이 남지 않아 재생성이 필요한 파일 |

### 릴리스 산출물 — A
- `SO3_DC_Disc1_Korean_Full_v1.2.1.xdelta` SHA-256 `2C28CB9B…` / Disc 2 `624D9093…` — 원본 릴리스와 동일 (v1.2.1 최종 ISO에서 재생성, xdelta 3.0.11 `-e -9`)

### 핵심 도구 — A (크기 일치 검증)
`so3_repack.py`, `tools/patch_hyda_dialogue.py`, `tools/so3_index.py`, `tools/verify_hyda_dialogue_iso.py`,
`tools/build_hyda_patch_manifest.py`, `unpacker/Program.cs`, `unpacker/So3Unpack.csproj`, `unpacker/verify_extract.py`,
`work/full_ko/so3_full_patch.py`, `so3_name_patch.py`, `verify_full_iso.py`, `slz_optimal.py`, `build_inventory.py`,
`build_full_plan.py`, `plan_fixups.py`, `width_overrides.py`, `fit_repair.py`, `width_oracle.py`, `validate_translations.py`,
`prepare_translation_batches.py`, `solve_controls.py`, `decode_mclib_text.py`, `selftest_verify_full_iso.py`,
`test_so3_full_patch.py`, `test_so3_name_patch.py`, `proof_nonjp_bytes.py`, `validate_one.py`, `batch_status.py`,
`work/full_ko_d2/build_container_catalog.py`, `name_patch_d2_config.json`, `validate_one_d2.py`, `reuse_stats.py`,
`work/img_ko/fis_repaint.py`, `apply_image_patch.py`, `verify_image_patch.py`, `decode_textures.py`,
`image_translations.json`, `john_supplement_d1_apply.py`, `make_sheets.py`, `work/img_ko_d2/transfer_match.py`, `make_new_sheets.py`,
`STYLE_GUIDE.md`, `PIPELINE_D2.md`, `docs/BUILD_FROM_SOURCE.md`, `docs/ANALYSIS.md`, `docs/HYDA_653_KOREAN_PATCH.md`,
`docs/releases/v0.5.0-alpha.1 ~ v1.2.1`, `README.md`, `RELEASE_NOTES.md`, `LICENSE`, `tests/test_so3_repack.py`,
`.github/workflows/test.yml`, `automation/workflows/*.js`

### B 근사
- `work/full_ko/control_sizes_full.json` — 2026-07-16 판독본. 피연산자 테이블은 동일하나 `_meta`(검증 통계)가 최종본과 다를 수 있음. `solve_controls.py --finalize`로 재생성 가능
- `work/full_ko/MASTER_PLAN.md` — 2026-07-17 이후 추가된 진행 기록 일부 누락(역사 문서)
- `work/img_ko/repaint_all.py` — 마지막 확인 크기보다 529바이트 작음(2026-07-18 셸 패치 2건 재생 여부 확인 중). 8개 리페인트 생성 로직은 포함
- `work/img_ko_d2/transfer_plan.json` — 8개 extent 버전. v1.2.1에서 추가된 John 사본 2행(2255:36469, 2256:36476)은 텍스처 카탈로그 재생성 후 보충 필요
- `THIRD_PARTY_NOTICES.md`, `tools/verify_first_dialogue_iso.py`, `docs/FIRST_DIALOGUE_PATCH.md` — Codex 시절 초기 판

### C 미복원 (재생성 작업 진행 중)
- `work/full_ko/patch_plan_full.json`, `work/full_ko_d2/patch_plan_d2.json` — 최종 패치 플랜. **v1.2.1 최종 ISO에서 역디코딩해 재구성 예정**(검증기 `verify_full_iso.py`의 디코더 활용)
- `work/full_ko/tr_out_v121/`, `work/full_ko_d2/tr_out_v121/` — 최종 번역 DB. 원천 라운드(tr_out 289/306, r1 21/27, d2 26/27, r2 21/29 파일)는 워크플로 에이전트 기록에서 복원됨; 병합본 재생성 예정
- `work/font_ocr/glyph_mapping_ordered_24.json` — 글리프 비트맵 SHA→문자 OCR 맵(6.4MB). 인벤토리 재생성에 필요. 번역 배치 기록(원문 텍스트)과 ISO 글리프 코드를 정렬해 부분 복원 예정
- `work/full_ko/glyph_labels_extra.json`, `glyph_label_audit.json`, `glossary_full.json`, `width_overrides_data*.json`,
  `width_fixes*.json`, `john_supplement_d1.json`, `translations/hyda_ko.json`, `hyda_patch_manifest.json`,
  `tools/patch_early_kanji_readings.py`, `patch_first_dialogue.py`, `verify_early_kanji_readings.py`, 나머지 `tests/*.py`,
  `docs/EARLY_KANJI_READING_PATCH.md`, `FIRST_TEXT_LOCATOR.md`, `translation_targets.json`, `assets/*.png`

## 검증 계획
복원된 도구·플랜으로 `docs/BUILD_FROM_SOURCE.md` 절차를 새 클론에서 다시 실행해
두 디스크의 최종 ISO SHA-256(`E31911EF…`, `753FE920…`)이 재현되는지 확인하고,
통과하면 이 문서의 C 항목을 갱신합니다. 그전까지 "소스에서 직접 빌드" 절차는
플랜 파일 부재로 4단계부터 실행할 수 없습니다.

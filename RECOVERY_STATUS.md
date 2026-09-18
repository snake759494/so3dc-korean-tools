# 저장소 복원 상태 (2026-09-18)

이전 GitHub 계정(`snake7594`)이 삭제되면서 원격 저장소와 릴리스가 함께
사라졌고, 로컬 작업 폴더도 남아 있지 않았습니다. 이 저장소는 작업 세션
기록(Claude Code 트랜스크립트, Codex 세션 로그)에 남은 파일 쓰기·편집·읽기
이력을 재생해서 복원한 것입니다.

## 요약

- **릴리스(v1.2.1 xdelta 2종)는 원본과 바이트 단위로 같습니다.** 패치 적용에는
  아무 영향이 없습니다.
- **도구와 문서는 대부분 복원했습니다.** 핵심 도구는 원본 파일 크기와
  일치하거나, 실제로 돌려서 v1.2.1과 같은 결과가 나오는 것을 확인했습니다.
- **최종 패치 플랜 등 일부 데이터는 복원하지 못했습니다.** 그래서
  [`docs/BUILD_FROM_SOURCE.md`](docs/BUILD_FROM_SOURCE.md)의 소스 빌드는 지금은
  4단계(텍스트 패치)부터 진행할 수 없습니다.

## A. 검증 완료

| 항목 | 확인 방법 |
|---|---|
| xdelta 2종 | 남아 있던 v1.2.1 최종 ISO에서 다시 만들었고 SHA-256이 원본 릴리스와 같음 (`2C28CB9B…`, `624D9093…`) |
| `unpacker/` | 복원한 언팩커로 두 디스크를 다시 풀었고 스트림 수가 기존 기록과 같음 (61,964 / 60,395) |
| `work/full_ko_d2/build_container_catalog.py` | 카탈로그 결과가 기존 기록과 같음 (D1 7,786행·1,516 고유, D2 7,578행·1,488 고유) |
| `work/full_ko/control_sizes_full.json` | 복원한 `solve_controls.py`로 다시 만들었고 36개 계열의 피연산자 표가 기록과 같음 |
| 이미지 패치 (`fis_repaint.py`, `repaint_all.py`, `apply_image_patch.py`, `transfer_plan.json`, `john_supplement_d1.json`) | 리페인트를 다시 만들어 적용한 결과, 두 디스크 모두 텍스처 10곳이 v1.2.1 최종 ISO와 바이트 단위로 같음 |
| `work/full_ko_d2/glyph_labels_extra_d2.json` | 라벨 63개 전부 복원 |
| 그 밖의 도구·문서 | 기록된 원본 크기와 일치: `so3_repack.py`, `so3_full_patch.py`, `so3_name_patch.py`, `verify_full_iso.py`, `slz_optimal.py`, `build_inventory.py`, `build_full_plan.py`, `plan_fixups.py`, `width_overrides.py`, `fit_repair.py`, `width_oracle.py`, `validate_translations.py`, `prepare_translation_batches.py`, `solve_controls.py`, `tools/*.py`, `tests/test_so3_repack.py`, `README.md`, `RELEASE_NOTES.md`, `docs/` 대부분 |

## B. 일부만 복원

- `work/*/tr_out_v121/` (번역 DB): 기록에 남은 번역 라운드로 다시 구성했습니다.
  Disc 1은 원래 333개 파일 중 309개, Disc 2는 367개 중 343개입니다. 빠진
  배치 목록은 각 폴더의 `RECOVERY_MANIFEST.json`에 있습니다.
- `work/full_ko/glyph_labels_extra.json`: 라벨 453개 중 400개입니다. 다섯 번째
  시트의 53개와 v1.2.1 감사에서 고친 부분이 빠져 있습니다.
- `work/full_ko/MASTER_PLAN.md`: 2026-07-17 판입니다. 그 뒤의 진행 기록이
  빠져 있습니다.
- `THIRD_PARTY_NOTICES.md`, `tools/verify_first_dialogue_iso.py`,
  `docs/FIRST_DIALOGUE_PATCH.md`: Codex 작업 시기의 초기 판입니다.

## C. 복원하지 못함

- `work/full_ko/patch_plan_full.json`, `work/full_ko_d2/patch_plan_d2.json` (최종 패치 플랜)
- `work/font_ocr/glyph_mapping_ordered_24.json` (OCR 글리프 맵, 인벤토리 재생성에 필요)
- `glossary_full.json`, `glossary_draft.json`, `glyph_label_audit.json`,
  `width_overrides_data*.json`, `width_fixes*.json`, `width_irreducible*.json`
- `translations/hyda_ko.json`, `translations/hyda_patch_manifest.json`, `assets/*.png`,
  `docs/EARLY_KANJI_READING_PATCH.md`, `docs/FIRST_TEXT_LOCATOR.md`

## 패치 플랜을 되살리는 방법

v1.2.1 최종 ISO에는 모든 번역문이 들어 있습니다. 원본 ISO와 v1.2.1 ISO에서
같은 컨테이너를 비교해 바뀐 메시지를 찾으면 플랜을 역추출할 수 있습니다.
한국어 텍스트는 패처의 디코더(`struct_to_text`)와, 나눔 글꼴로 렌더링한
비트맵을 문자로 되짚는 표로 읽어 냅니다. 이 작업은 2026-09-14에 시작했지만
아직 끝내지 못했습니다.

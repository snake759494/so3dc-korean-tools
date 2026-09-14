# 번역·라벨링 자동화 워크플로 (참고용)

이 디렉터리의 `.js` 파일들은 실제 한국어화 작업에서 대량 번역·검증을
돌리는 데 사용한 [Claude Code](https://claude.com/claude-code) Workflow
스크립트 원본입니다. 패치 재현에는 필요 없고, 유사한 대규모 팬번역
프로젝트에서 자동화 방식을 참고할 수 있도록 보존합니다.

| 스크립트 | 용도 |
|---|---|
| `so3-main-translation-wf_*.js` | 메인 번역 오케스트레이션 (배치 → 번역 → 자체 검증 루프) |
| `so3-tr-shard.js` / `so3-tr-shard-d2.js` | 디스크 1/2 번역 샤드(소규모 병렬 워크플로 다중 기동으로 6배 가속) |
| `so3-tr-shard-relabel.js` | v1.2.1 글리프 라벨 교정 후 영향 유닛 재번역 |
| `so3-glossary-translate-wf_*.js` | 용어집 2,933항목 구축 |
| `so3-glyph-labeling-wf_*.js` | 로컬 아틀라스 글리프 비트맵 시각 판독(라벨링) |
| `so3-img-jp-scan-wf_*.js` | 텍스처 전수 시각 스캔으로 일본어 잔존 탐색 |

동작 개요: 각 배치는 `work/full_ko/tr_batches/`(원문·예산 포함, 로컬 생성)를
입력으로 받아 `tr_out*/batch_*_ko.json`(키+한국어)을 출력하고,
`validate_one*.py`로 폭 게이트·마커 보존을 즉시 검사해 실패 시 재시도합니다.
번역 규칙은 `work/full_ko/STYLE_GUIDE.md`, 용어 통일은
`work/full_ko/glossary_full.json`을 따릅니다.

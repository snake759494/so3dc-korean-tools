# full_patch 범용 패처 명세 (v1, 2026-07-16)

목표: 디스크의 모든 대상 mclib 컨테이너에 한국어 번역을 주입하는 데이터 구동 패처.
기존 코드 재사용: `so3_repack.py`의 decode_index/decompress_slz_payload/compress_slz_mode2/encode_glyph_code/Mclib.parse/render_glyphs/align, `patch_hyda_dialogue.py`의 parse_pk1_table/first_package_boundary/replace_first_pk1_record/conservative_local_codes/rebuild_mclib/tokenize_body 계열.

## 입력
1. 원본 ISO (SHA 핀)
2. `patch_manifest_full.json` — 대상 컨테이너 목록: 각 항목 { archive_id, 대상 스트림 위치(패키지 idx, 레코드 row idx, SLZ 체인 idx, source_offset, iso_offset), file_sha256, 지오메트리, local_base }
3. `translations_full.json` — { text_key → { korean, source_line_count } } + 컨테이너별 { message_id → text_key }
4. `control_sizes_full.json` — 제어 opcode 전수 테이블 (솔버 산출)
5. 폰트 NanumSquareNeo-cBd.ttf (22px, 24셀, 2단계 명암, ink-box 센터링 — Hyda와 동일)

## 메시지 재인코딩 규칙 (Hyda 방식 승계 + 확장)
- 줄 수 보존: 한국어 줄 수 == source_line_count (구조적 꼬리 줄 허용)
- 제어 보존: 8080 개행으로 줄 분리; 줄 내 기타 제어는 비례 위치 재anchoring; 9080/9180(루비) 제거; 9380 처리는 이름 리소스 방침에 따름(전역 patch 시 보존, 아니면 리터럴화)
- 글리프: GLOBAL_CODE_MAP(ASCII·문장부호→글로벌) 우선, 나머지는 로컬 슬롯
- 미해석 opcode 잔존 메시지: 번역 제외(원본 유지) + 리포트
- **픽셀 폭 게이트 [D7]**: 대사 = max(원문 자체 최대폭, 대사예산) ≤576 캡 / UI·메뉴 = 원문 자체 최대폭. 위반 시 빌드 실패 목록 출력 → 번역 수정 루프

## 로컬 아틀라스 재구축
- 전면 재배치(rebuild_mclib 방식): 보호 대상 = 비번역 메시지가 참조하는 로컬 슬롯(보수적 스캔). 전체 패치에선 대부분 메시지가 대상이므로 보호 집합이 작음 → 슬롯 재사용 극대화
- glyph_count==0 컨테이너(2,608): 폭테이블·비트맵 섹션 신설, local_base는 기존 헤더값 유지
- 문자 순서: Unicode 정렬(압축 지역성) + 빡빡한 컨테이너는 비트맵 최근접 이웃 재배열(1245 특례의 일반화, opt-in)
- 32×32(엔트리 66 스태프롤)는 대상 제외 [D1]

## 용량 전략 (gap_map.json 실측 반영, 2026-07-16 개정)
- **실측: 아카이브 간 물리 갭 = 사실상 0 (전체 1개, 0MB). ISO 꼬리 여유 5섹터. aux(third)는 그룹 내 상대 LBA로 추정(76–85 동일 기준점, 3454 aux=0) → 재배치 시 그룹 매핑 파손 위험**
- 따라서 **전략 A/B만 사용. C(섹터 확장)/D(재배치)는 금지** — 모든 재구축 결과는 원본 아카이브 할당 내에 맞아야 함
- A. 레코드 in-place: 새 SLZ ≤ 기존 레코드 할당
- B. 패키지 공동 리플로우: 같은 패키지의 모든 대상 레코드를 동시에 재크기화하고 비대상 레코드는 바이트 동일 상태로 시프트, 패키지의 zero gap 총량 내에서 수용. 패키지 경계 이동 금지
- 부족 시 지렛대 (순서대로): ① 미사용 원본 글리프 제거(재구축 기본) ② 2단계 명암 ③ Unicode 정렬 + 비트맵 최근접 이웃 재배열 ④ **최적 파스 LZSS 압축기**(그리디 대체, 신규 구현) ⑤ 최후: 해당 컨테이너 부분 번역 축소 + 리포트
- 사전 fit 시뮬레이션 리포트 필수 (컨테이너별 old/new compressed, gap 소진)

## 검증기 확장 (verify_full_iso.py)
- 허용 변경 범위 = 대상 아카이브 extents (+ 전략 C/D 시 인덱스·부록 영역), 그 외 바이트 동일
- 컨테이너별: 지오메트리/local_base 불변, 비대상 메시지 논리 동일, 보호 글리프 비트맵·폭 동일
- 콘텐츠 증명: 기대 한국어 charset 재렌더 매칭 + 전 대상 메시지 역디코딩 == korean
- 제어 서명 보존 (9080/9180/9380 정책 제외)
- 픽셀 폭 게이트 전수 재검증
- xdelta 왕복

## 리스크 메모
- 인덱스 aux(third) 의미 불명 — 전략 A/B는 인덱스 불변이라 무위험(v0.4에서 인게임 검증됨). C는 sectors만, D는 LBA도 변경 → 소규모 선행 실험 필요
- 패키지 경계 이동 금지 (엔진이 경계를 어떻게 찾는지 미확인)
- EE RAM 캡처와 대조하여 게임이 실제 읽는 메뉴 세트(76–85 가정) 확증할 것
- E/F/G/S/I 병렬 세트는 불변 유지 (죽은 데이터 가정, 검증 항목)

# build_inventory.py 명세 (v1)

목적: 번역·패치의 데이터 백본. 범위 내 모든 고유 컨테이너의 모든 메시지를 구조화하고,
고유 텍스트 단위(translation unit)로 중복 제거한 산출물을 만든다.

## 입력
- `WS\work\mclib_all_decode\container_catalog.csv` (file_sha256로 고유화, path 사용)
- `WS\work\font_ocr\glyph_mapping_ordered_24.json` / `_32.json` (bitmap sha→unicode)
- `WS\work\full_ko\decode_mclib_text.py` 의 G (글로벌 292 전사표)
- `WS\work\full_ko\control_sizes_full.json` (있으면 로드; 없으면 내장 잠정 테이블:
  솔버 확정 17 + Hyda 프라이어 8280=F1, 8880=F1, 8a80=F4, 9380=F1, 9c80=F3, 9080=Z)
- `WS\work\dialogue_locator\spoken_dialogue_index.csv` (화자 정보 join)

## 범위 [D10]
- 고유 컨테이너 중 일본어([ぁ-ゟァ-ヿ一-鿿]) 포함 24px 컨테이너 전부 (glyph_count==0 포함)
- 제외: 32px(스태프롤), 일본어 없는 컨테이너

## 컨트롤 분류 (config로 유지, ELF 의미 분석 후 갱신)
- INVISIBLE (비례 재anchor 대상): 타이밍/스케일/스타일/이벤트큐 등 기본값 전부
- VISIBLE_INSERT (위치 마커 ⟦n⟧ 필수): 9380(이름, 리터럴화 예정), a180(파티 슬롯),
  숫자/변수 삽입으로 판명되는 계열 (잠정: 8880? — ELF 분석 후 확정, config에 명시)
- NEWLINE 8080, 루비 9080/9180(드롭 예정), 화자 구분자 8780(+8080)

## 메시지 파싱 (컨테이너별)
1. mclib 파스 (so3_repack.Mclib 방식: 정렬 offset 경계)
2. 각 메시지 세그먼트:
   - exact_sha256 (세그먼트 바이트, 트레일링 NUL 포함 — mclib_all_decode와 동일 규약)
   - 토큰화 (완전 제어 테이블). 실패 시 status=untokenizable로 기록만
   - 화자 검출: 8780 80 80 구분자(선행 8980 허용) 앞부분 = 화자 필드.
     화자 모드: literal_glyphs(글리프 나열) / character_reference(9380 포함) / 없음
   - 본문 디코딩: 글리프→유니코드(글로벌 G / 로컬 bitmap-sha→unicode), 8080→\n,
     VISIBLE_INSERT→⟦n⟧ (n=본문 내 순번), INVISIBLE 제어는 텍스트에서 생략,
     미라벨 로컬 글리프→〓 (개수 기록)
   - 줄별 픽셀 폭: Σ advance(폭테이블) × scale 추적 → max_line_px
   - 카테고리: 아래 규칙
3. 컨테이너 요약: 지오메트리, local_base, glyph_count, 전체 메시지 수, JP 메시지 수

## 카테고리 규칙
- menu_* (아카이브 76–135): msgid로 세분 — item_name(50000-54999) item_desc(55000-59999)
  effect(70000-74999) valuable(75000-76999) symbology(5000-6999) surname(5052-5061)
  place(0-1350) 기타 menu_misc
- battle_db (3454), ic (1775), item_flat (6068), system (38)
- event bank (event_bank_catalog.csv에 있거나 paired .bin 존재): spoken(화자 필드 있음) / event_text
- 기타: other_spoken(화자 있음) / other
- width_class: spoken→dialogue (예산 max(own,432), 캡 576) / 그 외→ui (예산 own_max_line_px, 단 own<48이면 48)

## 산출물 (WS\work\full_ko\)
1. `inventory_containers.json` — 컨테이너별 요약 + 메시지 목록(경량: id, offset, sha, text_key, category, max_line_px, speaker 유무, status)
2. `translation_units.jsonl` — 고유 텍스트당 1행:
   { key(=본문 정규화 텍스트의 sha1 12자리), jp_body, jp_speaker(있으면), line_count,
     insert_markers(개수), width_budget_px, width_class, category(다수결), n_occurrences,
     refs:[{file_sha(12), archive, stream, msgid}] (최대 20개 샘플 + 총수) }
   - 본문에 일본어가 없으면 제외. 〓 포함 시 has_unknown_glyph=true로 포함하되 플래그
3. `inventory_stats.json` — 카테고리별/상태별 집계, untokenizable 목록, 〓 포함 통계,
   컨테이너별 fit 예비 데이터(현 압축 크기, 레코드 할당)
   ※ fit 데이터: stream_manifest.csv에서 compressed/allocation 참조 가능하면 포함, 아니면 생략
4. 재실행 가능: control_sizes_full.json 갱신 후 같은 명령으로 전체 재생성

## 검증
- 메시지 총수가 mclib_all_decode 리포트(183,065 고유 컨테이너 기준)와 일치
- spoken_dialogue_index와 화자 검출 결과 교차 검증 (불일치 목록 리포트)
- exact_sha256 → unique_exact_segments.csv 대조 (표본)

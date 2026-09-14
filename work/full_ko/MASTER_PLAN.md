# SO3 DC 전체 한글패치 마스터 플랜 (작성 2026-07-16)

사용자 지시: 모든 대사·메뉴·아이템명 등 일본어 전부 한글화. 대사창 폭 등 규칙 준수. 질문 없이 자율 완수.
이 문서는 컨텍스트 압축에 대비한 영속 상태 기록. **진행하며 갱신할 것.**

## 경로
- 작업 루트: `C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2` (이하 WS)
- 저장소: `WS\publish\so3dc-korean-tools` → github.com/snake7594/so3dc-korean-tools (main aa4e3cf, v0.4.0-alpha.1 릴리즈됨)
- 원본 ISO: `D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso` (SHA256 95CC4E25..., 4,689,854,464 bytes)
- Disc2 ISO 동일 폴더. 숨김 인덱스 동일 포맷 확인(6144 엔트리, 5715 비어있지 않음) → 파이프라인 재사용 가능
- 전체 패치 산출물 폴더: `WS\work\full_ko\`
- 폰트: `D:\ps2\NanumSquareNeo-cBd.ttf` (SHA256 4749FA56...)

## 데이터 인벤토리 (이미 존재, 재구축 불필요)
- `WS\work\full_unpack\disc1\manifests\archive_manifest.csv` (6,144 아카이브) / `stream_manifest.csv` (61,964 스트림, ISO 오프셋·SLZ 모드·크기) / decoded\ 전체 해제 파일
- `WS\work\mclib_all_decode\container_catalog.csv` — mclib 7,786 발생(고유 1,516), 지오메트리·local_base·매핑 수
- `WS\work\mclib_all_decode\unique_exact_segments.csv` — 37,814 고유 세그먼트 (exact_sha256 기준, 발생 364,143)
- `WS\work\dialogue_locator\all_dialogue_targets_ja_v2.json` (86.5MB) — **마스터 일본어 덤프**: 고유 컨테이너 1,516 × 메시지 183,065, decode_status: complete 116,369 / partial_unmapped_glyph 15,616 / unresolved_control 51,080
- `WS\work\dialogue_locator\font_mapping_table.json` (65MB) — 전 슬롯 code→Unicode (171,001/184,433 확정, null 13,387은 비한자)
- `WS\work\dialogue_locator\spoken_dialogue_index.json` — 고신뢰 화자 대사 13,499 (9380 id표: 1페이트 2소피아 3마리아 4클리프 5넬 6알벨 7로저 8아드레이 9스프레 10미라쥬)
- `WS\work\event_text_classifier\event_text_pool.csv` — 이벤트 뱅크 메시지 31,046, 6개 병렬 언어 뱅크 중 일본어 뱅크 1,248개(event_bank_catalog.csv), .bin 직접 표시 참조 12,939
- `WS\work\font_ocr\` — 글리프 라벨 검증 데이터 (한자 4,316형→2,272자, 잔여 미확인 한자 0)

## 확정된 포맷 지식
- 숨김 인덱스: 0x200000, 6144×3 u32 XOR(SEED 0x13578642; LBA후 key^=key<<1, size후 key^=~SEED, aux후 key^=(key<<2)^SEED — 대칭이라 인코더 작성 가능. 현재 인코더 없음)
- SLZ: "SLZ"+mode u8 / u32 compressed / u32 unpacked / u32 next_rel. 모드 0store 1LZSS 2LZSS+RLE 3LZSS16. compress_slz_mode2 존재(그리디)
- mclib: 헤더 0x80; @0x10 테이블, @0x14 텍스트, @0x18 폭테이블, @0x1C 비트맵, @0x20 글리프수, @0x38 local_base, @0x3C 레코드수, @0x40 파일크기. 메시지 경계는 정렬된 고유 offset. 글리프 코드 = LE 7bit varint, code<base→글로벌(1.72, 292슬롯), code>=base→로컬. base 301(7,783개) / 1(3개)
- 제어코드: **전수 확정 (control_sizes_full.json, ELF 3중 교차검증, 183,065/183,065 토큰화 성공)**. 엔진 디코드 @0x462798: code&0x4000→제어, index=code-0x4000, index≥0x24는 무피연산 스킵. 구조: 8080 개행 / 8180 페이지 브레이크. 삽입(위치 보존 필수): 9280 레지스트리 문자열, 9380 캐릭터명(id— 8=스프레 9=아드레이 정정), A180 파티슬롯명, A280/A380 아이템명, 9C80 zero-terminated 페이로드(기존 패처의 5byte는 오류였음). 스타일: 8880 색 설정(+8980 리셋) 위치 보존. 비가시: 8280 속도, 8580/8680/9480/9580 float, 8A80 스케일 x·y, 8C80 자간, 8E80 행간, 9E80/9F80 이벤트큐 쌍, 각종 플래그. 루비 9080/9180 드롭. Disc2는 자체 0002.sle → solve_controls.py --finalize 재실행 필요
- 24×24 4bpp(low nibble 먼저) 288B/글리프, 폭테이블 1B/글리프. 32×32는 스태프롤 전용(아카이브 66) → **번역 제외 결정**

## 기존 패처의 Hyda 한정 요소 (범용화 시 변경 목록)
1. BANK_STREAMS 24개 화이트리스트 (patch/build/verify 3곳 중복) → 매니페스트 구동으로
2. 카운트 핀 434/653/24 → 매개변수화
3. 타깃 = 첫 PK1 패키지 1행 (DCMS,0), mode2 unchained 가정 → 임의 패키지/행/체인/중첩 지원
4. rebuild_mclib (24,24,24) 지오메트리 게이트 → glyph_count==0 컨테이너(2,608개)의 섹션 신규 생성 지원
5. 성장 = 첫 패키지 zero gap 내 리플로우만 → 패키지별 리플로우 + (부족시) 아카이브 재배치/ISO 확장 + 인덱스 인코더
6. GLOBAL_CODE_MAP (ASCII·문장부호→글로벌 코드 재사용) 은 유지
7. 픽셀 폭 게이트 신규 구현: 라인 폭 = Σ advance(글리프) × scale(8580/8680/8A80 float 보존값). 예산은 원문 통계에서 도출 예정
8. 아카이브 1245 글리프 재배열 특례 → 필요시 전 뱅크 옵션화

## 결정 로그
- [D1] 스태프롤(32×32, 아카이브 66, 1,220메시지)은 인명 위주 → 번역하지 않음
- [D2] 글로벌 아틀라스는 원본 유지(여유 57바이트뿐), 한글은 전부 로컬 아틀라스
- [D3] 용어집: 일본판 원명 기준 + 나무위키 통용 표기 (glossary_draft.json)
- [D4] Disc 1 완성 후 Disc 2 확장
- [D5] 번역 단위 = exact_sha256 고유 세그먼트, 뱅크 문맥과 함께 번역
- [D6] 문체: hyda_ko.json 기존 434개와 일관 (「 유지, 캐릭터별 말투)

## 파이프라인 단계 (작업 태스크 대응)
1. ✅ 도구 분석 (#1)
2. 제어 opcode 전수 규명 (#9, 에이전트) → 전 메시지 토큰화
3. 화자명 리소스 (#10, 에이전트) / 메뉴·아이템 위치 조사 (에이전트)
4. 인벤토리·분류 (#2): 일본어 포함 + 게임이 읽는 뱅크 전수 목록, 대사/메뉴/아이템/시스템 분류
5. 용어집 (#3): 아이템·스킬·지명 전수 확장
6. 번역 (#4): Workflow fan-out, 규칙 = 줄수 보존·픽셀 폭 게이트·제어 보존·용어 일관
7. 범용 패처 (#5) + 검증기 확장
8. 빌드·정적 검증 (#6) → 릴리즈 (#8) → Disc 2 (#7)

## 화자명(9380) 시스템 규명 (name_resource_findings.md 상세)
- 9380 이름 원천 = 0002.sle member 1 (VA 0x3E7C80) 내 반각 가타카나 테이블 (VA 0x5038A8–F7, 8B/slot, 역순 addr=0x5038F8−8×id). 부본 2곳: decoded\0068\s000061(+0x9080), decoded\1069\s001679(+0x153718, id9에 개발 잔재 ｲｻﾞｰｸ)
- 런타임 변환기(VA 0x465550): 반각 바이트→글로벌 글리프 코드('0-9'→1-10, 'A-Z'→14-39, 'a-z'→40-65, 반각 가나→가나 글로벌 슬롯)
- **ID 정정: 8=스프레, 9=아드레이** (프롬프트에서 8/9 스왑됐던 것 — spoken_dialogue_index.json이 정답)
- A180 = 파티 슬롯 동적 이름 (리터럴화 불가능한 진짜 동적 참조)
- [D11] 방침: ① 대사 화자란·본문 9380 → 한글 리터럴화 (Hyda 방식, 주 노선) ② 하드코딩 테이블 3곳 재작성 — 1차안: 가나 바이트로 쓰고 해당 가나 글로벌 슬롯 ~30개를 이름용 한글 비트맵으로 교체(전 텍스트 한글화로 가나 슬롯이 유휴화되는 것을 활용, 변환기 무수정) / 예비안: 라틴 이름(Fayt, Sophia, …, Peppita(id8), Adray(id9), Mirage — 전부 7자 이내 확인) ③ 0002.sle는 SLZ mode3 재압축 필요 → mode3 압축기 확장 또는 예비안 채택. 성명 성(姓) 메시지(5052-5061)·이름 목록(1076:1945 144-153)·전투 이름 뱅크(3454:60029 580-589)는 일반 번역 대상
- 대사 화자란 9380 사용: 6,449건 (id1 2,771 … id9 91)

## 픽셀 폭 규칙 (width_stats.json, 발생 기준 400,737 라인)
- 원문 라인 폭 분포: p50=24 / p90=216 / p95=244 / p99=432 / p99.9=1100 / max=3099 px
- [D7-final] 인벤토리 정정 분포(대사 own p50=332/p90=447/p95=472/p99=516/max=831): **대사 = max(원문 자체 최대폭, 480px)** / **UI = 원문 자체 최대폭 (엄격, 소형 라벨 하한 48px)**. (기존 width_stats.py의 절대오프셋 버그로 구버전 수치 폐기)
- 폭 계산: Σ advance(글리프별 폭테이블) × scale(858x/8A80 float 추적). 한글 글리프 advance = round(getlength(ch)@22px) ≤24

## 텍스트 저장 위치 지도 (메뉴/아이템 조사 완료)
- 디스크 전체 텍스트는 전부 mclib 글리프 코드. Shift-JIS 게임 텍스트 없음. ELF/IRX에 게임 텍스트 없음.
- 메뉴·아이템·시스템: 엔트리 76–85 (10개 컨테이너, ~19.1k msgs) — 아이템명 751(ID 50000+), 아이템설명 751(55000+), 효과 704(70000+), 지명 999+시설 350(엔트리82/92…, ID 0–1350), 스킬메뉴 444, 문장술 154, 설정 117, 택틱스 80, 상점 48, IC 188, 여행 81, 사전, 메모리카드
- ~~86–135는 언어 병렬 세트~~ **정정(2026-07-16 재검증): 76–135는 전부 일본어인 6개 준중복 메뉴 세트** (아이템 50014=ブルーベリィ 전 세트 동일, 세트간 메시지 수 미세 차이). 챕터별 스트리밍 복제 추정 → **60개 전부 패치** [D9]
- [D10] 패치 범위 = 일본어 텍스트를 포함한 모든 mclib 컨테이너 (24px, glyph0 포함), 예외: 아카이브 66 스트림 57(32px 스태프롤). 이벤트 뱅크 twin 포함 최대 커버리지
- 전투 DB: 엔트리 3454 (스트림 60027–60030) — 전투스킬 261, 적 이름 646, 액세서리 500, AI 지시 499 등 2,774
- IC/발명: 엔트리 1775 — 발명 아이템명 1,000, 마을 목록 622 등 3,988
- 아이템 픽업 플랫 테이블: 엔트리 6068 (ID 1–1526)
- 부트/시스템 다이얼로그·트로피: 엔트리 38 (스트림 19 base301, 스트림 20 base1 특수 15msgs) 492
- 스태프롤: 엔트리 66 스트림 57 (32px, 번역 제외 [D1])
- FIS 텍스처(엔트리 8 SHI UI, 11 ANKF HUD, 42 타이틀): 그래픽 텍스트 — 선택 과제로 보류 [D8]
- 이벤트 뱅크: JP 뱅크 1,248(고유 796). 아이템 표기 주의: ベリー가 아니라 ベリィ
- **전체 디코딩 덤프**: `WS\work\full_ko\full_text_dump.tsv` (30MB, 183,065 msgs) + `full_decode.py` (완전 제어코드 세트 포함)
- 추가 확인 제어코드: 8880+u8 스타일(3), 8680+4B(6), 8780/8480/8180 무피연산(2), 8580+f32 타이밍(6)

## 빌드 단계 진행 (2026-07-18)
- 번역 302배치 사실상 완료(20,589 유닛). 커버리지 격차 없음(미유닛 917키=893 비일본어+24 미표기글리프 엣지). 배치 282는 분할본 351–354로 커버.
- build_full_plan.py → patch_plan_full.json ({"unique":{file_sha:{msgid:{korean,speaker_korean}}}}), 1,350 컨테이너/152,611 메시지.
- **fit 시뮬레이션 실패 454→132로 감소**: parse_translated_text 페이지 파서 수정(인벤토리 `\n⟦P⟧` 규약의 유령 빈 줄 정리)으로 line_count 실패 331건 전부 해결. 패처 테스트 13/13 유지.
- **남은 132 실패 = 3범주**: ① 용량초과 91 아카이브(최적압축+비트맵재배열 자동재시도 후에도 64~3652B 초과, 대부분 <2KB) ② 변형화자마커 고유2유닛(9de782ba3371,d804565000d3; 인벤토리가 화자필드로 전달, 패처는 본문마커 기대) ③ 동적토큰 고유2유닛(6737081170be,8d7bf7e3f9cc; ⟦문자열#1c⟧ 토큰 인코더 미인식).
- 대응: fit-repair(용량) + 포맷픽스(4유닛) 에이전트 위임. sim_report.json/sim_fail2.py에 상세.
- **2026-07-18: 132 실패 전부 해소 → `archives_failed: 0` 달성** (플랜 시뮬 클린). 파이프라인 `build_full_plan.py → plan_fixups.py → fit_repair.py → CLI 시뮬`.
  - 포맷픽스(plan_fixups.py, 14엔트리 0드롭): ① 동적토큰=레지스트리삽입 화자 12엔트리(MEMORY CARD 컨테이너 3개 430a/68108/e82d) — 9280삽입이 8780 앞 prefix에 있어 본문마커화 불가 → `keep_speaker=True`로 헤더(9280포함) 원본보존 + 본문 번역(드롭보다 커버리지↑, 패턴검출로 12개 전수). ② 변형화자마커 2유닛(9de/d804) — `⟦1⟧이름⟦2⟧` 첫줄 재구성 + speaker 제거. 둘 다 rebuild_container 역디코딩 증명 통과. 포맷실패 41아카이브(변형36+동적5) 전부 해소, 신규실패 0.
  - 용량수리(fit_repair.py, 231메시지 드롭=0.151%): 자체 내부 시뮬로 91 용량실패 아카이브 식별 후, 패키지별 greedy drop-to-fit(최고 (고유로컬글리프수, 텍스트길이, msgid) 메시지 제거, 매 드롭 후 재계산). 챕터쌍 공유컨테이너는 공유 dict로 1회 축소→쌍둥이 동반해결(91중 45만 명시적 드롭). coverage_report.json에 전 드롭 기록.
  - 최종: 152,611계획 → **152,380 번역(99.849%)**, 231 용량드롭·0 포맷드롭. 패처 무수정, 테스트 13/13 유지. 백업 patch_plan_full.raw.json(원본)/prefix.json(포맷픽스후·수리전).

## 상태 (갱신할 것)
- 2026-07-16: 분석 3종 완료(파이프라인/데이터/메뉴·아이템). width_stats 완료. 전체 텍스트 덤프 확보.
- 2026-07-16 저녁: ✅ SLZ 최적 파스 압축기 완성(slz_optimal.py, +2.85% 평균, 전 라운드트립 통과, 포맷 발견: match cap 17·RLE-short run≥4). ✅ 화자명 시스템 규명(D11). 진행 중: opcode ELF 교차검증(잔여 13계열), 용어집 워크플로(14/27에서 재개), 인벤토리 빌더(INVENTORY_SPEC.md 기반 구현 위임).
- 2026-07-16 밤: ✅ **번역 배치 도구 완료**: prepare_translation_batches.py → tr_batches\ 300배치(대사 244: 전부 40–80 units, UI 56: 카테고리별 ≤140) + tr_batches_index.json. 커버리지 20,578/20,578(각 유닛 정확히 1회, 첫 ref 컨테이너 기준). 유닛별 per_line_orig_px(첫 ref 재계산)·budget_px(대사 max(own,480)/UI max(own,48))·char_budget_hint(÷22)·pages·marker family 포함. 용어집 서브셋(merged 2,933어, 최장우선 캡200/배치)+대사 배치에 core_names 52. **주의: "<12 유닛 동일 아카이브 병합" 규칙은 완화** — 실데이터가 589 아카이브 중앙값 1컨테이너/8유닛이라 아카이브 경계 넘어 (archive,stream) 연속 병합(컨테이너 유닛열은 연속 유지). glossary_full.json 완성 후 같은 명령으로 재생성. ✅ validate_translations.py: 커버리지/줄구조/⟦P⟧위치/마커 순서·잔존 ⟦이름:⟧/화자/폭(글로벌 1.72 원본 폭표+Nanum22 clamp[1,24], 동적마커 허용치 9280·a180 +96px, a280·a380 +144px)/용어집(경고)/일어잔존(・ U+30FB는 허용) — 합성 테스트 3+3건 전 검출 확인, exit 0=무오류.
- 2026-07-16 저녁: ✅ **인벤토리 완료**: build_inventory.py (제어표 확정판 control_sizes_full.json 36계열 사용) → inventory_containers.json(범위 1,350 컨테이너/174,050 msgs; 제외 165 비일본어+1 스태프롤) + translation_units.jsonl(**20,578 units**; dialogue 11,655/ui 8,923) + inventory_stats.json. 183,065/183,065 전 메시지 클린 토큰화(untokenizable 0). exact_sha256 37,814/37,814 완전 일치(오프셋은 텍스트블롭 상대 확정). 화자 교차검증 14,041/14,145 일치(잔여 104는 인덱스 측 미해독분을 본 빌드가 해독). 마커: 8880/8980 색상 스팬 포함 ⟦n⟧ 번호화 9,189 units, ⟦이름:⟧ 6,171 units, 루비 710 units. 주의사항: ① 화자 변형 패턴(8780 없이 8880+9380+8980+8080, 고유 3msgs/발생 211)—2차 패턴으로 검출 ② width_stats.py는 세그먼트를 절대 오프셋으로 읽는 버그—432 예산 재도출 권장(신규 데이터로는 dialogue own>432가 1,429/11,655, >576은 2뿐 → D7 골격 유지 가능) ③ 、/。는 G 전사표상 ヽ/゜로 표기됨(파이프라인 전체 일관, 번역·재인코딩 시 원 문장부호로 취급) ④ 〓 잔존 13,838자/5,049msgs(1,639 units 플래그).
- 2026-07-16 밤: ✅ **범용 풀디스크 패처 코어 완성** `work/full_ko/so3_full_patch.py`(1,665줄) + `test_so3_full_patch.py` 11테스트 전부 PASS (상세: patcher_progress.md).
  - T1 identity: 34컨테이너 17,512msgs PUA 바이트 일치(드롭 제외) + real-text 16,978msgs 컨트롤 서명 보존. positional 전 계열(8880/8980/9280/9C80/A180/A380) 4,680 마커 바이트 재현
  - T2 hyda 회귀: 신규 파이프라인으로 ISO 빌드 → **구 verify_hyda_dialogue_iso.py 무수정 통과**(653건, 전략 A×19/B×5, 외부 diff 0). 레거시 어댑터 = positional=∅ + structural_page=False
  - T3 용량: 멘유 60 + 3454/1775/6068/38 의사 한글화 107,905/125,113msgs → **64아카이브 전부 verdict A(in-place)**, 압축 합계 -44% → **용량 GO**. 레버 = 미사용 글리프 zero-fill(기본) + slz_optimal
  - 포맷 추가 발견: 3454는 최상위 "PACK" 테이블(count u16@6, hs u32@8=0x80, aux0@0xC, (offset,aux)쌍@0x10+8k, 레코드 크기 암시적, 0x80 정렬) — 오프셋 재작성 리플로우 구현·검증. hyda 대사 600/653이 다중 페이지(8180 본문 중간+가시 텍스트)
  - 패처 인터페이스: plan={archive:{(pkg,row,chain):{msgid:{korean,speaker_korean,keep_speaker}}}}, build_plan()이 stream_manifest 기반 (archive,stream)→주소 해석, expand_unique_to_occurrences()가 file_sha→전 발생 매핑. CLI `--simulate` 지원
  - 인터페이스 주의: ①인벤토리의 화자 변형 패턴(8780 없는 8880+9380+8980, 3msgs/211발생)은 패처에선 본문 마커(⟦1⟧이름⟦2⟧)로 넘겨야 함(speaker_korean 주면 에러—fail-closed) ②픽셀 폭 게이트는 validate_translations.py 담당(패처 미구현) ③(38,20) base-1 특수글리프 15msgs 라벨 필요

- 2026-07-17: ✅ 용어집 확정(glossary_full.json 2,933어, 검수 343건 적용). ✅ 글리프 라벨링(416 high+32 med, 잔여는 ⟦G:sha8⟧ 보존 토큰화 — build_inventory/validator 반영, 〓 제거). ✅ 인벤토리 재생성(20,654 유닛). ✅ 배치 재생성(302개: 대사245+UI57, 확정 용어집 임베드). 🚀 본문 번역 메가 워크플로 가동(run wf_53593a1e-38a, 302 에이전트, 배치별 자가검증 루프). 🚀 독립 전체 검증기(verify_full_iso.py) 커미션. 다음: 번역 완료 → 글로벌 감수 → 플랜 빌드·시뮬레이션 → ISO 빌드 → 이름 테이블(#11) → 릴리즈.
- 2026-07-17: ✅ **[D11-②] 하드코딩 이름 테이블 패치 완성** `work/full_ko/so3_name_patch.py` + 테스트 13개 전부 PASS (상세: name_patch_progress.md). 1차안(가나 슬롯 재용도) 채택: 21음절→가나 바이트 재철자 + 글로벌 1.72의 해당 21슬롯 한글 교체(변환기 무수정). 신규 SLZ mode-3(LZSS16) 토큰최소 인코더로 0002.sle member1(571,490/할당 571,948)·1069 s001679(667,496/669,680) 모드 보존 재압축, 폰트 22,788/24,544, 0068 17,202/18,416 전부 fit. VA 0x507718/20 리터럴은 strcpy 전용 확인 후 함께 패치, 1069 id9 ｲｻﾞｰｸ 잔재 교정. 엔드투엔드(원본 사본, 4 extent 밖 diff 0, 부정 테스트) 통과. --fallback-latin 동봉. **본편 메시지 패치 후 중간 ISO에 적용할 것.**
- 2026-07-17: ✅ **패처 ⟦G:sha8⟧ 보존 비트맵 토큰 지원** (so3_full_patch.py, 테스트 13개 전부 PASS): 원본 컨테이너 비트맵 sha 8-hex 접두 해석(0/다중매치 fail-closed), 원 슬롯 protected 편입으로 제자리 보존(비트맵+폭 불변, 복사·신규 슬롯 불필요, dedupe 자동), 줄 내 가시 유닛, 화자란도 허용. container_code_to_char(unlabeled_g_tokens=True)로 디코드 측 토큰화. (38,20) base-1 15msgs 전체 검증. 화자 변형 패턴 3msgs(1070:1722:35, 1070:1727:35, 1921:31233:7)는 본문 마커 "⟦1⟧이름⟦2⟧" 전달로 인코딩 확인(speaker_korean 주면 에러).
- 2026-07-17 밤: ⚡ 번역 속도 개편. 병목=4코어라 워크플로당 동시실행 2개로 제한됨. 해결: (1) validate_one.py 단일배치 검증(0.14s, 기존 전수검사 대체) (2) batch_status.py 완료분 전수 점검 (3) so3-tr-shard.js 샤드 워크플로 — pending 237배치를 10샤드로 라운드로빈 분할, 10개 워크플로 동시 실행 → 실질 ~20 병렬. 모델=Sonnet 고정(Fable5/Opus 한도 회피). ⚠️ 발견: 기존 완료분 batch_id가 문자열("batch_0001")이라 검증 우회됨 → validate_translations.py에 normalize_batch_id 추가됨(사용자 백그라운드 작업). batch_status 결과: 완료65 클린/2 폭위반/235 미착수. 샤드 파일=shards.json. 재개법: batch_status.py→shard_pending.py→각 샤드 Workflow(args=배치리스트). 남은 빌드체인(플랜 시뮬→ISO빌드→이름패치→verify_full_iso→xdelta릴리즈) 전부 준비완료.

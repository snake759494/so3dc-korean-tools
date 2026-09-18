# 소스에서 직접 빌드하기 (v1.2.1 재현 가이드)

> **현재 상태 (2026-09-18)**: 저장소를 옮기면서 최종 패치 플랜
> (`patch_plan_full.json`, `patch_plan_d2.json`)과 일부 데이터를 복원하지
> 못했습니다. 그래서 아래 절차는 **4단계(텍스트 패치)부터 진행할 수
> 없습니다.** 1~3단계(언팩·카탈로그)와 6단계의 이미지 리페인트는 복원한
> 도구로 동작합니다. 자세한 내용은 [`RECOVERY_STATUS.md`](../RECOVERY_STATUS.md)를
> 참고하세요. 패치 적용은 릴리스 xdelta를 쓰세요.

이 문서는 **이 저장소와 본인 소유의 원본 디스크 덤프만으로** Star Ocean 3
Director's Cut 한국어 패치 ISO(v1.2.1과 동일)를 처음부터 빌드하는 절차를
설명합니다. 릴리스 xdelta를 적용하는 간편 경로는 [README](../README.md)를
보세요. 이 가이드는 xdelta 없이 전체 파이프라인을 직접 돌리는 경로이며,
번역을 수정해 자기만의 빌드를 만드는 방법(부록 A)도 다룹니다.

이 저장소에는 재현에 필요한 **모든 도구·번역·설정 데이터**가 들어 있습니다.
게임에서 추출한 데이터(원문 텍스트, 리소스, 픽셀)는 저장소에 없으며, 전부
본인의 원본 ISO에서 로컬로 재생성합니다.

## 0. 준비물

| 항목 | 요구 사항 |
|---|---|
| OS | Windows 10/11에서 검증(경로 표기도 Windows 기준). 도구는 전부 Python/.NET이라 다른 OS도 이론상 가능하나 미검증 |
| Python | 3.10 이상 (3.13.1에서 검증) + `pip install -r requirements.txt` (Pillow, numpy) |
| .NET SDK | 6.0 (언팩커 `So3Unpack` 빌드·실행용) |
| xdelta3 | 선택 — 배포용 패치 파일을 만들 때만 필요 |
| 디스크 공간 | 디스크당 언팩 산출물 약 12GB + 중간 ISO 4.7GB×2 (여유 40GB 권장) |
| 원본 ISO | 일본판 Director's Cut, 본인이 적법하게 소유한 덤프. Disc 1 (SLPM-65438): `4,689,854,464` bytes, SHA-256 `95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826` / Disc 2: `4,685,955,072` bytes, SHA-256 `349FF9443E3ACFD92045C5DFAD6D97EB51D2765B0781BCD59DCD424D81670288` |
| 글꼴 | NanumSquare Neo Bold `NanumSquareNeo-cBd.ttf`, SHA-256 `4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767` — [네이버 한글캠페인 공식 배포처](https://hangeul.naver.com/font)에서 "나눔스퀘어 네오" TTF 묶음을 내려받아 그 안의 `NanumSquareNeo-cBd.ttf`를 사용 (OFL 1.1) |

SHA-256 확인은 PowerShell `Get-FileHash <파일> -Algorithm SHA256`.
**해시가 다른 덤프·글꼴에는 절대 진행하지 마세요** — 파이프라인 곳곳의
fail-closed 검증이 중단시키며, 강행하면 결과 보증이 없습니다.

## 1. 저장소 클론 = 작업 폴더

```powershell
git clone https://github.com/snake759494/so3dc-korean-tools.git
cd so3dc-korean-tools
pip install -r requirements.txt
```

**클론 루트가 곧 워크스페이스입니다.** 모든 도구는 자기 위치에서 워크스페이스를
자동 인식하므로 환경 변수 없이 그대로 동작합니다. 산출물을 다른 곳에 두고
싶을 때만 아래 환경 변수를 쓰세요.

| 환경 변수 | 의미 (기본값) |
|---|---|
| `SO3_WS` | 워크스페이스 루트 (클론 루트) |
| `SO3_FONT` | 글꼴 경로 (`D:\ps2\NanumSquareNeo-cBd.ttf`) |
| `SO3_ISO_D1` | 디스크 1 원본 ISO 경로 (일부 보조 도구의 기본값) |
| `SO3_XDELTA` | xdelta 실행 파일 경로 |
| `SO3_NAME_PATCH_CONFIG` | 이름 패치 설정(JSON) — 디스크 2 검증기 연동용 |

아래 예시는 원본 ISO가 `D:\ps2\`에, 글꼴이 `D:\ps2\NanumSquareNeo-cBd.ttf`에
있다고 가정합니다. 다른 경로면 인자만 바꾸면 됩니다. 이 세션 전체에서:

```powershell
$env:SO3_FONT = "D:\ps2\NanumSquareNeo-cBd.ttf"
$D1 = "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso"
$D2 = "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 2).iso"
```

## 2. 언팩 (디스크당 1회)

숨겨진 tri-Ace 아카이브(암호화 인덱스 0x200000)를 풀어 매니페스트와 디코딩된
리소스를 만듭니다. 디스크당 20분 안팎, 약 12GB.

```powershell
dotnet run -c Release --project unpacker/So3Unpack.csproj -- "$D1" "work/full_unpack/disc1" --raw-mode full --decoded-mode all --resume --no-json
dotnet run -c Release --project unpacker/So3Unpack.csproj -- "$D2" "work/full_unpack/disc2" --raw-mode full --decoded-mode all --resume --no-json
```

끝나면 `work/full_unpack/disc1|disc2/` 아래에 `manifests/stream_manifest.csv`,
`raw/`(0002.sle 등 아카이브 원본), `decoded/`(SLZ 해제 리소스)가 생깁니다.
중단돼도 `--resume`으로 이어집니다.

## 3. 컨테이너 카탈로그

텍스트를 담은 `so3mclib` 컨테이너의 위치·구조 카탈로그를 만듭니다.
(디스크 1 카탈로그는 도구들의 기본 경로인 `work/mclib_all_decode/`에 둡니다.)

```powershell
mkdir work\mclib_all_decode -Force
python work/full_ko_d2/build_container_catalog.py --stream-manifest work/full_unpack/disc1/manifests/stream_manifest.csv --disc-root work/full_unpack/disc1 --out work/mclib_all_decode/container_catalog.csv --no-glyph-gap
python work/full_ko_d2/build_container_catalog.py
```

두 번째 명령(디스크 2, 기본값)은 `work/full_ko_d2/container_catalog.csv`와
글리프 커버리지 리포트를 만듭니다. 기대값: 디스크 1 7,786행/1,516 고유,
디스크 2 7,578행/1,488 고유.

## 4. 텍스트 패치 (본 패치)

저장소의 **최종 패치 플랜**(`patch_plan_full.json` / `patch_plan_d2.json` —
번역·폭 보정·용량 보정이 전부 반영된 완성본)을 원본 ISO에 적용합니다.
디스크당 수십 분(SLZ 최적 재압축이 CPU를 많이 씁니다).

```powershell
# 디스크 1 — 시뮬레이션(쓰기 없음, archives_failed=0 확인) 후 실제 빌드
python work/full_ko/so3_full_patch.py "$D1" --plan work/full_ko/patch_plan_full.json --simulate --report work/full_ko/sim_rebuild.json --font $env:SO3_FONT
python work/full_ko/so3_full_patch.py "$D1" "D:\ps2\rebuild_d1_msg.iso" --plan work/full_ko/patch_plan_full.json --report work/full_ko/build_rebuild.json --font $env:SO3_FONT

# 디스크 2 — 카탈로그·매니페스트를 디스크 2 것으로 지정
python work/full_ko/so3_full_patch.py "$D2" "D:\ps2\rebuild_d2_msg.iso" --plan work/full_ko_d2/patch_plan_d2.json --catalog work/full_ko_d2/container_catalog.csv --stream-manifest work/full_unpack/disc2/manifests/stream_manifest.csv --report work/full_ko_d2/build_rebuild.json --font $env:SO3_FONT
```

기대값: 디스크 1 아카이브 1,006/1,006 성공·실패 0, 디스크 2 973/973·실패 0.
ISO 크기는 절대 변하지 않습니다.

## 5. 이름 패치

엔진 코드 모듈(0002.sle)의 하드코딩 이름 테이블 3벌과 글로벌 폰트의 21개
슬롯을 한글화합니다. 언팩 산출물(`work/full_unpack/disc1`의 참조 파일)을
사용하므로 2단계가 끝나 있어야 합니다.

```powershell
# 디스크 1
python work/full_ko/so3_name_patch.py "D:\ps2\rebuild_d1_msg.iso" "D:\ps2\rebuild_d1_named.iso" --font $env:SO3_FONT --report work/full_ko/name_rebuild.json

# 디스크 2 — 아카이브 위치 오버라이드 설정 파일 지정
python work/full_ko/so3_name_patch.py "D:\ps2\rebuild_d2_msg.iso" "D:\ps2\rebuild_d2_named.iso" --font $env:SO3_FONT --config work/full_ko_d2/name_patch_d2_config.json --report work/full_ko_d2/name_rebuild.json
```

리포트의 `ok: true`와 "3 tables decode the 10 Korean names"를 확인하세요.
중간 ISO(`*_msg.iso`)는 이제 지워도 됩니다.

## 6. 이미지(텍스처) 패치

픽셀로 그려진 일본어 10곳(고정 텍스트 3 + 디스크 메시지 John 사본 3 +
필살기 라벨 4)을 한글로 다시 그립니다. 리페인트는 **디스크 1 원본에서 1회**
생성하고(두 디스크의 해당 텍스처는 바이트 동일), 두 디스크에 적용합니다.
디스크별 적용은 각각 **한 번씩만** 실행하세요(이미 패치된 ISO에 다시 적용
하면 검증기가 정상적으로 이를 잡아내 실패로 보고합니다).

```powershell
# 6-1. 텍스처 카탈로그 (디코드; 디스크당 수 분)
python work/img_ko/decode_textures.py
python work/img_ko/decode_textures.py --disc-root work/full_unpack/disc2 --out work/img_ko_d2

# 6-2. 리페인트 생성 (디스크 1 원본 ISO에서 읽음 → work/img_ko/patched/*.slz 8종)
$env:SO3_ISO_D1 = $D1
python work/img_ko/repaint_all.py

# 6-3. 디스크 1 적용: 10곳 일괄(적용 + 독립 검증까지 한 번에) = 최종 ISO
python work/img_ko/john_supplement_d1_apply.py --input "D:\ps2\rebuild_d1_named.iso" --output "D:\ps2\SO3_DC_Disc1_Korean_Full_rebuild.iso" --report work/img_ko/apply_rebuild.json --verify-report work/img_ko/verify_img_rebuild.json

# 6-4. 디스크 2 적용: 전사 플랜으로 10곳 일괄 (John 3벌 포함) = 최종 ISO
python work/img_ko/apply_image_patch.py --input "D:\ps2\rebuild_d2_named.iso" --output "D:\ps2\SO3_DC_Disc2_Korean_Full_rebuild.iso" --transfer-plan work/img_ko_d2/transfer_plan.json --catalog work/img_ko_d2/texture_catalog.json --report work/img_ko_d2/apply_rebuild.json
```

## 7. 결과 확인

```powershell
Get-FileHash "D:\ps2\SO3_DC_Disc1_Korean_Full_rebuild.iso" -Algorithm SHA256
Get-FileHash "D:\ps2\SO3_DC_Disc2_Korean_Full_rebuild.iso" -Algorithm SHA256
```

같은 원본·같은 글꼴·같은 Pillow 버전(`requirements.txt`에 고정)이면 공식
v1.2.1과 **바이트 단위로 동일**해야 합니다:

- Disc 1: `E31911EF099F3BDB46928DEA9420F745B77A3FC34DDF3BA3A3F272EC2E1E01BA`
- Disc 2: `753FE9209A82EFAD27365D4E660F263D19448D166274A027B00C322C9C0C65FF`

이 절차는 실제로 **깨끗한 클론에서 두 디스크 모두 위 해시가 재현됨을 확인해
검증했습니다**(2026-08-05, Windows 11 / Python 3.13.1 / Pillow 11.1.0 /
.NET SDK 6.0). 중간 단계 해시도 릴리스 빌드와 전부 일치했습니다:

| 단계 | Disc 1 | Disc 2 |
|---|---|---|
| 4. 텍스트 패치 | `3F614030…` (아카이브 1,006/1,006) | `01350454…` (973/973) |
| 5. 이름 패치 | `1B48EDFE…` | `1FA9A528…` |
| 6. 이미지 패치(최종) | `E31911EF…` | `753FE920…` |

다른 Pillow 버전을 쓰면 이미지 리페인트 영역의 안티앨리어싱이 미세하게
달라져 해시가 다를 수 있습니다(기능상 문제는 없음; 텍스트·이름 패치는
Pillow 버전과 무관하게 결정론적입니다).

배포용 xdelta를 만들려면:

```powershell
xdelta3 -e -9 -s "$D1" "D:\ps2\SO3_DC_Disc1_Korean_Full_rebuild.iso" SO3_DC_Disc1_Korean_Full.xdelta
```

### 심층 검증 (선택)

이미지 패치의 보존성 검증(변경 범위가 텍스처 영역으로 한정되는지):

```powershell
python work/img_ko/verify_image_patch.py --input "D:\ps2\rebuild_d1_named.iso" --output "D:\ps2\SO3_DC_Disc1_Korean_Full_rebuild.iso" --orig "$D1" --report work/img_ko/verify_img_rebuild.json
```

전 발생 역디코딩 대조까지 하는 독립 검증기 `verify_full_iso.py`는 인벤토리
재생성(부록 A-1)이 선행돼야 합니다. 전체 옵션 예시는
`work/full_ko_d2/PIPELINE_D2.md` 9단계를 참고하세요(디스크 1은 기본값으로
대부분 생략 가능). 릴리스 검증 당시의 전 항목·수치는
[`docs/releases/v1.2.1-verification.md`](releases/v1.2.1-verification.md)에
있습니다.

## 부록 A. 번역을 수정해서 나만의 빌드 만들기

저장소의 번역 원천 데이터는 다음과 같습니다.

- `work/full_ko/tr_out_v121/` — 디스크 1 최종 번역 DB (333파일, 21,877레코드).
  각 레코드는 `key`(원문의 SHA-1 12자리), `speaker_korean`, `korean`뿐입니다.
- `work/full_ko_d2/tr_out_v121/` — 디스크 2 최종 번역 DB (367파일; 디스크 1과
  공유 키는 동일 번역)
- `work/full_ko/glossary_full.json` — 용어집 2,933항목 /
  `work/full_ko/STYLE_GUIDE.md` — 번역 규칙(대사창 폭, 마커, 존대 등)
- `work/full_ko/patch_plan_full.json`, `work/full_ko_d2/patch_plan_d2.json` —
  위 DB에 폭 보정·용량 보정을 적용해 완성한 **최종 플랜**(재현 빌드의 입력)

### A-1. 인벤토리 재생성 (원문 대조용)

원문(일본어)은 저장소에 없으므로, 수정 작업에는 본인 ISO에서 뽑은
인벤토리가 필요합니다:

```powershell
# 디스크 1 (부가 교차검증 입력은 빈 값으로 생략 — 유닛 키는 동일하게 산출됩니다)
python work/full_ko/build_inventory.py --spoken-csv '""' --event-bank-catalog '""' --ues-csv '""'
# 디스크 2
python work/full_ko/build_inventory.py --catalog work/full_ko_d2/container_catalog.csv --stream-manifest work/full_unpack/disc2/manifests/stream_manifest.csv --out-dir work/full_ko_d2 --spoken-csv '""' --event-bank-catalog '""' --ues-csv '""' --glyph-extra work/full_ko/glyph_labels_extra.json --glyph-extra work/full_ko_d2/glyph_labels_extra_d2.json
```

`work/full_ko/translation_units.jsonl`에 유닛별 원문·`text_key`·폭 예산이
생깁니다. 이 파일로 원하는 유닛의 `key`를 찾으세요.

> PowerShell에서 빈 문자열 인자는 `'""'`처럼 작은따옴표로 감싸야 전달됩니다
> (`""`만 쓰면 인자가 사라져 `expected one argument` 오류). cmd.exe나 bash에서는
> `--spoken-csv ""` 그대로 쓰면 됩니다.

### A-2. 번역 수정 → 플랜 재생성 → 빌드

1. `tr_out_v121/`에서 해당 `key`를 가진 레코드의 `korean`을 고칩니다
   (또는 새 `batch_9xxx_ko.json` 파일을 추가 — 같은 키는 나중 파일이 이김).
2. 규칙 검사(폭 게이트·마커 보존):
   `python work/full_ko/validate_translations.py --batches-dir work/full_ko/tr_batches --out-dir work/full_ko/tr_out_v121 ...`
   — 배치 원본이 없으면 개별 폭 확인만 `width_oracle.py`로 할 수도 있습니다.
3. 플랜 재생성 및 후처리(순서 고정):
   ```powershell
   python work/full_ko/build_full_plan.py --tr-out-dir work/full_ko/tr_out_v121 --inventory work/full_ko/inventory_containers.json --out work/full_ko/patch_plan_full.json
   python work/full_ko/plan_fixups.py
   python work/full_ko/width_overrides.py --data work/full_ko/width_overrides_data_v121.json
   python work/full_ko/fit_repair.py        # 용량 초과 아카이브 자동 보정
   ```
   (디스크 2는 각 도구의 `--plan/--inventory/--catalog` 인자로 d2 경로 지정.
   전체 명령 예시는 `work/full_ko_d2/PIPELINE_D2.md` 7~8단계 참고.)
4. 4단계(텍스트 패치)부터 다시 빌드합니다. `--simulate`의
   `archives_failed == 0` 게이트를 반드시 확인하세요.

주의: 위 3의 후처리를 건너뛰고 플랜을 직접 쓰면 폭 초과(대사창 잘림)나
용량 초과(빌드 실패)가 생길 수 있습니다. 저장소의 최종 플랜은 이미 전부
반영된 상태이므로 **수정 없이 재현만 할 때는 A절이 필요 없습니다**.

### A-3. 참고 문서

- `work/full_ko/MASTER_PLAN.md` — 전체 파이프라인 상태·의사결정 로그(역사적 기록)
- `work/full_ko/PATCHER_SPEC.md` / `INVENTORY_SPEC.md` — 패처·인벤토리 명세
- `work/full_ko/build_v121_progress.md` — v1.2.1 최종 빌드의 단계별 실측 기록
- `work/full_ko_d2/PIPELINE_D2.md` — 디스크 2 전체 명령 체인
- `work/full_ko/name_resource_findings.md` / `save_name_findings.md` — 이름
  시스템·세이브 역공학 노트
- `work/img_ko/decode_notes.md` — FIS 텍스처 포맷 해독 노트
- `automation/workflows/` — 번역·글리프 라벨링에 실제 사용한 Claude Code
  워크플로 스크립트(대량 번역을 재현할 때 참고)

## 부록 B. 저장소에 없는 것 (의도적 제외)

| 항목 | 이유 | 확보 방법 |
|---|---|---|
| 게임 ISO·추출 리소스·원문 텍스트 덤프 | 저작권 | 본인 디스크에서 언팩(2단계)·인벤토리(A-1)로 재생성 |
| `NanumSquareNeo-cBd.ttf` | 폰트 재배포 대신 공식 배포처 안내 | 0단계 링크에서 다운로드 후 SHA 확인 |
| 언팩 산출물·카탈로그·리포트류 | 전부 재생성 가능한 파생물 | 이 문서 2~3단계 |
| 완성 패치(xdelta) | 저장소 대신 GitHub Releases로 배포 | [Releases](https://github.com/snake759494/so3dc-korean-tools/releases) |

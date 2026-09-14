# 하이다 초반 대사 653개 한국어 패치

## 목표와 범위

이 패치는 일본판 『Star Ocean 3 Till the End of Time Director's Cut』 Disc 1의
초반 하이다(Hyda) 구간에서 확인한 대사 653개를 한국어로 교체합니다. 여기서
653은 `(archive_id, stream_id, message_id)`로 식별되는 물리적 위치 수입니다.
바이트가 완전히 같은 원문 세그먼트가 여러 장면에서 재사용되는 경우를 묶으면
번역 레코드는 434개입니다.

- 이벤트 뱅크: `24`개
- 물리적 적용 위치: `653`개
- 고유 원문 세그먼트 및 한국어 번역: `434`개
- 폰트: `NanumSquareNeo-cBd.ttf`
- 폰트 SHA-256:
  `4749FA5691157CF56A59D297B45E88894A646846048018CD7A4117FFB2869767`

이 범위는 게임 전체 대사나 하이다 이후의 모든 스토리 대사를 뜻하지 않습니다.

## 대상 이벤트 뱅크

`stream_id`는 분석 과정에서 부여한 스트림 식별자입니다. 패처는 각 archive의 첫
PK1 package에서 row 1이 기대한 장면 `DCMS`/mclib인지 다시 검사합니다.

| archive | stream | archive | stream |
|---:|---:|---:|---:|
| 1204 | 5829 | 1206 | 5984 |
| 1208 | 6005 | 1210 | 6040 |
| 1212 | 6061 | 1214 | 6120 |
| 1216 | 6141 | 1218 | 6169 |
| 1220 | 6194 | 1222 | 6258 |
| 1224 | 6438 | 1226 | 6459 |
| 1228 | 6543 | 1230 | 6621 |
| 1232 | 6653 | 1234 | 6684 |
| 1236 | 6715 | 1243 | 6989 |
| 1245 | 7017 | 1247 | 7081 |
| 1249 | 7154 | 1251 | 7227 |
| 1253 | 7300 | 1255 | 7404 |

## 공개 데이터 구성

패처의 공개 입력은 다음 두 JSON으로 나뉩니다.

### `translations/hyda_ko.json`

한국어 번역 434개를 담습니다. 각 레코드는 고유 ID, 원문 세그먼트 SHA-256,
한국어 화자, 한국어 본문, 원문의 줄 수와 하나 이상의 적용 위치를 가집니다.
일본어 원문 문자열은 포함하지 않습니다.

하나의 번역 레코드가 여러 occurrence를 가질 수 있으므로 총 레코드 수 434와 적용
위치 수 653은 다릅니다. 패처는 653개 위치가 중복이나 누락 없이 정확히 한 번씩
덮이는지 검사합니다.

### `translations/hyda_patch_manifest.json`

원본을 식별하고 메시지 경계를 찾는 compact 구조 매니페스트입니다. 다음과 같은
검증 정보만 포함합니다.

- archive·stream·message 식별자
- 대상 archive/scene 파일 SHA-256과 폰트 지문
- 원문 세그먼트 SHA-256
- 화자 모드와 화자·본문 경계 오프셋
- 원문 줄 수

매니페스트의 콘텐츠 정책은
`hashes_and_offsets_only_no_extracted_text_or_game_bytes`입니다. 일본어 원문,
메시지 원시 바이트, 폰트 bitmap이나 기타 추출 게임 리소스는 넣지 않습니다.

## 폰트 적용 방식

각 대상 장면의 mclib에는 24×24 로컬 bitmap 폰트가 있습니다. 한국어 글리프는
NanumSquare Neo Bold를 22px로 렌더링하고 2단계 명암으로 양자화합니다.

패처는 다음 순서로 슬롯을 배정합니다.

1. 비목표 메시지에서 참조할 가능성이 있는 모든 로컬 glyph code를 보수적으로
   수집하고 보호합니다.
2. 목표 메시지에서만 쓰이거나 미사용인 기존 슬롯을 먼저 재사용합니다.
3. 부족한 글리프만 로컬 폰트 끝에 추가합니다.
4. 이미 확인된 전역 숫자, 영문과 문장부호는 전역 glyph code를 재사용합니다.
5. 새 glyph code가 검증된 인코딩 범위를 넘거나 압축 결과가 할당 공간에 들어가지
   않으면 출력을 만들지 않고 중단합니다.

이 방식은 장면별로 별도 글리프 표를 만들기 때문에 동일한 한국어 글자가 서로 다른
장면에서 다른 로컬 code를 가질 수 있습니다. code 번호가 아니라 bitmap과 번역
문자 대응을 검증하는 이유입니다.

## 메시지와 아카이브 재구성

패처는 모든 입력을 먼저 검사한 다음 ISO 복사본을 만듭니다.

1. 원본 ISO 크기와 SHA-256을 고정값과 비교합니다.
2. 숨겨진 6,144-entry archive index를 읽어 대상 24개 archive를 찾습니다.
3. compact 매니페스트의 653개 위치, 메시지 SHA-256과 경계 오프셋을 실제 원본
   데이터와 비교합니다. 뱅크별 파일·폰트 지문은 분석 출처를 추적하기 위한
   provenance이며, 원본 전체는 1단계 ISO SHA-256으로 고정합니다.
4. 명시적 화자 필드와 본문의 보이는 glyph를 한국어로 교체합니다. continuation 등
   암시적 화자 메시지는 기존 prefix를 유지합니다.
5. 각 번역은 원문과 같은 줄 수여야 하며, 줄 내부의 이벤트·레이아웃 제어를 문자
   경계에 비례해 다시 배치합니다. 보이는 문자가 없는 구조적 꼬리 줄은 그대로
   보존합니다.
6. 변경한 mclib를 SLZ mode 2로 다시 압축하고 첫 PK1 package의 row 1 record를
   재구성합니다.
7. 뒤따르는 record는 검증된 zero gap 안에서만 이동할 수 있습니다. 이후 PK1
   package와 archive tail은 바이트 단위로 유지합니다.
8. 쓰기 직후 24개 archive를 다시 읽어 653개 대상이 모두 바뀌었고 원문 해시가
   남아 있지 않은지 확인합니다.

지원하지 않는 원본, 누락·중복 번역, 줄 수 불일치, 예상과 다른 message bytecode,
보호 글리프 변경, SLZ 왕복 불일치나 할당 공간 초과가 발견되면 fail closed로
중단합니다.

## 패처 실행

지원 원본 ISO의 SHA-256은 다음과 같아야 합니다.

```text
95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826
```

```powershell
python -m pip install -r requirements.txt
python tools/patch_hyda_dialogue.py `
  "SO3_DC_Disc1_original.iso" `
  "SO3_DC_Disc1_Korean_Hyda_653_Nanum.iso" `
  --catalogue "translations/hyda_patch_manifest.json" `
  --translations "translations/hyda_ko.json" `
  --font "NanumSquareNeo-cBd.ttf" `
  --report "hyda_patch_report.json"
```

`--font`를 생략하면 입력 ISO와 같은 폴더의 `NanumSquareNeo-cBd.ttf`를
사용합니다. 입력 ISO와 출력 ISO는 같은 경로일 수 없으며, 보고서 경로도 보호된
입력 파일을 덮을 수 없습니다.

## 별도 검증

`tools/verify_hyda_dialogue_iso.py`는 패처 모듈을 import하지 않습니다. 별도
PK1·메시지 토큰·NanumSquare Neo bitmap 검증 경로로 결과를 다시 해석합니다.
숨겨진 인덱스, SLZ 해제와 mclib 구조 파서는 공통 저수준 `so3_repack`
라이브러리를 사용합니다.

```powershell
python tools/verify_hyda_dialogue_iso.py `
  "SO3_DC_Disc1_original.iso" `
  "SO3_DC_Disc1_Korean_Hyda_653_Nanum.iso" `
  --catalogue "translations/hyda_patch_manifest.json" `
  --translations "translations/hyda_ko.json" `
  --font "NanumSquareNeo-cBd.ttf" `
  --report "hyda_verification.json"
```

최종 출력 ISO 해시를 함께 고정하려면 다음 옵션을 추가합니다.

```powershell
  --expected-output-sha256 "7080D2226400C5B3747C7FE92F939E9DAD1EFAE580A81CAA2D371D885238E464"
```

검증기는 다음 조건을 모두 요구합니다.

- 원본·출력 ISO 크기와 원본 SHA-256 일치
- NanumSquare Neo 폰트 SHA-256 일치
- 숨겨진 인덱스 원시 바이트 및 6,144개 디코딩 항목 불변
- 허용된 24개 archive 밖 ISO 차이 `0` bytes
- PK1 row identity, 비목표 record, 이후 package와 tail 불변
- SLZ mode·link, record allocation과 zero padding 유효
- 비목표 메시지의 논리 bytecode와 그 메시지가 참조하는 기존 로컬 글리프 불변
- 대상 653개의 이벤트·레이아웃 제어 서명 보존
- 화자와 본문이 `hyda_ko.json`의 한국어와 정확히 일치
- 로컬 bitmap이 제공된 NanumSquare Neo 렌더링과 일치

검증기는 전역 font atlas가 ISO에서 바뀌지 않았음을 확인하지만, 재사용하는 전역
숫자·영문·문장부호 코드의 의미는 기존 atlas 분석 결과를 신뢰합니다.

## xdelta 적용

릴리스 ZIP을 푼 뒤 지원 원본에만 적용합니다.

```powershell
xdelta3 -d -s "SO3_DC_Disc1_original.iso" `
  "SO3_DC_Disc1_Korean_Hyda_653_Nanum_v0.4.0-alpha.1.xdelta" `
  "SO3_DC_Disc1_Korean_Hyda_653_Nanum.iso"
```

적용 전 원본 SHA-256을 확인하고, 적용 후 출력 SHA-256을 릴리스의
`SHA256SUMS.txt`와 비교하세요. 해시가 다르면 실행하지 마세요.

## 배포 범위와 런타임 상태

저장소와 릴리스에는 패처·검증기, 공개 JSON, xdelta, 사용법과 폰트 저작권·OFL
고지만 포함합니다. 다음 항목은 배포하지 않습니다.

- 원본 또는 패치된 ISO
- 게임 실행 파일과 추출 게임 데이터
- 일본어 원문 전체 또는 원문 메시지 raw bytes
- BIOS, 에뮬레이터와 세이브스테이트
- `NanumSquareNeo-cBd.ttf` 폰트 파일 자체

이 릴리스 준비 과정에서는 사용자의 요청에 따라 에뮬레이터를 실행하지 않았습니다.
정적 검증 통과는 실제 표시 품질, 화면 폭, 문맥, 대사 진행과 게임 안정성을 보장하지
않습니다. 이 항목은 사용자가 적법한 원본으로 만든 패치 ISO에서 확인해야 합니다.

# Star Ocean 3 / tri-Ace PS2 형식 공개 자료 조사 및 로컬 교차검증

조사일: 2026-07-13  
대상: `Star Ocean Till the End of Time Director's Cut` 일본판 Disc 1

## 결론

1. 이 디스크의 본체 데이터는 ISO9660 파일 트리의 일반 파일이 아니라, **ISO 원시 LBA 1024(`0x200000`)부터 시작하는 tri-Ace 전용 파일 테이블과 섹터 단위 데이터**다. 흔히 `sotet.bin`이라고 부르는 별도 파일이 있는 것이 아니라, 추출기가 ISO 전체를 직접 읽는다.
2. 공개된 CUE의 `triAce-PS2.c`에 SO3 형식 상수가 정확히 들어 있다.
   - seed `0x13578642`
   - signature `0x27D51556`
   - table offset `0x00200000` = LBA 1024
   - entry count `0x1800` = 6144
3. 해당 C 알고리즘을 독립 구현하여 로컬 ISO를 다시 해독한 결과, 기존 `work/kanji_archive/index.csv`의 **6144개 항목이 모두 일치했다**(`mismatches=0`). 현재 추출 방향은 공개 도구와 바이트 단위로 교차검증됐다.
4. 같은 계열 게임 중 `Radiata Stories`는 SO3와 같은 seed/signature를 쓰되 테이블 위치와 엔트리 수가 다르며, `Valkyrie Profile 2`는 같은 LBA 1024 방식이지만 seed/signature가 다르다.
5. `PACK`과 `SLZ`는 공개 QuickBMS 스크립트로 구조와 해제가 확인된다. 다만 공개 스크립트는 실질적으로 **추출/해제용**이며, 기존 압축률을 유지하는 완전한 재압축기는 아니다.
6. 웹에서 `FIS`는 CUE 도구가 magic에 따라 확장자를 붙이는 수준까지만 공개돼 있고, `SHI`·`ANKF`의 공개 사양/툴은 찾지 못했다. 이 부분은 로컬 역분석 결과가 더 앞서 있다.
7. **SO3의 실제 일본어 한자 폰트는 PS2 BIOS의 `KROM`/`FONTM`이 아니다.** 로컬 분석에서 `so3mclib 1.75` 내부의 **32×32, 4bpp, 부분집합(subset) 글리프 아틀라스**로 확인됐다. 아래 KROM/FONTM 자료는 형식 비교와 오탐 제거용 대조군이다.

## 1. 공개 원본 추출기

### CUE `triAce-PS2.c`

- 원본: [bwass.org의 triAce-PS2.c](https://www.bwass.org/bucket/triAce-PS2.c)
- GitHub 보존본: [AdmiralCurtiss/triAce-Star-Ocean-4-extract의 triAce-PS2.c](https://github.com/AdmiralCurtiss/triAce-Star-Ocean-4-extract/blob/master/triAce-PS2.c)
- 로컬 보존: `work/format_research/triAce-PS2.c`
- 원본 다운로드본 SHA-256: `1B7F424F48F2B8EEE80D7FAE22E390125CACBE5675249F590CB65DDB65B76F83`

소스에 선언된 게임별 값은 다음과 같다.

| 게임 | seed | signature | table byte offset | 엔트리 수 |
|---|---:|---:|---:|---:|
| Star Ocean 3 | `0x13578642` | `0x27D51556` | `0x00200000` | `0x1800` (6144) |
| Radiata Stories | `0x13578642` | `0x27D51556` | `0x3C6C1800` | `0x1200` (4608) |
| Valkyrie Profile 2 | `0x49287491` | `0x516F6699` | `0x00200000` | `0x0C00` (3072) |

테이블은 `N`개씩 연속된 32비트 배열 3개다.

- array 0: LBA
- array 1: sector count
- array 2: 보조값(공개 C 추출기는 사용하지 않음)

각 i에 대해 32비트 wraparound를 적용하면서 다음 순서로 XOR한다.

```text
key = seed
LBA[i]       ^= key; key ^= key << 1
sectors[i]   ^= key; key ^= ~seed
third[i]     ^= key; key ^= (key << 2) ^ seed
```

첫 raw dword는 암호화된 LBA가 아니라 게임 판별 signature라서, CUE 소스는 해독 뒤 `table[0] = table_offset / 0x800`으로 덮어쓴다. SO3에서는 1024다.

추출 파일 크기는 `sector_count × 0x800`이며, 공개 도구는 헤더 magic과 몇 가지 휴리스틱으로 `.elf`, `.slz`, `.sle`, `.zls`, `.seq`, `.pac`, `.fis`, `.pk1`, `.pk2`, `.pk3` 등의 확장자만 붙인다. 원래 파일명 테이블은 없다.

### 로컬 ISO 교차검증

검증 코드: `work/format_research/verify_index.py`  
결과: `work/format_research/verify_index.txt`

```text
raw_signature=0x27D51556
signature_matches=True
entries=6144
mismatches=0
```

즉 지금 만든 인덱스 CSV는 CUE의 공개 원형 알고리즘과 6144개 전체가 정확히 같다.

동일 도구가 현재도 VP2 데이터 추출에 사용된다는 사례도 확인된다: [ResHax의 Valkyrie Profile 2 tri-Ace 아카이브 글](https://reshax.com/topic/1071-ps2-valkyrie-profile-2-silmeria-cant-find-gameplay-voices-tri-ace-square-enix/).

## 2. PACK / PK1과 SLZ

### 공식 QuickBMS 스크립트

- 현재 스크립트: [aluigi의 slz.bms](https://aluigi.altervista.org/bms/slz.bms)
- 로컬 보존: `work/format_research/slz_official.bms`
- SHA-256: `3DD03AD03E707270009FA0C064FA3E54589A635717686EEDCC775D0CF138A505`
- QuickBMS 소스 보존본: [LittleBigBug/QuickBMS](https://github.com/LittleBigBug/QuickBMS)
- 실제 tri-Ace SLZ 해제 코드: [src/unz.c](https://github.com/LittleBigBug/QuickBMS/blob/master/src/unz.c)

현재 `slz.bms`는 `Star Ocean (Tri-ace SLZ/PACK/ADLD) 0.2.5`로 표기돼 있다.

### PACK 헤더

QuickBMS 스크립트가 읽는 기본 구조는 다음과 같다.

```text
0x00  char[4]  "PACK"
0x04  u32      endian 판별용 값
0x08  u32      file_count
0x0C  u32      pack_size
0x10  entries[file_count]

entry (16 bytes):
  u32 dummy
  u32 file_id
  u32 file_size
  u32 file_offset
```

각 entry가 가리키는 내용이 `SLZ`이면 즉시 해제하고, 아니면 raw로 저장한다. SO3의 CUE 추출기가 붙이는 `.pk1`은 항상 literal `PACK` magic인 것은 아니며, 별도의 휴리스틱으로 분류되는 섹터 정렬형 복합 아카이브도 포함한다.

### SLZ 헤더와 모드

PS2 little-endian 기본 헤더는 다음처럼 적용해야 한다.

```text
0x00  char[3]  "SLZ"
0x03  u8       mode
0x04  u32      compressed payload size (ZSIZE)
0x08  u32      decompressed size (SIZE)
0x0C  u32      next SLZ relative offset, 0이면 없음
0x10  ...      payload
```

주의: QuickBMS `src/unz.c`의 오래된 주석은 `0x04`와 `0x08` 설명이 서로 뒤바뀌어 있지만, 실제 C 코드와 `slz.bms`는 `0x08`을 출력 크기로 사용한다. 로컬 해제기도 실제 동작 쪽을 따라야 한다.

모드는 다음과 같다.

| mode | PS2 SO3 의미 | QuickBMS comtype |
|---:|---|---|
| 0 | 저장/무압축 | copy |
| 1 | LZSS | `slz_01` |
| 2 | LZSS + RLE | `slz_02` |
| 3 | 16비트 단위 LZSS | `slz_03` |

현재 스크립트는 후대 tri-Ace 자료를 위해 4(XMem), 5(zlib/deflate), 7(zstd)도 처리하지만 SO3 PS2 원형의 핵심은 0~3이다.

QuickBMS 소스에는 `slz_01_compress`/`02_compress`/`03_compress` 이름도 있으나 구현 함수가 `slz_triace_compress_fake`이며, 플래그를 전부 literal로 내보내는 비압축에 가까운 출력이다. 원본 크기를 유지해야 하는 재삽입에는 충분하지 않다.

### 실제 SO3 번역 프로젝트가 남긴 구조 정보

프랑스어 SO3 번역 프로젝트 게시물:

- [2014년 메시지 추출 결과가 있는 3페이지](https://romhack.org/viewtopic.php?start=50&t=2994)
- [PK1/SLZ/재압축 설명이 있는 4페이지](https://romhack.org/viewtopic.php?start=75&t=2994)
- [2022~2025년 진행 상태가 있는 5페이지](https://romhack.org/viewtopic.php?start=100&t=2994)

게시물에서 확인되는 내용:

- 48,622개 메시지를 추출했으며 중복 제거 시 16,045개라고 보고했다.
- 게임 지역(zone)이 `.pk1`이며 그 안에 스크립트, 대화, 맵 등이 들어 있다.
- 예시 1차 아카이브 구성: `SCE`, `SMCD`, `MAP`, `ATRM`, `MAPA`, `MMAP`, `MSE`.
- 그 뒤 2048바이트 섹터 경계까지 0 padding 후, `CHAR`, `ANIM` 등이 든 2차 아카이브가 올 수 있다.
- 대화는 SLZ 안에 있고, 1차 SLZ를 무압축 mode 0으로 바꾸면 크기가 증가해 2차 아카이브 위치가 움직이므로 게임이 깨진다고 보고했다.
- 게시물은 mode 0=store, 1=LZSS, 2=LZSS+RLE, 3=LZSS16이라고 명시한다.
- 2022년에는 프랑스 확장 문자를 처리하는 데 성공했지만, 2025-04 수정 시점에도 도구 소스는 공개 준비 중이라고 적혀 있다. 현재 재사용 가능한 공개 저장소는 검색되지 않았다.

따라서 재삽입 시에는 다음 중 하나가 필요하다.

1. 원래 SLZ 크기/섹터 배치를 넘지 않는 진짜 compressor 구현.
2. PK1 내부의 후속 아카이브 위치 참조를 모두 찾아 갱신.
3. 최상위 6144-entry 테이블의 LBA/sector count를 다시 인코딩하고, 뒤 파일들의 섹터 배치까지 재작성.

폰트 교체 단계에서는 1번처럼 **동일하거나 더 작은 SLZ**를 만드는 방식이 가장 안전하다.

## 3. FIS / SHI / ANKF와 실제 한자 폰트

### 공개 자료에서 확인되는 범위

CUE 추출기는 첫 4바이트가 `FIS\0`(`0x00534946`)이면 `.fis` 확장자를 붙인다. 그러나 픽셀 포맷, 팔레트, 이미지 목록을 해석하지는 않는다.

웹과 GitHub에서 다음을 조합해 검색했지만 `SHI`와 `ANKF`를 tri-Ace PS2 이미지/폰트 컨테이너로 설명하는 공개 소스는 찾지 못했다.

- Star Ocean 3 / tri-Ace / FIS
- SHI / ANKF / font / PS2
- Radiata Stories / Valkyrie Profile 2 / FIS / SLZ
- magic와 CUE 헤더 상수

따라서 이 둘은 로컬 분석 결과를 기준으로 해야 한다. 현재 로컬 결과상:

- archive file #8, LBA 1833: `SHI`, 256×256 4bpp 2장. 첫 장은 작은 kana/UI, 둘째는 영문/UI.
- archive file #11, LBA 1868: `ANKF`, 256×80 4bpp 영숫자.
- 이들은 작은 UI용이며 큰 대화 한자 폰트가 아니다.

### 실제 SO3 한자 폰트 결론

최종 로컬 역분석에서는 `so3mclib 1.75` 내부 데이터에서 **32×32 셀, 4bpp, 게임이 사용하는 문자만 모은 subset atlas**가 확인됐다. 대화 스크린샷의 실제 글리프와 매칭되므로, 한글패치가 수정해야 할 대상은 이 리소스다.

즉 다음 가설은 폐기한다.

- `FIS` 전체 이미지 중 큰 한자 아틀라스가 있을 것이라는 가설
- `SHI`/`ANKF`가 큰 대화 한자 폰트라는 가설
- PS2 BIOS `KROM` 또는 `FONTM`을 SO3가 직접 사용한다는 가설

## 4. KROM/FONTM 대조군 조사

KROM 가설을 확실히 배제하기 위해 공개 PS2 폰트 구현과 원본 변환기를 내려받아 비교했다.

### 1차 자료

- [PS2SDK font API 문서](https://ps2dev.github.io/ps2sdk/font_8h.html)
- [PS2SDK `fontx.c`](https://github.com/ps2dev/ps2sdk/blob/master/ee/font/src/fontx.c)
- [Woon Yung의 FONTM Font Project](https://sites.google.com/view/ysai187/home/projects/fontm-font-project)

PS2SDK 소스가 명시하는 KROM 구조:

- `rom0:KROM`
- double-byte 3489자
- 16×15, 1bpp, 문자당 30바이트
- 51개의 Shift-JIS range
- ASCII는 `0x198DE`부터 95자, 8×15, 1bpp

Woon Yung 프로젝트가 명시하는 FONTM 구조:

- `rom0:FONTM`
- OSD용 JIS X 0208 계열
- 26×26, 4bpp로 변환 가능
- 헤더, 문자 수, base offset, 글리프 offset table을 갖는 압축 폰트
- KROM/FONTM 모두 JIS level-2 한자가 빠진다.

### 직접 내려받은 원본 파일

- 라이브러리/문서: [Font project.7z](https://www.mediafire.com/file/3lo5j3icuanklnc/%5B130101%5D_Font_project.7z/file)
- 변환된 BMP와 실행 파일: [Playstation 2 ROM fonts.7z](https://www.mediafire.com/file/7d2v7fy951ikel6/Playstation_2_ROM_fonts.7z/file)
- 변환기 C 소스: [Playstation2 ROM font converters src.7z](https://www.mediafire.com/file/947ok2g1i3407ch/Playstation2_ROM_font_converters_src.7z/file)

로컬 SHA-256:

| 파일 | SHA-256 |
|---|---|
| `3lo5j3icuanklnc.7z` | `DD9D1CA22D1E884D14434CA74CE1D5129632D682EBBB298CBA8CEEA6B77C992F` |
| `7d2v7fy951ikel6.7z` | `F08A7CDF50DDCDD1277960424C7D39D216125B41A25883903F5919ECFB88988C` |
| `947ok2g1i3407ch.7z` | `6F00E7CADE85C4EB1CDB57CEE4C2AB4371F3D645007CF57174CAA2CDB124FF45` |
| `KROM.bmp` | `E4859E601F4796EF7C7CB34D130E1FB3E725FE63A05C0E4025CE5EB218433667` |
| `FONTM.bmp` | `CDAB1515E4D8BE19ED1783F28F8F46CAEF803471476EEEF2C9B1BC63EC49F110` |

추출 폴더:

- `work/format_research/3lo5j3icuanklnc_files`
- `work/format_research/7d2v7fy951ikel6_files`
- `work/format_research/947ok2g1i3407ch_files`

KROM 샘플 문자도 Shift-JIS range로 직접 인덱싱하여 렌더링했지만, 실제 SO3 리소스가 32×32 4bpp subset임이 확인됐으므로 KROM은 비교 자료로만 보존한다.

## 5. 현재 프로젝트에 적용할 파이프라인

```text
ISO raw image
  -> 0x200000의 암호화 테이블 3개 해독
  -> 6144개 LBA/sector 엔트리 추출
  -> PK/PACK 내부 엔트리 해체
  -> SLZ chain을 mode별로 해제
  -> so3mclib 1.75 구조 파싱
  -> 32×32 4bpp subset glyph atlas + 문자 매핑 추출
  -> 한글 글리프/매핑 교체
  -> SLZ 재압축 또는 크기 제약형 재작성
  -> PK/PACK offset 및 2048-byte alignment 보존
  -> 최상위 인덱스 재인코딩/ISO 재삽입
```

당장 재사용할 수 있는 공개 자산은 CUE의 최상위 ISO 추출 알고리즘과 QuickBMS의 PACK/SLZ 해제 알고리즘이다. 실제 32×32 한자 폰트 파서는 공개 자료가 없으므로 이번 로컬 역분석 결과를 도구화해야 한다.


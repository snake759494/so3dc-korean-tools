# SO3 첫 한글 메시지 재삽입 보고서

## 결과

원본 ISO를 수정하지 않고 별도 시험 ISO를 생성했다.

- 원본: `D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso`
- 시험본: `D:\ps2\SO3_DC_Disc1_Korean_First_Text.iso`
- 대상: archive 66, stream 57, ISO `0x1D621000`, message ID 10
- 원문: `Game Designer`
- 변경문: `게임 디자이너`
- ISO 크기: 양쪽 모두 4,689,854,464 bytes

message 10의 14바이트 세그먼트는 다음처럼 바뀐다.

```text
원본: 01 02 03 04 05 06 04 07 08 09 0A 04 0B 00
패치: 01 02 05 03 04 06 07 00 00 00 00 00 00 00
```

32px atlas의 기존 코드 1, 2, 3, 4, 6, 7을 각각 `게`, `임`, `디`, `자`,
`이`, `너`로 교체했다. code 5는 원래 공백 글리프를 그대로 사용한다. 이는 첫 실행 증명용
크기 고정 패치이며, 같은 atlas 슬롯을 사용하는 뒤쪽 크레딧도 영향을 받을 수 있다. 최종
번역에서는 local atlas 확장 또는 메시지별 별도 슬롯 배정을 사용해야 한다.

재조립 메시지의 정적 렌더는 `first_text_render.png`다.

## 구현한 파이프라인

`so3_repack.py`는 다음 작업을 한 번에 수행한다.

1. SO3의 `0x200000` 암호화 인덱스를 해독해 대상 archive 경계를 검증한다.
2. 기존 SLZ mode 2 스트림을 해제하고 `so3mclib`의 13개 헤더 필드, message table,
   text/width/bitmap 구간을 파싱한다.
3. Windows Malgun Gothic의 한글을 24px 또는 32px grayscale로 렌더하고 low-nibble-first
   4bpp로 양자화한다.
4. 메시지 bytecode, advance width, glyph bitmap을 함께 바꾼다.
5. LZSS 12-bit window와 SO3 mode-2 short/extended RLE를 사용하는 실제 SLZ2 encoder로
   재압축한다.
6. `next_rel`, 다음 catalogued stream, archive EOF 중 가장 가까운 구조 경계와 원본 뒤의
   연속 zero padding을 모두 검사한다. 압축 결과가 이 범위 안에 들 때만 별도 ISO를 만든다.
7. 출력 ISO를 다시 SLZ 해제하고 mclib 파싱해 byte-for-byte 검증한다.

일반적인 기본값은 새 local glyph를 atlas 끝에 추가하고 mclib의 모든 section offset과
file size를 다시 계산하는 `append`다. 공간이 부족한 시험 스트림에는 명시한 기존 local
code만 교체하는 size-neutral `reuse` 전략을 쓸 수 있다.

이번 실행 명령의 핵심 인수는 다음과 같다.

```powershell
python work\repack_engine\so3_repack.py `
  "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" `
  "D:\ps2\SO3_DC_Disc1_Korean_First_Text.iso" `
  --stream-id 57 --message-id 10 --text "게임 디자이너" `
  --strategy reuse --reuse-codes 1,2,3,4,6,7 --space-code 5 `
  --preview work\repack_engine\first_text_glyphs.png `
  --report work\repack_engine\patch_result.json
```

## 압축 및 경계 검증

| 항목 | 원본 | 패치 |
|---|---:|---:|
| SLZ mode | 2 | 2 |
| compressed payload | 128,278 | 128,097 |
| decoded mclib | 346,112 | 346,112 |
| next_rel | 128,296 | 128,296 |
| message mapping count | 1,220 | 1,220 |
| glyph count | 624 | 624 |

payload가 쓸 수 있는 검증된 범위는 128,280바이트이므로 새 스트림은 183바이트의 여유를
남긴다. 다음 chained member 시작 `0x1D640528`은 이동하지 않았고 그 뒤 64 KiB가 원본과
완전히 같다.

전체 4.69 GB ISO를 양쪽에서 다시 읽어 비교한 결과:

- 원본 SHA-256: `95CC4E25AC71DE7C6263AA2E544910DE30667EA3BA62726CF4A019F24B038826`
- 패치 SHA-256: `3729A58B52DA7E0458F7B9E3B23CDAB0C102547B3DA4EC2028BEA2E597485176`
- 서로 다른 바이트: 121,701
- 최초 차이: `0x1D621004` (SLZ compressed-size field)
- 최종 차이: `0x1D640470`
- 모든 차이는 선택한 stream 57 헤더/압축 payload 안에만 존재한다.
- 패치 mclib SHA-256: `AC08005C247E5F6BB2154D1F8AE775D5619A688FA8A72F3CA29ED74770225299`
- 해제된 mclib에서 달라진 1,162바이트도 message 10의 14바이트 구간, width index
  0/1/2/3/5/6, 같은 여섯 bitmap index 안에만 존재한다. 헤더, 테이블, 나머지 메시지와
  나머지 618개 글리프는 그대로다.

상세 기계 판독 결과는 `patch_result.json`과 `verification.json`에 있다.

## 테스트

`test_so3_repack.py`는 random/incompressible/repeated/RLE 경계 데이터를 포함한 SLZ2
round-trip, ULEB glyph code, mclib atlas 확장/재배치와 공간 부족 fallback을 검사한다.

```text
Ran 5 tests
OK
```

원본 대표 mclib 세 개를 같은 encoder로 무수정 재압축한 크기도 모두 원본 이하였고,
모두 다시 해제했을 때 원본과 완전히 일치했다.

| decoded resource | 기존 compressed | 새 encoder |
|---|---:|---:|
| global 24px 84,608 bytes | 24,496 | 24,490 |
| local 24px 628,736 bytes | 364,775 | 364,626 |
| credits 32px 346,112 bytes | 128,278 | 128,223 |

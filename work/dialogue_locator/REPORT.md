# Issue #1 첫 대사 위치 분석

## 결론

Issue #1의 대사는 Disc 1의 다음 `so3mclib 1.75`에 있다.

| 항목 | 값 |
|---|---:|
| archive ID | **1220** |
| archive LBA / ISO offset | 616147 / `0x4B369800` |
| archive 크기 | 1209 sectors / 2,476,032 bytes |
| archive raw | `work/full_unpack/disc1/raw/1220.pk1` |
| archive raw SHA-256 | `5466CDB5CEFBC1A1F190354CED2487CBEF18F5EECC30252607EED5FB0DCF5D5A` |
| stream ID | **6194** |
| archive 내 stream offset | `0x1188` |
| stream ISO offset | **`0x4B36A988`** |
| SLZ | mode 2, compressed 10,411, decoded 22,528 bytes |
| decoded file | `work/full_unpack/disc1/decoded/1220/s006194_d0_o00001188.mclib` |
| decoded SHA-256 | `591C3C4FB746F618BB7DCBB4C3920B6CBCBEB913E436F5CC5FD39EA7441E91D5` |
| message ID | **5** |
| table row / bytes | `0x98` / `05 00 00 00 96 01 00 00` |
| text-relative offset | `0x196` |
| mclib file offset | **`0x296`** |
| segment length | **124 bytes (`0x7C`)** |

대사의 둘째 줄을 이루는 글리프 byte sequence

```text
A7 01 A7 01 65 BB 01 B0 01 C6 01 4B 5F 57 43 9C 02 ED 01
```

는 실제 atlas로 `ココのホテルってさぁ…。`를 그린다. 이 19-byte 표식을
Disc 1의 7,786개 mclib occurrence 전체에서 다시 검색한 결과는 위 파일의 단 **1건**이었다.
따라서 비슷한 대사가 아니라 Issue #1 원문 자체의 위치이다.

## mclib geometry

| header 항목 | 값 |
|---|---:|
| version | `so3mclib 1.75` |
| table start / text start | `0x80` / `0x100` |
| width table / bitmap start | **`0x680` / `0x700`** |
| mapping count | 14 |
| local glyph count | 72 |
| local glyph code base | **301** |
| glyph cell / stride | **24x24 / 24 pixels** |
| pixel format | linear 4bpp, low nibble first |
| bytes per glyph | 288 |
| cache surface | 288x288 |
| file size | `0x5800` |

`code < 301`은 전역 `so3mclib 1.72`의 `code - 1`번 글리프이고,
`code >= 301`은 이 파일 local atlas의 `code - 301`번 글리프다.
이 대사에서 local로 쓰는 확인 가능한 글자는 다음과 같다.

| encoded | code | local index | 글자 |
|---|---:|---:|---|
| `B2 02` | 306 | 5 | `1` |
| `B3 02` | 307 | 6 | `0` |
| `B8 02` | 312 | 11 | `4` |
| `B5 02` | 309 | 8 | `号` |
| `B6 02` | 310 | 9 | `室` |
| `B9 02` | 313 | 12 | `無` |
| `CE 02` | 334 | 33 | `何` |

나머지 가나와 문장부호는 전역 atlas를 참조한다.

## message ID 5 원본 bytecode

```text
8A800000803F 888006 868000000000 938002 8780 8080
8A800000803F 888007
90026446EB01 888005 938001 888007 ED01 8480 8180
A701A70165BB01B001C6014B5F57439C02ED01 8080
8580CDCC4C3E B202B302B802B502B6027AB9024E7264ED01 8080
8580CDCCCC3E 56764B5FCE02870161655261F101 8480 8180 00
```

전체 연속 hex는 `target.json`에도 보존했다. 실제 전역/local bitmap으로 복원한 결과가
`message_000005_evidence.png`이며 다음 다섯 줄을 정확히 그린다.

```text
ソフィア
「ねぇ、フェイト。
ココのホテルってさぁ…。
104号室が無いよね。
これって何でなのかな?
```

Issue의 `･･･`는 bytecode에서 세 글자가 아니라 전역 atlas의 한 글리프 `…`
(`9C 02`)이다.

## 제어 블록과 이름 삽입

메시지에서 육안으로 보이는 `ソフィア`와 `フェイト`의 literal 글리프 열은 이 mclib
전체에 존재하지 않는다. 대신 다음 제어가 정확히 그 두 위치에 있다.

- `93 80 02`: speaker/character ID 2의 이름 `ソフィア` 삽입
- `93 80 01`: character ID 1의 이름 `フェイト` 삽입

같은 파일의 message 1/5/6은 ID 2가 말하고 message 2/21/23/24/30은 ID 1이 말하는
동일한 prefix를 반복한다. 따라서 `93 80 <u8>`의 이름 치환 기능과 ID 1/2 대응은
원문·위치·반복 문맥으로 교차 확인된다.

`88 80`은 사용 문맥상 글자 style/color selector이다.

- `88 80 06`: speaker name style
- `88 80 07`: 일반 대사 style
- `88 80 05`: 대사 안의 캐릭터명 강조 style

그 외 layout/timing 상태는 번역 시 의미를 추측해 제거할 필요가 없다. 정확한 토큰과
상대 offset은 `target_tokens.csv`에 있다. 특히 `80 80` newline,
`85 80 <float32>`의 0.2/0.4 timing, `84 80 81 80` page/wait 경계는 그대로 보존하는
것이 안전하다.

## 한글 치환용 안전 골격

자연스러운 번역 예시는 다음과 같다.

```text
소피아
「있잖아, 페이트.
여기 호텔 말이야…
104호실이 없지.
이건 왜 그런 걸까?
```

이름까지 한글로 표시하려면 `93 80 02`와 `93 80 01`을 그대로 두면 안 된다. 해당
control은 일본어 이름을 출력하므로 두 control만 새 local 한글 글리프 code 열로 바꾸고,
주변 style control은 보존한다.

```text
8A80 0000803F
888006 868000000000 <소피아> 8780 8080
8A80 0000803F 888007
<「있잖아,> 888005 <페이트> 888007 <.> 8480 8180
<여기 호텔 말이야…> 8080
8580 CDCC4C3E <104호실이 없지.> 8080
8580 CDCCCC3E <이건 왜 그런 걸까?> 8480 8180 00
```

즉 이름 삽입 control 둘만 literal 한글로 바꾸고 `8680...`, `8780`, `8880`,
`8080`, `8580 float`, `8480 8180`은 유지한다. 메시지 길이가 달라지므로 이후 message
offset과 width/bitmap section offset을 재계산하는 기존 mclib rebuild 경로를 사용해야 한다.

## 재현 산출물

- `analyze_target.py`: 위치/원문 assertion, 실제 atlas 증거 렌더, metadata 생성
- `target.json`: 기계 판독 가능한 위치·header·원시 bytecode
- `target_tokens.csv`: control/glyph token과 message-relative offset
- `message_000005_evidence.png`: 전역/local 실제 bitmap으로 복원한 원문
- `locate_by_pattern.py`: 초기 구조 후보 검색 도구. 최종 확정은 위 exact glyph 표식 전수 검색으로 수행

원본 ISO와 추출 파일은 수정하지 않았다.

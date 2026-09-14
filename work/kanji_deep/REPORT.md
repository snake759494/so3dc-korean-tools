# Star Ocean 3 DC 일본어 한자 폰트 분석 보고서

## 결론

한자 폰트를 디스크에서 실제로 찾았다. 이 게임의 주 메시지 글꼴은 PS2 BIOS `KROM`이 아니라,
숨은 tri-Ace 컨테이너 안의 `so3mclib` 리소스에 4bpp 비트맵으로 들어 있다.

- 전역 24px 글꼴: archive entry **#8**, SLZ at ISO byte `0x0039CD90`
  (LBA 1849 + 0x590), `so3mclib 1.72`, 292 slots.
- 메시지별 24px 한자 subset: `so3mclib 1.75`, 수천 개의 SLZ member.
- 별도 32px 스태프롤 글꼴: archive entry **#66**, SLZ at ISO byte
  `0x1D621000` (LBA 240706), 624 slots.

실제 글리프 모음은 다음 산출물에서 바로 확인할 수 있다.

- 전체 24px unique 4,714자: `mclib_catalog/unique_24px_glyphs.png`
- 그중 전역 글꼴 이후 첫 1,024자를 index와 함께 표시:
  `kanji_sample_1024_labelled.png`
- 624자 32px 스태프롤 글꼴: `mclib_346112_render/glyph_contact_indexed.png`
- 1,593자짜리 대표 24px local atlas:
  `mclib_largest_1593_render/glyph_contact_indexed.png`

## ISO/컨테이너 추출 경로

정상 ISO9660 파일 목록은 로더만 보여 주고, 게임 리소스는 ISO byte `0x200000`
(LBA 1024)부터 시작하는 숨은 컨테이너에 있다. CUE의 공개 `triAce-PS2.c`와 실제 디스크를
대조해 다음 값을 재현했다.

| 항목 | 값 |
|---|---:|
| index offset | `0x200000` |
| entry 수 | `0x1800` = 6,144 |
| XOR seed | `0x13578642` |
| decoded arrays | LBA, sector count, aux 각 6,144 `u32le` |
| sector size | `0x800` |
| nested compression | `SLZ\x00..\x03` |

Disc 1 전체를 훑어 **61,862개**의 유효 SLZ stream을 catalog했고, 그중
**7,786개**가 `so3mclib`였다.

## `so3mclib` 글꼴 포맷

헤더는 다음과 같이 해석된다. 모든 값은 little-endian `u32`이다.

| offset | 의미 |
|---:|---|
| `0x00` | `so3mclib 1.72` / `1.75` version string |
| `0x10` | `(message_id, text_offset)` table 시작 |
| `0x14` | encoded text section 시작 |
| `0x18` | glyph advance-width byte table 시작 |
| `0x1C` | bitmap section 시작 |
| `0x20` | local glyph count |
| `0x24`, `0x28` | runtime cache/texture width, height |
| `0x2C`, `0x30` | glyph cell width, height |
| `0x34` | glyph row stride (pixels) |
| `0x38` | **local glyph character-code base** |
| `0x3C` | message table record count |

각 message table record는 `u32 message_id, u32 text_offset`의 8바이트이다.
레코드가 table에서 offset 순으로 정렬되어 있지 않은 파일도 있으므로, 바로 다음 table row를
문자열 끝으로 간주하면 안 된다. offset들을 정렬해서 다음 큰 offset을 경계로 잡고 bytecode
문법에 따라 실제 NUL terminator를 읽어야 한다. `8A 80` scale control의 float32 안에도 `00`이
있을 수 있으므로 단순 `find(00)` 역시 일반 해법이 아니다.

width section의 첫 `glyph_count` 바이트가 각 글리프 advance이다. 이어지는 bitmap은 글리프별
고정 크기 4bpp 데이터이며 **low nibble pixel first**이다.

- 24px: 글리프당 `24 * 24 / 2 = 288` bytes
- 32px: 글리프당 `32 * 32 / 2 = 512` bytes
- 최종 bitmap 뒤에는 SLZ 출력 정렬 때문에 최대 `0x7F` zero padding이 붙는다.

대표 32px 파일은 다음 등식으로 완전히 검증된다.

`0x6800 + 624 * 512 = 0x54800 = 346112 (EOF)`

대표 24px 파일은 padding까지 포함해 다음과 같다.

`0x29780 + 1593 * 288 = 0x997A0`, 이어서 zero 0x60 bytes, EOF `0x99800`.

## 전역 글꼴과 local 한자의 연결

`0x38` 값이 두 atlas 사이의 연결 고리다.

- 전역/standalone library는 base `1`이다. code `1`이 local atlas index 0이다.
- 일반 24px 메시지 library는 base `301`이다.
  - code `< 301`: entry #8 전역 `1.72` atlas, index `code - 1`
  - code `>= 301`: 현재 `1.75` local atlas, index `code - 301`

대표 1,593자 파일의 message ID 2000..2009는 codes 1..10으로 전역 숫자 0..9를 그린다.
ID 2010..2019는 codes 301..310으로 local atlas의 별도 숫자 0..9를 그린다. 실제로
`global_local_samples/message_002000.png`와 `message_002010.png`가 서로 다른 전역/local
모양의 `0`을 렌더한다.

이 구조 때문에 모든 한자를 한 장의 고정 Shift-JIS atlas에 둘 필요가 없다. 공통 ASCII/가나는
전역 글꼴로 공유하고, 각 메시지 묶음에 필요한 한자만 local atlas로 넣는다.

## 문자 bytecode

일반 문자 code는 unsigned LEB7 형태이다.

- `code < 0x80`: 한 바이트 그대로.
- 그 이상: `code = (b0 & 0x7F) | (b1 << 7)`, 여기서 `b0`의 bit 7은 set,
  `b1 < 0x80`.
- 두 바이트가 모두 high-bit set이면 문자 code가 아니라 control namespace이다.
- 확인된 control:
  - `80 80`: newline
  - `8A 80 <float32le>`: scale

base 1인 32px library 전체를 `verify_bytecode.py`로 검증한 결과:

| metric | 값 |
|---|---:|
| records | 1,220 |
| 완전히 해독된 records | 1,220 |
| glyph operands | 12,637 |
| 2-byte extended operands | 1,859 |
| newlines | 63 |
| scale controls | 26 |
| 최대 local glyph index | 623 |
| 범위 밖 index | 0 |
| 미해독 control | 0 |

샘플 렌더도 bytecode와 atlas 연결을 시각적으로 검증한다.

- ID 10: `Game Designer`
- ID 12: `ゲームデザイン`
- ID 13: `則本 真樹`
- ID 20: `Original Story` / `Technical Programmer` / `Director`
- ID 151: `Hiroya Hatsushiba` / `(tri-Crescendo)`, scale 0.6 control 포함
- 24px base-301 ID 2083: `武器`
- ID 2084: `防具`
- ID 2089: `素材`
- ID 3018: `作戦`

관련 이미지는 `message_samples/`와 `kanji_message_samples_24/`에 있다.

base 301 메시지에는 위 둘 이외의 formatting/control opcode도 존재한다. 이번 분석은 글리프
선택과 atlas 경로를 확정했지만, 번역문을 완전히 재조판하려면 그 control들의 operand 길이와
의미를 추가로 표로 만들어야 한다. 글리프/폭/bitmap geometry 자체는 control 해독과 독립적으로
검증되었다.

## 전체 catalog 통계

| 분류 | member occurrence | unique file SHA-256 |
|---|---:|---:|
| 모든 mclib | 7,786 | 1,516 |
| local glyph 포함 | 5,178 | 1,277 |
| glyph_count=0, 전역 글꼴만 사용 | 2,608 | 239 |

세부 geometry:

- `1.72`, 24x24, glyph-bearing: 1
- `1.75`, 24x24, glyph-bearing: 5,176
- `1.75`, 32x32, glyph-bearing: 1
- `1.75`, glyph_count=0: 2,608

bitmap SHA-256로 deduplicate한 결과:

- unique 24px glyph bitmap: **4,714**
- 그중 전역 1.72가 제공하는 unique bitmap: **291** (292 slots 중 blank 중복 1개)
- per-message library에서 추가된 24px bitmap: **4,423**
- unique 32px bitmap: **623** (624 slots 중 중복 1개)
- 크기를 합친 전체 unique bitmap: **5,337**

message table row는 모든 7,786 occurrence 합계 **401,957**개이다. 이 중 glyph-bearing
member가 344,993개, glyph_count=0 member가 56,964개를 가진다. unique mclib 파일만 세면
183,065 rows이다. `message_catalog/unique_encoded_messages.csv`의 22,612 값은 첫 NUL 기준
실험적 dedup 결과이므로, control operand 내부 NUL 때문에 정확한 semantic message 수로
사용하면 안 된다.

## SHI/ANKF와의 관계

앞서 찾은 FIS 이미지는 다른 UI 경로다.

- entry #8 LBA 1833: `SHI`, 256x256 4bpp, 12px 작은 가나/아이콘.
- 같은 entry #8 LBA 1840: 두 번째 `SHI`, 영문 UI.
- entry #8 LBA 1849: 이번에 확정한 전역 `so3mclib 1.72` 24px 글꼴.
- entry #11 LBA 1868: `ANKF`, 256x80 4bpp 영숫자.

즉 SHI/ANKF는 고정 UI atlas이고, 일반 메시지와 한자는 `mclib` 전역 + local subset 경로다.

## BIOS KROM 가설 검증

일본 BIOS에서 `KROM` 106,096 bytes(16x15 1bpp 3,489 double-byte characters)를 추출해
비교했으나 SO3의 주 글꼴 경로라는 증거는 나오지 않았다.

- boot ELF, runtime RAM snapshot, main ELF에 `rom0:KROM`, `KROM`, 고정 KROM 주소가 없다.
- 61,862개 SLZ output을 BIOS KROM 30-byte glyph와 exact-match scan했다.
- 2,871 member에서 저밀도 선문자와 우연히 같은 단일 glyph match 22,702개가 나왔지만,
  **두 글자 이상 연속 match는 0개**였다 (`max run = 1`).
- 반대로 SO3 고유 24/32px antialiased 4bpp glyph가 디스크 `mclib`에서 완전하게 복원된다.

따라서 이 게임의 분석/한글화 대상으로 사용할 폰트는 BIOS KROM이 아니라 disc-side
`so3mclib`이다.

## 공개 프랑스어 패치 교차 검증

공개 SO3 FR 0.2.3 패치의 JP-source hybrid에서 `so3mclib 1.80i`를 추출했다. 이 파일도
동일한 `bitmap_start=0x200`, 292 slots, 24x24, 4bpp geometry이고, 전역 atlas 자리에
프랑스어 악센트 글리프를 넣었다. 즉 실제 선행 번역도 같은 global mclib를 수정했으며,
이번에 찾은 포맷/경로를 독립적으로 뒷받침한다. hybrid의 source-copy가 완전한 원본 ISO는
아니므로 개별 픽셀 byte diff는 증거로 쓰지 않았고, 구조와 변경된 글리프만 확인했다.

## 재현 명령

```powershell
python tools\so3_index.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" --output work\index.csv
python work\kanji_archive\so3_archive_scan.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" work\slz_candidates.csv
python work\kanji_archive\catalog_slz.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" work\slz_candidates.csv work\slz_catalog.csv

python work\kanji_deep\extract_member.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" 3788176 work\kanji_deep\so3mclib_172.bin
python work\kanji_deep\extract_member.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" 492965888 work\kanji_deep\mclib_346112.bin
python work\kanji_deep\render_mclib.py work\kanji_deep\mclib_346112.bin work\kanji_deep\mclib_346112_render --columns 24 --scale 2

python work\kanji_deep\catalog_mclib.py "D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso" work\kanji_archive\slz_catalog.csv work\kanji_deep\mclib_catalog --union-cell 24
python work\kanji_deep\verify_bytecode.py work\kanji_deep\mclib_346112.bin --output work\kanji_deep\bytecode_verification.json

python work\kanji_deep\render_messages.py work\kanji_deep\mclib_largest_1593.bin work\kanji_deep\kanji_message_samples_24 2083 2084 2089 3018 --global-mclib work\kanji_deep\so3mclib_172.bin --scale 8
```

## 핵심 파일 해시

- `so3mclib_172.bin`: `8F91FE6C630BF7890E2934D3B302911188C52DDE5732AA70E3ED40EEA325A3BC`
- `mclib_346112.bin`: `77E595E98AA886F74D8F27695D987642895D08C28A8CE3D5F243518D959ECDE4`
- `mclib_largest_1593.bin`: `9513564ADC656E0E97D961B483175052F4883614A9D0819B27D72E03ED30800F`

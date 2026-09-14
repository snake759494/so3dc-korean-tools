# 첫 대사 패치 분석

> 이 문서는 Noto Sans CJK KR을 사용한 `v0.2.0-alpha.1`의 구조와 검증값을
> 기록합니다. `v0.3.0-alpha.1`은 같은 첫 대사 변환을 NanumSquare Neo로 다시
> 빌드한 뒤 초반 장면의 한자 bitmap 13개를 추가로 교체합니다. 새 빌드의 값은
> [`EARLY_KANJI_READING_PATCH.md`](EARLY_KANJI_READING_PATCH.md)를 참고하세요.

## 대상

- archive ID: `1220`
- archive ISO offset: `0x4B369800`
- root member relative offset: `0x1188`
- mclib message ID: `5`
- 원본 message segment: `124` bytes
- 새 message payload: `113` bytes

화면 문구는 다음과 같습니다.

```text
소피아
「페이트, 봐.
이 호텔은…
104호가 없어.
왜?
```

원문의 `93 80 02`(소피아)와 `93 80 01`(페이트) 이름 호출은 전역 한글 글리프
literal로 바꿨습니다. scale, speaker style/state, 줄바꿈, 본문 style, 이름 강조,
page/wait, 0.2·0.4초 timing, 종료 제어는 원래 순서를 유지합니다.

## 폰트 확장

전역 폰트는 archive 8의 `so3mclib`이며 24×24 4bpp bitmap을 사용합니다.

- 전역 glyph count: `292 → 306`
- 추가 글자: `소피아페이트봐호텔은가없어왜`
- 추가 code: `293..306`
- decoded mclib: `84,608 → 88,704` bytes, 0x80 정렬
- compressed payload: `24,496 → 25,360` bytes
- 고정 allocation: `25,440` bytes, 잔여 `80` bytes
- DTT wrapper: `0xE580 → 0xE900`
- end marker: `0xEC00 → 0xEF80`

릴리스 글리프는 Noto Sans CJK KR Regular, 22px를 사용하고 4bpp 중 네 개의 명암
단계로 양자화했습니다. 이는 24×24 가독성을 유지하면서 고정 archive allocation에
들어가기 위한 설정입니다.

## 장면 mclib 충돌 회피

목표 장면은 기존 local code `301..372`를 사용하므로 새 전역 code `301..306`과
충돌합니다. local base를 `301 → 307`로 옮기고 텍스트 영역의 local operand
108개를 모두 `+6` remap했습니다. 기존 local width table과 72개 bitmap은 한
바이트도 바꾸지 않았습니다.

재압축된 목표 record는 `0x28BC → 0x28D0`, 즉 20바이트 커졌습니다. 같은 첫 PK1
package 안의 후속 10개 상위 record를 20바이트 이동했고, 두 package 사이 원래
380바이트 padding 중 20바이트만 사용했습니다. `0xFD000`에서 시작하는 두 번째
PK1 package는 그대로입니다.

## 최종 검증값

- global archive SHA-256:
  `9DD30E3146A0A797657013DD18B7E4F981A8927AD96078D98493E173BA497778`
- global mclib SHA-256:
  `E62762F6E965D51419EE41AD0B6FD2AF6AE74FF7F3B93EAA5198E6DE543BA843`
- target archive SHA-256:
  `FC3D44EEDD2FC618E1CAD1104366ECE563720A60B1660BCDBE6F40C85A7D45BE`
- target mclib SHA-256:
  `DFDBDC2BA6E9E91A756930A6A70A99C78A350FC143B4F432D9222970DC535768`
- 최종 ISO SHA-256:
  `BBC366A55DA0C2C985BB7E5329A7D2FE8674913995EEB4C81E9674C1E42AF27C`

전체 ISO diff는 `1,014,473` changed bytes이며 archive 8에 `25,258`, archive
1220에 `989,215`가 있습니다. archive 1220의 큰 수치는 20바이트 record 이동으로
인한 위치 차이입니다. 공개 manifest-free 검증에서 나머지 상위 PK1 record 11개가
원본과 같고, 로컬 전체 추출 manifest 검증에서는 root SLZ stream 63개 중 목표를
제외한 62개의 decoded 내용이 같음을 확인했습니다. 두 허용 archive 밖 changed
bytes는 `0`입니다.

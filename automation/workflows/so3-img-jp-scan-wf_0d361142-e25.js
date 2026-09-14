export const meta = {
  name: 'so3-img-jp-scan',
  description: 'SO3 텍스처 컨택트 시트 12장을 병렬 비전 스캔해 고정 일본어 이미지 텍스트 타깃 확정',
  phases: [
    { title: 'Scan', detail: '시트별 일본어 텍스트 판독' },
    { title: 'Merge', detail: '타깃 목록 통합' },
  ],
}

const DIR = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/img_ko'
const pad = (n) => String(n).padStart(3, '0')

const SHEET_SCHEMA = {
  type: 'object', required: ['sheet', 'targets'],
  properties: {
    sheet: { type: 'number' },
    targets: {
      type: 'array',
      items: {
        type: 'object',
        required: ['label', 'jp_text', 'kind'],
        properties: {
          label: { type: 'string', description: 'texture label from the thumbnail, e.g. "40:25 strt 512x256 8b"' },
          jp_text: { type: 'string', description: '해당 텍스처의 일본어 텍스트 전체(줄바꿈은 \\n). 폰트 글리프 그리드면 "KANA_FONT_GRID" 로만 표기.' },
          kind: { enum: ['fixed_text', 'font_grid', 'attack_label_on_screenshot'], description: '고정 메시지 텍스트 / 가나폰트 그리드 / 스크린샷 위 필살기 라벨' },
          notes: { type: 'string', description: '위치·크기·특이사항' },
        },
      },
    },
  },
}

phase('Scan')
const sheetIds = Array.from({ length: 12 }, (_, i) => i)
const results = await parallel(sheetIds.map((i) => () => agent(
  `이미지 판독 작업. ${DIR}/sheets/sheet_${pad(i)}_*.png 파일을 Read 하라 (파일명 접미사가 다양하니, ${DIR}/sheets 에서 sheet_${pad(i)}_ 로 시작하는 png 를 찾아 Read). ` +
  `이 이미지는 Star Ocean 3(PS2 일본판)의 게임 텍스처 컨택트 시트로, 각 썸네일에 텍스처 라벨(archive:stream 이름 WxH bpp)이 붙어 있다.\n` +
  `너의 임무: 이 시트에서 **일본어(히라가나·가타카나·한자) 텍스트가 실제로 렌더된 픽셀로 들어있는 텍스처만** 찾아 보고하라.\n` +
  `- 영어 그래픽 텍스트(SELECT, START, GAME OVER, Display Setting, Attack 등)는 대상이 아니다 — 무시.\n` +
  `- 캐릭터 초상화·3D 렌더·배경·이펙트는 대상이 아니다 — 무시.\n` +
  `- 가나가 격자로 배열된 폰트 아틀라스(あいうえお… 순서대로 나열)는 kind="font_grid" 로 표기하고 jp_text="KANA_FONT_GRID".\n` +
  `- 문장/안내문 형태의 일본어(예: 옵션 설명, 설치 안내)는 kind="fixed_text" 이고 jp_text 에 보이는 일본어를 최대한 정확히 옮겨라(줄바꿈 \\n).\n` +
  `- 전투 스크린샷 위에 작게 얹힌 일본어 필살기/기술 이름 라벨은 kind="attack_label_on_screenshot".\n` +
  `각 대상 텍스처마다 썸네일 아래의 라벨 문자열(예 "40:25 strt 512x256 8b")을 label 로 정확히 적어라.\n` +
  `일본어 텍스트 텍스처가 하나도 없으면 targets=[] 로 반환.\n` +
  `StructuredOutput: {sheet: ${i}, targets: [...]}`,
  { label: `scan:${pad(i)}`, phase: 'Scan', schema: SHEET_SCHEMA }
)))
const ok = results.filter(Boolean)
const totalTargets = ok.reduce((s, r) => s + (r.targets ? r.targets.length : 0), 0)
log(`스캔 완료 ${ok.length}/12 시트, 일본어 타깃 후보 ${totalTargets}건`)

phase('Merge')
const merged = await agent(
  `SO3 이미지 한글패치 타깃 통합. 아래는 12개 컨택트 시트를 비전 스캔한 결과다(JSON).\n${JSON.stringify(ok)}\n` +
  `이를 ${DIR}/jp_image_targets.json 에 통합해 Write 하라: {"fixed_text":[{label, jp_text, notes}], "attack_labels":[{label, jp_text, notes}], "font_grids":[{label, notes}], "summary":{fixed_text_count, attack_label_count, font_grid_count}}.\n` +
  `중복 라벨은 하나로 합치고, jp_text 가 더 완전한 쪽을 채택하라. font_grid 는 jp_text 없이 label·notes 만.\n` +
  `StructuredOutput: {fixed_text_count, attack_label_count, font_grid_count, distinct_labels}`,
  {
    label: 'merge', phase: 'Merge',
    schema: { type: 'object', required: ['fixed_text_count', 'attack_label_count', 'font_grid_count', 'distinct_labels'],
      properties: { fixed_text_count: { type: 'number' }, attack_label_count: { type: 'number' }, font_grid_count: { type: 'number' }, distinct_labels: { type: 'number' } } },
  }
)
return { scanned: ok.length, merged }
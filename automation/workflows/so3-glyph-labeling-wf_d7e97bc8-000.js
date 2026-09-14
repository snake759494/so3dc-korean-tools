export const meta = {
  name: 'so3-glyph-labeling',
  description: '미표기 로컬 글리프(비한자 ~1,000개)를 시트로 렌더해 병렬 시각 판독으로 라벨링',
  phases: [
    { title: 'Render', detail: '미표기 비트맵 → 컨택트 시트 PNG' },
    { title: 'Label', detail: '시트별 병렬 시각 판독' },
    { title: 'Merge', detail: '교차검증 및 라벨 파일 생성' },
  ],
}

const DIR = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko'
const WS = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2'

phase('Render')
const renderRes = await agent(
  `SO3 한글패치 작업. 인벤토리에서 〓로 표시된 미표기 로컬 글리프들을 시각 판독용 컨택트 시트로 렌더링하라.\n` +
  `1) ${DIR}/inventory_containers.json 과 ${DIR}/build_inventory.py 를 참고해, 범위 내 메시지에 실제로 등장하는(=참조되는) 미표기 글리프의 고유 비트맵 목록을 수집하라. 미표기 = ${WS}/work/font_ocr/glyph_mapping_ordered_24.json 에 unicode가 없는 bitmap_sha256. 각 비트맵의 픽셀 데이터는 컨테이너 파일(24x24 4bpp, low nibble 먼저, 288B/글리프)에서 추출.\n` +
  `2) 각 고유 비트맵을 8배 확대해 흰 배경에 검정으로 렌더하고, 10x10 격자 컨택트 시트 PNG로 저장: ${DIR}/glyph_sheets/sheet_NN.png. 각 셀 위에 셀 번호(작은 빨간 숫자)를 그려라 (Pillow 사용).\n` +
  `3) 문맥 힌트: 각 비트맵이 등장하는 대표 메시지 텍스트(〓 위치 포함, 최대 3개)를 뽑아 ${DIR}/glyph_sheets/sheet_NN_context.json 에 {cell: {sha, contexts: [...]}} 로 저장.\n` +
  `4) ${DIR}/glyph_sheets/manifest.json 에 {sheets: N, total_glyphs: M, cells: {sheet: {cell: sha}}} 저장.\n` +
  `StructuredOutput: {sheets, total_glyphs}`,
  { label: 'render-sheets', phase: 'Render', schema: { type: 'object', required: ['sheets', 'total_glyphs'], properties: { sheets: { type: 'number' }, total_glyphs: { type: 'number' } } } }
)
if (!renderRes || !renderRes.sheets) throw new Error('sheet rendering failed')
log(`시트 ${renderRes.sheets}개, 글리프 ${renderRes.total_glyphs}개`)

phase('Label')
const sheetIds = Array.from({ length: renderRes.sheets }, (_, i) => i)
const pad2 = (n) => String(n).padStart(2, '0')
const LABEL_SCHEMA = {
  type: 'object', required: ['labels'],
  properties: {
    labels: {
      type: 'array',
      items: {
        type: 'object', required: ['cell', 'char', 'confidence'],
        properties: { cell: { type: 'number' }, char: { type: 'string' }, confidence: { enum: ['high', 'medium', 'low'] } },
      },
    },
  },
}
const labelResults = await parallel(sheetIds.map((i) => () => agent(
  `이미지 판독 작업. ${DIR}/glyph_sheets/sheet_${pad2(i)}.png 를 Read 하라 (PS2 게임 폰트 글리프 컨택트 시트, 10x10 격자, 각 셀에 빨간 번호).\n` +
  `${DIR}/glyph_sheets/sheet_${pad2(i)}_context.json 도 Read 하라 (각 셀 글리프가 나타나는 일본어 문장 문맥, 〓 위치가 해당 글리프).\n` +
  `각 셀의 글리프가 어떤 문자인지 판독하라. 이들은 비한자(가나 변형, 라틴, 숫자, 기호, 괄호, 화살표, 버튼 아이콘 등)로 알려져 있다. 문맥 문장을 적극 활용하라 (예: 문맥이 ボタンを〓してください 이고 글리프가 원형 기호면 PS 버튼 아이콘).\n` +
  `- 일반 문자는 그 문자 1글자로 (전각/반각 구분).\n` +
  `- PS 버튼 아이콘은 {BTN:○} {BTN:×} {BTN:△} {BTN:□} {BTN:L1} {BTN:R1} {BTN:L2} {BTN:R2} {BTN:START} {BTN:SELECT} {BTN:방향} 형식으로.\n` +
  `- 장식/불명 기호는 {SYM:설명} 형식으로 (예: {SYM:별}).\n` +
  `- 판독 불가면 char='?', confidence='low'.\n` +
  `StructuredOutput: {labels: [{cell, char, confidence}]} — 시트의 모든 셀 포함.`,
  { label: `label:${pad2(i)}`, phase: 'Label', schema: LABEL_SCHEMA }
)))
const okSheets = labelResults.filter(Boolean).length
log(`판독 완료 시트: ${okSheets}/${renderRes.sheets}`)

phase('Merge')
const mergedInput = sheetIds.map((i) => ({ sheet: i, result: labelResults[i] })).filter((x) => x.result)
const mergeRes = await agent(
  `SO3 글리프 라벨 병합 작업.\n` +
  `1) ${DIR}/glyph_sheets/manifest.json 을 Read 하라 (셀→bitmap_sha 매핑).\n` +
  `2) 아래 판독 결과(JSON)를 셀→sha에 대응시켜 ${DIR}/glyph_labels_extra.json 에 Write 하라: {"labels": {"<bitmap_sha256>": {"char": "...", "confidence": "..."}}, "stats": {...}}.\n` +
  `3) 정합성 검사: 같은 시트 내 중복 문자 라벨 중 의심스러운 것(동일 char가 3회 이상 high로 나오면 검토 플래그), '?' 비율.\n` +
  `판독 결과: ${JSON.stringify(mergedInput)}\n` +
  `StructuredOutput: {total, high, medium, low, unknown}`,
  {
    label: 'merge', phase: 'Merge',
    schema: { type: 'object', required: ['total', 'high', 'unknown'], properties: { total: { type: 'number' }, high: { type: 'number' }, medium: { type: 'number' }, low: { type: 'number' }, unknown: { type: 'number' } } },
  }
)
return { rendered: renderRes, labeled_sheets: okSheets, merged: mergeRes }
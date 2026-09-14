export const meta = {
  name: 'so3-tr-shard',
  description: 'SO3 한글패치 번역 샤드: args로 받은 배치들을 번역하고 배치별 단독 검증',
  phases: [
    { title: 'Translate', detail: '샤드 배치 번역 + O(1) 자가검증' },
  ],
}

const DIR = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko'
const WIN = 'C:\\Users\\Jay\\Documents\\Codex\\2026-07-13\\d-3-ps2\\work\\full_ko'
let ids = args
if (typeof ids === 'string') ids = JSON.parse(ids)
if (!Array.isArray(ids)) ids = (ids && ids.batches) || []
const pad = (n) => String(n).padStart(4, '0')

const SCHEMA = {
  type: 'object', required: ['batch', 'count', 'errors'],
  properties: {
    batch: { type: 'number' }, count: { type: 'number' },
    errors: { type: 'number' }, warnings: { type: 'number' }, notes: { type: 'string' },
  },
}

const mkPrompt = (i) => (
  `스타오션3 DC(PS2) 전체 한글패치 본문 번역. 배치 ${pad(i)}.\n\n` +
  `## 절차 (빠르게, 군더더기 없이)\n` +
  `1. ${DIR}/STYLE_GUIDE.md 를 Read (규칙·캐릭터별 말투).\n` +
  `2. ${DIR}/tr_batches/batch_${pad(i)}.json 을 Read. units 배열 전부를 한국어로 번역.\n` +
  `   - jp_speaker 있으면 speaker_korean 필수(한 줄). 없으면 null.\n` +
  `   - korean: 줄 수 == line_count 정확히. ⟦P⟧는 원문과 같은 줄 인덱스에서 줄 시작에.\n` +
  `   - ⟦1⟧⟦2⟧… 마커 전부 순서대로 정확히 1회씩, 의미상 올바른 위치에.\n` +
  `   - ⟦이름:X⟧ → X 로 리터럴화. ⟦G:xxxxxxxx⟧ 는 그대로 복사.\n` +
  `   - 각 줄 char_budget_hint 자 이내 (px 예산 초과 금지).\n` +
  `   - glossary + core_names 대역어 엄수(활용형 변화는 허용).\n` +
  `   - 배치 내 유닛은 장면 순서 — 대화 흐름 자연스럽게.\n` +
  `3. ${DIR}/tr_out/batch_${pad(i)}_ko.json 에 Write:\n` +
  `   {"batch_id": ${i}, "translations": [{"key","speaker_korean","korean"}]} — units 전체·같은 순서. batch_id는 반드시 정수 ${i}.\n` +
  `4. 자가검증(단일 배치, 0.2초): PowerShell 로\n` +
  `   python "${WIN}\\validate_one.py" ${i}\n` +
  `   errors=0 이 될 때까지 수정 후 재실행 (최대 4회). glossary_mismatch 경고는 활용형이면 무시.\n` +
  `5. StructuredOutput: {batch: ${i}, count, errors(최종 0), warnings, notes}\n\n` +
  `주의: 파일 UTF-8. 원문 의미 왜곡·밈 금지. 다른 배치는 건드리지 말 것.`
)

phase('Translate')
const results = await parallel(ids.map((i) => () => agent(
  mkPrompt(i), { label: `tr:${pad(i)}`, phase: 'Translate', schema: SCHEMA, model: 'sonnet' }
)))
const ok = results.filter((r) => r && r.errors === 0).length
log(`샤드 완료: ${ok}/${ids.length} 무오류`)
return { shard_size: ids.length, clean: ok, batches: ids }

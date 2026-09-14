export const meta = {
  name: 'so3-tr-shard-relabel',
  description: 'SO3 재라벨 재번역 샤드: 기존 번역 이식 + 원문 변경분 반영, 배치별 단독 검증',
  phases: [
    { title: 'Translate', detail: '이식·수정 번역 + O(1) 자가검증' },
  ],
}

// args = { disc: 1|2, batches: [n, ...] }
let cfg = args
if (typeof cfg === 'string') cfg = JSON.parse(cfg)
const disc = cfg.disc
const ids = cfg.batches
const D1 = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko'
const D2 = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko_d2'
const DIR = disc === 1 ? D1 : D2
const BATCHES = disc === 1 ? `${D1}/tr_batches_r1` : `${D2}/tr_batches_r2`
const OUT = disc === 1 ? `${D1}/tr_out_r1` : `${D2}/tr_out_r2`
const VAL = disc === 1
  ? 'C:\\Users\\Jay\\Documents\\Codex\\2026-07-13\\d-3-ps2\\work\\full_ko\\validate_one_r1.py'
  : 'C:\\Users\\Jay\\Documents\\Codex\\2026-07-13\\d-3-ps2\\work\\full_ko_d2\\validate_one_r2.py'
const pad = (n) => String(n).padStart(4, '0')

const SCHEMA = {
  type: 'object', required: ['batch', 'count', 'errors'],
  properties: {
    batch: { type: 'number' }, count: { type: 'number' },
    errors: { type: 'number' }, warnings: { type: 'number' }, notes: { type: 'string' },
  },
}

const mkPrompt = (i) => (
  `스타오션3 DC(PS2) 한글패치 재번역 작업 (글리프 라벨 수정에 따른 원문 정정 반영). Disc ${disc}, 배치 ${pad(i)}.\n\n` +
  `배경: 폰트 글리프 라벨 오류를 수정해 일부 유닛의 일본어 원문이 정정되었다. 각 유닛에는 port_hint(기존 한국어 번역)가 들어 있을 수 있다:\n` +
  `- port_hint.jp_body_changed=false 이면 본문 원문은 그대로이고 화자 표기 등만 바뀐 것 — 기존 korean을 거의 그대로 쓰되 규칙 검증만.\n` +
  `- jp_body_changed=true 이면 정정된 원문(jp_body)을 기준으로 기존 번역을 수정하라. 정정 전 원문에 가짜 글자가 섞여 있었으므로(예: 主蛇→主砲, P型→B型, 王→主), 바뀐 글자를 정확히 반영하라. 기존 번역이 이미 문맥으로 옳게 옮긴 경우가 많으니 불필요한 재작성은 금지.\n\n` +
  `## 절차\n` +
  `1. ${D1}/STYLE_GUIDE.md Read.\n` +
  `2. ${BATCHES}/batch_${pad(i)}.json Read. 각 유닛 번역(이식 우선).\n` +
  `   - 줄 수 == line_count, ⟦P⟧ 위치, ⟦n⟧ 마커 전부 순서대로 1회, ⟦이름:X⟧→X, ⟦G:...⟧ 그대로, char_budget_hint 이내, glossary 준수.\n` +
  `3. ${OUT}/batch_${pad(i)}_ko.json Write: {"batch_id": ${i}, "translations": [{"key","speaker_korean","korean"}]} — units 전체·같은 순서, batch_id는 정수.\n` +
  `4. 자가검증: python "${VAL}" ${i} — errors=0 될 때까지 수정(최대 4회).\n` +
  `5. StructuredOutput: {batch: ${i}, count, errors, warnings, notes}.`
)

phase('Translate')
const results = await parallel(ids.map((i) => () => agent(
  mkPrompt(i), { label: `r${disc}:${pad(i)}`, phase: 'Translate', schema: SCHEMA, model: 'sonnet' }
)))
const clean = results.filter((r) => r && r.errors === 0).length
log(`Disc ${disc} 샤드 완료: ${clean}/${ids.length} 클린`)
return { disc, shard_size: ids.length, clean, batches: ids }

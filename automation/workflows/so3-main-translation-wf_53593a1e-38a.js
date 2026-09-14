export const meta = {
  name: 'so3-main-translation',
  description: 'SO3 DC 전체 한글패치: 20,654 유닛을 302개 배치로 병렬 번역 (배치별 자가검증 루프 포함)',
  phases: [
    { title: 'Translate', detail: '302개 배치 병렬 번역 + 자가검증' },
    { title: 'Retry', detail: '실패/미완 배치 재시도' },
  ],
}

const DIR = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko'
const N = 302
const pad = (n) => String(n).padStart(4, '0')

const SCHEMA = {
  type: 'object', required: ['batch_id', 'count', 'errors'],
  properties: {
    batch_id: { type: 'number' }, count: { type: 'number' },
    errors: { type: 'number' }, warnings: { type: 'number' },
    notes: { type: 'string' },
  },
}

const mkPrompt = (i) => (
  `스타오션3 Till the End of Time DC(PS2) 전체 한글패치의 본문 번역 작업. 배치 ${pad(i)}.\n\n` +
  `## 절차\n` +
  `1. ${DIR}/STYLE_GUIDE.md 를 Read (전체 규칙·캐릭터별 말투 — 반드시 준수).\n` +
  `2. ${DIR}/tr_batches/batch_${pad(i)}.json 을 Read. units 배열의 모든 유닛을 한국어로 번역:\n` +
  `   - jp_speaker 있으면 speaker_korean 필수 (한 줄, 화자명 번역: 캐릭터명은 core_names/용어집, 직책·종족 화자는 자연역 예: 地球人の女性→지구인 여성).\n` +
  `   - korean: 줄 수 == line_count 정확히. ⟦P⟧는 원문과 같은 줄 인덱스에서 줄 시작에. ⟦1⟧⟦2⟧… 마커 전부 순서대로 정확히 1회씩 의미상 올바른 위치에. ⟦이름:X⟧→X 그대로 표기. ⟦G:xxxxxxxx⟧ 토큰은 그대로 복사(원본 아이콘 보존).\n` +
  `   - 폭: 각 줄은 char_budget_hint 자 이내 목표 (px 예산 = per_line_px_budget). 대사는 자연스러운 어절 단위 줄바꿈.\n` +
  `   - glossary + core_names 의 대역어 엄수. 문맥상 활용형 변화는 허용.\n` +
  `   - 배치 내 유닛은 장면 순서 — 앞뒤 대사 문맥을 반영해 자연스러운 대화 흐름으로.\n` +
  `3. ${DIR}/tr_out/batch_${pad(i)}_ko.json 에 Write: {"batch_id": ${i}, "translations": [{"key": "...", "speaker_korean": "...혹은 null", "korean": "..."}]} — units 전체, 같은 순서.\n` +
  `4. 자가검증 루프: PowerShell로 실행 → python "${DIR.replace(/\//g, '\\\\')}\\validate_translations.py" --batches-dir "${DIR.replace(/\//g, '\\\\')}\\tr_batches" --out-dir "${DIR.replace(/\//g, '\\\\')}\\tr_out" --report "${DIR.replace(/\//g, '\\\\')}\\tr_out\\val_${pad(i)}.jsonl"\n` +
  `   리포트에서 batch_id==${i} 인 위반만 확인. 오류(E)가 있으면 번역을 수정해 3단계부터 반복 (최대 5회). 경고(W) 중 glossary_mismatch 는 활용형이면 무시 가능, 그 외 경고는 가급적 해소.\n` +
  `5. StructuredOutput: {batch_id: ${i}, count: 번역 유닛 수, errors: 최종 잔여 오류 수(반드시 0이어야 함), warnings: 잔여 경고 수, notes: 특이사항 한 줄}.\n\n` +
  `주의: 다른 배치의 위반은 무시하라. 파일은 UTF-8. 원문 의미 왜곡 금지, 밈·유행어 금지.`
)

phase('Translate')
const ids = Array.from({ length: N }, (_, k) => k + 1)
const results = await parallel(ids.map((i) => () => agent(
  mkPrompt(i), { label: `tr:${pad(i)}`, phase: 'Translate', schema: SCHEMA }
)))
const done = results.filter((r) => r && r.errors === 0)
log(`1차 완료: ${done.length}/${N} 배치 무오류`)

phase('Retry')
const bad = ids.filter((i) => !results[i - 1] || results[i - 1].errors !== 0)
let retryResults = []
if (bad.length) {
  log(`재시도 대상: ${bad.length}개 배치`)
  retryResults = await parallel(bad.map((i) => () => agent(
    `이전 시도가 실패했거나 오류가 남은 배치다. 기존 산출물 ${DIR}/tr_out/batch_${pad(i)}_ko.json 이 있으면 Read 하여 이어서 수정하고, 없으면 처음부터 번역하라.\n` + mkPrompt(i),
    { label: `retry:${pad(i)}`, phase: 'Retry', schema: SCHEMA }
  )))
}
const finalOk = done.length + retryResults.filter((r) => r && r.errors === 0).length
const totalWarn = [...results, ...retryResults].filter(Boolean).reduce((s, r) => s + (r.warnings || 0), 0)
return { clean_batches: finalOk, total_batches: N, remaining: N - finalOk, total_warnings: totalWarn }
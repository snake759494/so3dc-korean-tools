export const meta = {
  name: 'so3-glossary-translate',
  description: 'SO3 DC 한글패치 용어집: 2,933개 고유 명칭을 27개 배치로 병렬 번역하고 일관성 검수',
  phases: [
    { title: 'Translate', detail: '27개 배치 병렬 번역' },
    { title: 'Merge', detail: '병합 및 중복 확인' },
    { title: 'Review', detail: '4개 렌즈 일관성 검수' },
    { title: 'Finalize', detail: '수정 적용 및 확정본 작성' },
  ],
}

const DIR = 'C:/Users/Jay/Documents/Codex/2026-07-13/d-3-ps2/work/full_ko'

const CORE = `[기준 고유명사 표기 — 반드시 준수]
캐릭터: フェイト=페이트, ソフィア=소피아, クリフ=클리프, マリア=마리아, ネル=넬, アルベル=알벨, ロジャー=로저, スフレ=스프레, ミラージュ=미라쥬, アドレー=아드레이, クレア=클레어, ルシフェル=루시퍼, ブレア=블레어, ウェルチ=웰치, アミーナ=아미나, クロセル=크로셀, レナス=레나스, フレイ=프레이, ガブリエ=가브리에, イセリア=이세리아
지명/세력: ハイダ=하이다, ヴァンガード=뱅가드, エリクール=에리클, エアリグリフ=아리그리프, シーハーツ=시하츠, シーランド=시랜드, キルスア=컬소어, ヴェンデーン=반덴, メノディックス=메노딕스, エクスキューショナー=엑스큐셔너, エターナルスフィア=이터널 스피어, サンマイト=선마이트
시스템: 紋章術=문장술, フォル=폴(통화), ガッツ=거츠, バニー=바니, アイテムクリエイション=아이템 크리에이션`

const RULES = `[번역 규칙]
1. 가타카나 외래어 → 표준 외래어 표기법 + 한국 게임계 관용 표기 (ソード=소드, ドラゴン=드래곤, シールド=실드, ベリィ=베리, ポーション=포션, メタル=메탈). 복합어는 붙여쓰기 우선하되 관용상 띄는 경우만 띄어쓰기 (예: ホーリーソード=홀리 소드처럼 무기명 2어절은 띄어쓰기 허용).
2. 한자어 명칭 → 자연스러운 한국어 게임 용어 (예: 突きまくり=마구 찌르기, 号令=호령, 回復=회복). 한자 스킬명의 무협풍 명칭은 한국 한자음 (空破斬=공파참).
3. 글자 수: 결과 한글 글자 수는 원문 글자 수 이하를 강력 권장, 불가피하면 최대 +2자. 메뉴 칸 폭 제한이 있으므로 짧을수록 좋다.
4. 특수 토큰 보존: '¦' 문자, '〓' 문자, 괄호·기호는 위치 유지하고 주변만 번역. (なし)=(없음), (無効)=(무효).
5. cat 의미: item_name/valuable=아이템·귀중품명, symbology=문장술(주문)명, battle_skill=전투 스킬명, enemy=몬스터명, place_facility=지명·시설명, ic_short=아이템 크리에이션 용어, battle_db_other=전투 DB 짧은 텍스트(문장이면 자연스럽게 번역).
6. 이미 영문/숫자뿐인 항목은 그대로 유지. 일본어가 일부인 항목은 일본어 부분만 번역.`

const TR_SCHEMA = {
  type: 'object', required: ['batch_id', 'count'],
  properties: { batch_id: { type: 'number' }, count: { type: 'number' } },
}
const FIX_SCHEMA = {
  type: 'object', required: ['corrections'],
  properties: {
    corrections: {
      type: 'array',
      items: {
        type: 'object', required: ['jp', 'ko', 'reason'],
        properties: { jp: { type: 'string' }, ko: { type: 'string' }, reason: { type: 'string' } },
      },
    },
  },
}

phase('Translate')
const ids = Array.from({ length: 27 }, (_, i) => i)
const pad = (n) => String(n).padStart(3, '0')
const trResults = await parallel(ids.map((i) => () => agent(
  `스타오션3 Till the End of Time DC(PS2) 한글패치의 용어 번역 작업이다.\n` +
  `1) ${DIR}/glossary_batches/batch_${pad(i)}.json 을 Read 하라 (UTF-8 JSON, terms 배열: {jp, cat}).\n` +
  `2) 모든 항목을 순서 그대로 한국어로 번역하라.\n${CORE}\n${RULES}\n` +
  `3) 결과를 ${DIR}/glossary_batches/batch_${pad(i)}_ko.json 에 Write 하라. 형식: {"batch_id": ${i}, "translations": [{"jp": "...", "ko": "..."}]} — 원본과 같은 개수·순서 필수.\n` +
  `4) StructuredOutput 으로 {batch_id: ${i}, count: 번역한 개수} 를 반환하라.`,
  { label: `tr:${pad(i)}`, phase: 'Translate', schema: TR_SCHEMA }
)))
const okCount = trResults.filter(Boolean).length
log(`번역 배치 완료: ${okCount}/27`)
const failed = ids.filter((i) => !trResults[i])
if (failed.length) {
  const retry = await parallel(failed.map((i) => () => agent(
    `스타오션3 한글패치 용어 번역. ${DIR}/glossary_batches/batch_${pad(i)}.json 을 Read 하여 전 항목을 한국어로 번역하고 ${DIR}/glossary_batches/batch_${pad(i)}_ko.json 에 {"batch_id": ${i}, "translations": [{"jp","ko"}...]} 형식으로 Write 하라.\n${CORE}\n${RULES}\nStructuredOutput: {batch_id, count}`,
    { label: `tr-retry:${pad(i)}`, phase: 'Translate', schema: TR_SCHEMA }
  )))
  log(`재시도 완료: ${retry.filter(Boolean).length}/${failed.length}`)
}

phase('Merge')
const mergeRes = await agent(
  `${DIR}/glossary_batches/ 안의 batch_*_ko.json 파일 27개(batch_000_ko.json ~ batch_026_ko.json)를 전부 Read 하여 하나의 사전으로 병합하라.\n` +
  `- 각 파일: {"batch_id", "translations": [{"jp","ko"}]}\n` +
  `- 병합 결과를 ${DIR}/glossary_merged.json 에 Write: {"count": N, "terms": {"<jp>": "<ko>", ...}}\n` +
  `- 같은 jp에 다른 ko가 있으면 목록화하고 더 짧고 자연스러운 쪽을 채택.\n` +
  `- 누락 파일이 있으면 그 batch_id를 보고하라.\n` +
  `StructuredOutput: {count, missing_batches: number[], conflict_count: number}`,
  {
    label: 'merge', phase: 'Merge',
    schema: {
      type: 'object', required: ['count'],
      properties: { count: { type: 'number' }, missing_batches: { type: 'array', items: { type: 'number' } }, conflict_count: { type: 'number' } },
    },
  }
)
log(`병합: ${mergeRes ? mergeRes.count : 0}개 용어`)

phase('Review')
const LENSES = [
  '외래어 표기 일관성: 같은 가타카나 조각(ソード, ドラゴン, ブレード, シールド, アーマー, リング, ベリィ 등)이 항상 같은 한글 표기로 번역되었는지 전수 검사. 다르면 다수결/관용 표기로 통일하는 수정을 제안.',
  '무기·방어구·아이템명 자연스러움: 한국 RPG 유저에게 어색한 직역, 띄어쓰기 불일치, 과도하게 긴 이름(원문+2자 초과)을 찾아 수정 제안.',
  '문장술(symbology)·전투 스킬명 관용: 주문/스킬명이 한국 게임 관용(파이어볼트, 힐링 등)과 무협풍 한자음(공파참 등)을 일관되게 따르는지 검사, 수정 제안.',
  '몬스터명·지명·시설명: 기준 고유명사 표기와의 충돌, 시리즈 관용 표기 위반, 동일 지명의 상이한 표기를 찾아 수정 제안.',
]
const fixLists = await parallel(LENSES.map((lens, li) => () => agent(
  `${DIR}/glossary_merged.json 을 Read 하라 (스타오션3 한글패치 용어집, {"terms": {jp: ko}}).\n검수 렌즈: ${lens}\n${CORE}\n` +
  `문제 항목만 corrections 로 반환하라 (정상 항목은 반환 금지). 각 수정은 {jp, ko: 수정된 한글, reason: 근거 한 줄}.`,
  { label: `review:${li}`, phase: 'Review', schema: FIX_SCHEMA }
)))
const allFixes = fixLists.filter(Boolean).flatMap((f) => f.corrections || [])
log(`검수 수정 제안: ${allFixes.length}건`)

phase('Finalize')
const finalRes = await agent(
  `${DIR}/glossary_merged.json 을 Read 하고, 아래 수정 목록을 적용한 뒤 ${DIR}/glossary_full.json 에 Write 하라.\n` +
  `형식: {"count": N, "terms": {jp: ko}}. 같은 jp에 여러 수정이 있으면 reason 이 더 구체적인 쪽을 채택하라.\n` +
  `수정 목록(JSON): ${JSON.stringify(allFixes)}\n` +
  `StructuredOutput: {count, applied}`,
  {
    label: 'finalize', phase: 'Finalize',
    schema: { type: 'object', required: ['count', 'applied'], properties: { count: { type: 'number' }, applied: { type: 'number' } } },
  }
)
return { merged: mergeRes, fixes: allFixes.length, final: finalRes }
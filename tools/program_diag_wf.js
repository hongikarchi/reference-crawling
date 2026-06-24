export const meta = {
  name: 'program-contradiction-classify',
  description: 'Classify typology<->program contradictions: program_error vs map_artifact vs typology_error vs ambiguous (Sonnet, source prose)',
  phases: [{ title: 'Classify' }],
}

// args: { batchIndices?: number[] }  (default all 34). Smoke = {batchIndices:[0]}.
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const DIR = '/tmp/prog_diag_batches'
const ALL = Array.from({ length: 34 }, (_, i) => i)
const BATCHES = (A.batchIndices && A.batchIndices.length) ? A.batchIndices : ALL

const CLASSIFY_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['results'],
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'category', 'suggested_program', 'confidence', 'reason'],
        properties: {
          id: { type: 'string' },
          category: { type: 'string', enum: ['program_error', 'map_artifact', 'typology_error', 'ambiguous'] },
          suggested_program: { type: ['string', 'null'], description: 'controlled-vocab program; non-null ONLY if category=program_error' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          reason: { type: 'string' },
        },
      },
    },
  },
}

function classifyPrompt(path) {
  return `You are auditing typology<->program label contradictions in an architecture database.
Each row has a fine-grained \`stored_typ\` (typology) and a COARSE \`stored_program\` (one of 14 controlled programs).
A row is here because \`stored_program\` falls OUTSIDE \`acceptable_programs\` (programs normally compatible with that typology).
Your job: read the source \`evidence\` (prose, possibly non-English — translate mentally) and classify WHY.

Read the batch file FIRST (use the Read tool): ${path}
JSONL; each line: id, name, stored_typ, stored_program, acceptable_programs, allowed_programs (full 14-value vocab), evidence.

For EACH row choose exactly one category:
- "program_error" — stored_program is genuinely WRONG per the evidence (e.g. a gallery tagged "Other" that is clearly a Museum/Public use; an office tagged "Public"). Set suggested_program to the CORRECT program (MUST be a member of allowed_programs). Prefer a program inside acceptable_programs when evidence supports it.
- "map_artifact" — stored_program is DEFENSIBLE; it only looks wrong because acceptable_programs is too narrow (e.g. a Religious Building that is genuinely also a sports/community complex tagged "Mixed Use"; a Civic Building with an educational/healthcare function). DATA is fine; contradiction is a metric artifact. suggested_program = null.
- "typology_error" — evidence shows the TYPOLOGY is wrong (rare). suggested_program = null.
- "ambiguous" — evidence insufficient. suggested_program = null.

Rules:
- suggested_program non-null ONLY for program_error, and MUST be an exact member of allowed_programs.
- Do NOT invent programs. Do NOT default everything to program_error — map_artifact is common and correct here.
- Be conservative: if stored_program is a plausible coarsening of the real use, it is map_artifact.
- \`reason\`: one short clause.

Return ALL rows from the file via StructuredOutput. Text output is ignored.`
}

phase('Classify')
const perBatch = await parallel(
  BATCHES.map(bi => () =>
    agent(classifyPrompt(`${DIR}/batch_${String(bi).padStart(3, '0')}.jsonl`),
      { label: `classify:b${bi}`, phase: 'Classify', schema: CLASSIFY_SCHEMA, model: 'sonnet' }
    ).then(res => ({ bi, results: (res && res.results) || [] }))
  )
).then(rs => rs.filter(Boolean))

// flatten
const all = []
for (const b of perBatch) for (const r of (b.results || [])) all.push({ ...r, batch: b.bi })
const counts = {}
for (const r of all) counts[r.category] = (counts[r.category] || 0) + 1

return { total: all.length, batches: perBatch.length, counts, results: all }

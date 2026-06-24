export const meta = {
  name: 'program-error-verify',
  description: 'Independent Opus blind A/B verification of program_error candidates (which program fits the evidence)',
  phases: [{ title: 'Verify' }],
}

// args: { numBatches: number }  (count of verify_NNN.json files prepared by program_verify_prep.py)
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const N = A.numBatches || 0
const DIR = '/tmp/prog_diag_batches'
const BATCHES = Array.from({ length: N }, (_, i) => i)

const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'better', 'why'],
        properties: {
          id: { type: 'string' },
          better: { type: 'string', enum: ['A', 'B', 'equal'] },
          why: { type: 'string' },
        },
      },
    },
  },
}

function prompt(path) {
  return `You are an independent architecture expert judging PROGRAM labels.
The 14 controlled programs are coarse functional buckets: Housing, Office, Museum, Education, Public, Religion, Healthcare, Hospitality, Sports, Transport, Infrastructure, Landscape, Mixed Use, Other.

Read the JSON file (use the Read tool): ${path}
It is an array of items, each: { id, evidence (source prose, may be non-English — translate mentally), option_A (a program), option_B (a program) }.

For each item decide which program label better fits the building DESCRIBED IN THE EVIDENCE:
- "A" if option_A fits clearly better
- "B" if option_B fits clearly better
- "equal" if both are equally defensible
You are BLIND to which option is the database's current value. Judge ONLY on evidence fit. Be willing to say "equal" when the call is genuinely close.

Return one verdict per id via StructuredOutput. Text output is ignored.`
}

phase('Verify')
const out = await parallel(
  BATCHES.map(bi => () =>
    agent(prompt(`${DIR}/verify_${String(bi).padStart(3, '0')}.json`),
      { label: `verify:b${bi}`, phase: 'Verify', schema: VERIFY_SCHEMA, model: 'opus' }
    ).then(res => ({ bi, verdicts: (res && res.verdicts) || [] }))
  )
).then(rs => rs.filter(Boolean))

const all = []
for (const b of out) for (const v of (b.verdicts || [])) all.push(v)
return { batches: out.length, verdicts_total: all.length, verdicts: all }

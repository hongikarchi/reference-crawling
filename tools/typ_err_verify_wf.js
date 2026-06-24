export const meta = {
  name: 'typology-error-verify',
  description: 'Independent Opus check of the 38 typology_error candidates flagged by the program diagnosis',
  phases: [{ title: 'Verify' }],
}
const DIR = '/tmp'
const BATCHES = [0, 1]

const SCHEMA = {
  type: 'object', additionalProperties: false, required: ['results'],
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'stored_typ_ok', 'suggested_typ', 'confidence', 'why'],
        properties: {
          id: { type: 'string' },
          stored_typ_ok: { type: 'boolean', description: 'true if the stored typology is a defensible label for this building' },
          suggested_typ: { type: ['string', 'null'], description: 'a clearly-better typology from allowed_typologies; null if stored is fine' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          why: { type: 'string' },
        },
      },
    },
  },
}

function prompt(path) {
  return `You are an independent architecture expert auditing TYPOLOGY labels (the building-kind label).
These rows were flagged by a program-axis judge as possibly having a WRONG typology. Verify independently.

Read the JSON file (use the Read tool): ${path}
Array of: { id, name, stored_typ, stored_program, classify_reason, allowed_typologies (the controlled vocab), evidence (source prose, may be non-English) }.

For EACH row judge ONLY the typology (ignore program):
- stored_typ_ok = true if stored_typ is a DEFENSIBLE typology for the building described. Be lenient on near-synonyms (House/Housing, Museum/Gallery).
- If stored_typ is clearly wrong, set stored_typ_ok=false and suggested_typ = the clearly-better label (MUST be an exact member of allowed_typologies). Common real error here: landscape/public-realm projects (squares, parks, playgrounds, gardens, installations) mislabeled as a building typology — but ONLY mark wrong if the built deliverable genuinely is not the stored kind.
- Be conservative: typology was recently re-derived; default to stored_typ_ok=true unless the evidence clearly contradicts it.

Return one result per id via StructuredOutput. Text output is ignored.`
}

phase('Verify')
const out = await parallel(
  BATCHES.map(bi => () =>
    agent(prompt(`${DIR}/typ_err_b${bi}.json`),
      { label: `typverify:b${bi}`, phase: 'Verify', schema: SCHEMA, model: 'opus' }
    ).then(res => ({ bi, results: (res && res.results) || [] }))
  )
).then(rs => rs.filter(Boolean))

const all = []
for (const b of out) for (const r of (b.results || [])) all.push(r)
const wrong = all.filter(r => r.stored_typ_ok === false && r.suggested_typ)
return { total: all.length, confirmed_typ_wrong: wrong.length, results: all }

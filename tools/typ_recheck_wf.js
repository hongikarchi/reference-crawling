export const meta = {
  name: 'typology-pavilion-recheck',
  description: 'Skeptical second Opus pass: confirm or REFUTE the proposed Pavilion/Office typology before applying (Pavilion is a known over-call trap)',
  phases: [{ title: 'Recheck' }],
}

const SCHEMA = {
  type: 'object', additionalProperties: false, required: ['results'],
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'proposed_typ_confirmed', 'better_typ', 'why'],
        properties: {
          id: { type: 'string' },
          proposed_typ_confirmed: { type: 'boolean', description: 'true only if the proposed typology is clearly the best label' },
          better_typ: { type: ['string', 'null'], description: 'if not confirmed, the typology that is actually best (from allowed_typologies), or null to keep stored' },
          why: { type: 'string' },
        },
      },
    },
  },
}

function prompt(path) {
  return `You are a SKEPTICAL independent architecture expert. A first pass proposed changing each building's typology to \`proposed_typ\` (often "Pavilion"). "Pavilion" is a KNOWN OVER-CALL trap — it gets over-applied to real buildings. Your job is to REFUTE the proposal unless the evidence clearly supports it.

Read the JSON file (use the Read tool): ${path}
Array of: { id, name, stored_typ, proposed_typ, opus1_why, allowed_typologies, evidence (source prose, may be non-English) }.

For EACH row:
- Set proposed_typ_confirmed = true ONLY if the evidence clearly shows the building genuinely IS that kind. For "Pavilion": confirm only for genuinely small/temporary/exhibition/installation/open-structure projects (trade-fair stands, biennale installations, art objects, small garden follies, meditation/ceremonial open structures). Do NOT confirm Pavilion for a real permanent building.
- If NOT confirmed, set better_typ to the typology that is actually best from allowed_typologies (could be the stored_typ, or another). Set better_typ=null to keep stored.
- Default to skepticism: if the proposal is shaky, refute it.

Return one result per id via StructuredOutput. Text output is ignored.`
}

phase('Recheck')
const res = await agent(prompt('/tmp/typ_recheck.json'),
  { label: 'pavilion-recheck', phase: 'Recheck', schema: SCHEMA, model: 'opus' })
const all = (res && res.results) || []
const confirmed = all.filter(r => r.proposed_typ_confirmed)
return { total: all.length, confirmed: confirmed.length, results: all }

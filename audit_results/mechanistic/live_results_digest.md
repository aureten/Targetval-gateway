# TargetVal03 live mechanistic-biologist audit — objective digest

- Run date: 2026-07-17.
- Endpoint: deployed public `/api/v1/validate`, mode `SETTLE`, the same backend used by `/chat`.
- All responses were produced by served commit `7da153f`, UI `v4.43-phenotype-outcome-inversion`.
- The final capture re-read nine cards from the just-populated response cache; their embedded compute stamps are 2026-07-17 06:59–07:18 UTC. `GDF15 | cancer cachexia` was an uncached resolution halt.
- Ten requests returned HTTP 200. Nine produced assessment cards; one halted before evidence fetch because disease resolution failed.

## Cross-case surface

- Verdicts: 4 `ACQUIRE-EVIDENCE`, 2 `GO`, 1 `HOLD`, 1 `GATE`, 1 `CLINICALLY_DISCONFIRMED`, and 1 resolution halt.
- Direction was answered on 5 of 9 cards.
- The causal-mechanism cell returned `mechanistic edges but no closed path` on all 9 cards.
- The separate mechanistic-spine object emitted a non-empty path on only 2 of 9 cards: `VEGFA → FLT1 → preeclampsia` and `TGFB1 → COL1A1 → osteogenesis imperfecta`.
- `engagement` was insufficient on 9 of 9 cards.
- `molecular_disease` was insufficient on 9 of 9 cards.
- `model_recap` was insufficient on 8 of 9 cards; BRD9 alone returned mouse-KO concordance.
- `perturbation` was answered on all 9 cards.
- Seven of nine perturbation cells explicitly said that no strong DepMap/ORCS effect occurred in the queried disease lineage, yet `genetics_perturbation_concordance` returned `Concordant signals` on all 9 cards.
- The phenotype→outcome object triggered no reader-facing warning in any case.

## Case-level outputs

| Target | Disease | Header | Direction | Mechanistic cell | Perturbation context | Molecular disease | Engagement |
|---|---|---|---|---|---|---|---|
| GDF15 | obesity | ACQUIRE-EVIDENCE | insufficient | edges, no path | no obesity-lineage dependency | insufficient | insufficient |
| GDF15 | cancer cachexia | RESOLUTION HALT | — | — | — | — | — |
| FGF21 | MASH | ACQUIRE-EVIDENCE | insufficient | edges, no path | no MASH-lineage dependency | insufficient | insufficient |
| IL10 | inflammatory bowel disease | GO · genetics-led | agonize / restore, contested | edges, no path | no IBD-lineage dependency | insufficient | insufficient |
| HHLA2 | NSCLC | ACQUIRE-EVIDENCE | insufficient | edges, no path | no NSCLC-lineage dependency | insufficient | insufficient |
| FLT1 | preeclampsia | ACQUIRE-EVIDENCE | inhibit / lower | edges, no path | no preeclampsia-lineage dependency | insufficient | insufficient |
| COL1A1 | osteogenesis imperfecta | HOLD | pole suppressed: mixed haploinsufficiency / dominant-negative | edges, no path | no OI-lineage dependency | insufficient | insufficient |
| TREM2 | Alzheimer disease | GO · genetics-led | agonize / restore | edges, no path | no AD-lineage dependency | insufficient | insufficient |
| BRD9 | synovial sarcoma | CLINICALLY_DISCONFIRMED | insufficient | edges, no path | one SyS-lineage dependency | insufficient | insufficient |
| KRAS | pancreatic cancer | GATE | inhibit, gain-of-function | edges, no path | 19 PDAC-lineage dependencies | insufficient | insufficient |

## High-impact live observations

### GDF15 — obesity

- The mechanistic spine correctly described GDF15 as a stress hormone binding GFRAL in the area postrema / nucleus tractus solitarius and named the aversive, nausea/vomiting, appetite-loss circuit.
- The structured effector and path were nevertheless empty; therapeutic direction was insufficient.
- The safety/window cells called the starting window favorable and did not use the aversive circuit as a mechanistic liability.
- Nine CRISPR screens and eight strong effects in other lineages were treated as a perturbation signal; the concordance cell then called genetics and perturbation concordant.
- Best-fit modality was `antibody, splice-switching ASO`, despite unresolved direction.

### GDF15 — cancer cachexia

- The query did not reach evidence fetch.
- The served halt said that `adult hepatocellular carcinoma` resolved ambiguously and asked whether the user meant unrelated diseases including XFE progeroid syndrome, spinocerebellar ataxia 48, neuropathy, Rett syndrome, Morimoto–Ryu–Malicdan neuromuscular syndrome, or young adult-onset parkinsonism.
- Thus the same target's opposite-sign cachexia mechanism could not be evaluated.

### FGF21 — MASH

- The mechanistic spine stated that FGF21 activity requires the co-receptor β-Klotho, but emitted no effector or causal path.
- Direction was insufficient and the phenotype→outcome object was `unmeasured`.
- The card called FGF21 `novel / under-drugged (Tbio)` and returned clinical validation as insufficient.
- Best-fit modality was antibody, but the card did not distinguish a neutralizing antibody from an agonistic β-Klotho/FGFR-complex strategy or an FGF21 analogue.
- Twelve CRISPR-screen hits and eight strong effects outside the MASH lineage were still reconciled as genetics–perturbation concordance.

### IL10 — inflammatory bowel disease

- The mechanistic spine accurately described IL10RA/IL10RB, JAK1/TYK2–STAT3 signaling, and anti-inflammatory effects on macrophages/monocytes.
- Direction correctly resolved to `agonize / restore`, and the expression cell explicitly warned that elevated disease expression may be compensatory.
- The causal cell nevertheless named IL17A, IL1B and IL5 as partial edges and emitted no closed path.
- The card recommended antibody plus splice-switching ASO, while target engagement, molecular disease and disease-specific clinical validation were insufficient.
- The safety read called the window favorable and treated the observed loss phenotypes as non-severe.
- Five strong effects outside the IBD lineage were still called concordant with human genetics.

### HHLA2 — NSCLC

- The mechanistic spine named only the TMIGD2 costimulatory interaction.
- A separate network cell detected a dual arm, but labeled HHLA2 itself as an inhibitory receptor and proposed blocking HHLA2 while preserving/agonizing CD28; it did not name KIR3DL3 and TMIGD2 as the two actionable receptor poles.
- Direction was insufficient.
- The disease-lineage DepMap result was non-dependent, but other-lineage perturbations were still reconciled as concordant evidence.
- Best-fit modality was small molecule plus splice-switching ASO; localization remained ambiguous.

### FLT1 — preeclampsia

- The spine emitted `VEGFA → FLT1 → preeclampsia`; the direction cell chose inhibition from 47 inhibitor versus 1 agonist drug mechanisms.
- The card did not resolve placental soluble sFLT1 isoforms versus full-length membrane FLT1, or fetal/placental versus maternal compartments.
- Thirty-four pathogenic ClinVar variants, many from unrelated syndromic contexts, were called an allelic series and a genotype response stratifier.
- Oncology VEGFR drugs were correctly treated as off-indication clinical context, yet their class toxicities were also imported into the FLT1 on-target safety read.
- Attrition nevertheless said `Approved mechanism in class — de-risked`.

### COL1A1 — osteogenesis imperfecta

- This was the strongest mechanistic call in the battery.
- The direction pole was explicitly suppressed because a mixed haploinsufficiency plus dominant-negative allelic series does not fit whole-target agonize-versus-antagonize logic; the card recommended allele/subtype-selective resolution instead.
- The separate mechanistic spine nevertheless emitted `TGFB1 → COL1A1 → osteogenesis imperfecta`, which is not the variant-to-matrix causal mechanism identified by the direction cell.
- The modality cell proposed antibody plus splice-switching ASO, without tying the oligonucleotide lane specifically to mutant-allele silencing.
- Fourteen strong dependencies outside the OI lineage were still called concordant with human genetics.
- Off-indication collagenase/ocriplasmin precedent caused attrition to read `Approved mechanism in class — de-risked`.

### TREM2 — Alzheimer disease

- Human genetics and direction were correctly read as loss-of-function / haploinsufficiency supporting agonize or restore.
- The mechanistic spine contained detailed microglial functions and partners but emitted no effector or causal path.
- The card called TREM2 `novel / under-drugged`, target engagement insufficient, clinical validation insufficient, and one trial an unverified business termination.
- The safety narrative contradicted the direction: it described the case as a gain-of-function disease treated by inhibition while the card's direction was agonize/restore.
- Nine strong effects outside the Alzheimer lineage were still treated as concordant perturbation evidence.

### BRD9 — synovial sarcoma

- The header correctly scoped `CLINICALLY_DISCONFIRMED` to refractory monotherapy and attempted an adequacy analysis across FHD-609 and CFT8634.
- The spine described BRD9 as a GBAF chromatin-regulator component, but had no effector/path and did not represent SS18::SSX-dependent complex remodeling.
- Direction was insufficient; molecular disease was insufficient.
- One synovial-sarcoma-lineage dependency was captured.
- The standard attrition cell still called the same clinical record an unverified business termination, creating a conflict with the header's affirmative adequate-test disconfirmation.
- The decision cell remained generic: fill a mechanistic bridge.

### KRAS — pancreatic cancer

- The direction cell correctly recognized recurrent pathogenic missense hotspots and gain-of-function, resolving inhibition.
- The perturbation cell captured 19 pancreatic-lineage dependencies.
- The causal-mechanism cell nevertheless selected CALML5/CALML3/CALM3 as partial edges and emitted no KRAS→PDAC path.
- Cell-membrane/cytoplasmic localization was misread as extracellular surface accessibility, opening antibody, ADC and T-cell-engager formats.
- A trial explicitly described as an `Administrative hold pending non-safety related changes` was classified as a safety stop.
- The card surfaced only phase-2 adagrasib/sotorasib pancreatic precedent and returned disease-specific clinical validation as insufficient.

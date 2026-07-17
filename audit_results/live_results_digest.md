# TargetVal03 live drug-hunter audit — objective digest

- Run date: 2026-07-17.
- Endpoint: deployed public `/api/v1/validate`, mode `SETTLE`.
- Served build on all ten cards: commit `c78cf61`, UI `v4.43-phenotype-outcome-inversion`.
- All ten cases returned HTTP 200 JSON, uncached, on the first attempt.
- Latency: minimum 13.039 s; median 77.105 s; mean 73.435 s; maximum 128.231 s.
- Drug-hunter cells answered: median 25.5 of 33; range 23–29.
- `engagement` was insufficient in 10/10 cases; `molecular_disease` was insufficient in 10/10.

## Case-level outputs

| Target | Disease | s | Header | Grade | Cells | Direction | Best-fit modality output | Clinical-validation output | Attrition output |
|---|---|---:|---|---|---:|---|---|---|---|
| PCSK9 | familial hypercholesterolemia | 44.116 | GO | convergent | 29/33 | Antagonize / inhibit | antibody, splice-switching ASO | 8 disease-scoped Phase-3+ trials + 4 approved drugs | approved mechanism, de-risked |
| GLP1R | obesity | 67.717 | GO | clinically validated | 27/33 | Agonize / activate | small molecule, antibody, splice-switching ASO | 4 approved drugs | validated mechanism; verify terminated-trial readout |
| TYK2 | psoriasis | 128.231 | GO | convergent | 28/33 | Antagonize / inhibit / lower | small molecule, splice-switching ASO | 2 approved drugs | validated mechanism; verify terminated-trial readout |
| GBA | Parkinson disease | 61.313 | GATE | — | 25/33 | Agonize / restore | small molecule, antibody, splice-switching ASO | insufficient | 1 terminated trial, readout unverified |
| TREM2 | Alzheimer disease | 13.039 | GO | genetics-led | 24/33 | Agonize / restore | small molecule, antibody, splice-switching ASO | insufficient | 1 terminated trial, readout unverified/business |
| CETP | coronary artery disease | 92.588 | GATE | — | 25/33 | Antagonize / inhibit / lower | antibody, splice-switching ASO | insufficient | 5 late-stage mechanisms, none approved |
| IL1B | coronary artery disease | 93.777 | ACQUIRE-EVIDENCE | — | 25/33 | Antagonize / inhibit / lower | small molecule, PROTAC, splice-switching ASO | insufficient | approved mechanism, de-risked |
| BRD9 | synovial sarcoma | 24.006 | CLINICALLY_DISCONFIRMED | — | 23/33 | insufficient | small molecule, PROTAC, degrader/molecular glue | insufficient | 1 terminated trial, readout unverified/business |
| FLT1 | preeclampsia | 123.066 | ACQUIRE-EVIDENCE | — | 26/33 | Antagonize / inhibit / lower | small molecule, antibody, splice-switching ASO | insufficient | approved mechanism, de-risked |
| KRAS | pancreatic cancer | 86.493 | GATE | — | 26/33 | Antagonize / inhibit | small molecule, antibody, ADC | insufficient | 15 trials stopped on safety |

## High-impact live observations

### PCSK9 / familial hypercholesterolemia
The card was `GO — convergent`; clinical validation reported 8 disease-scoped Phase-3+ trials and 4 approved drugs. Engagement remained `Insufficient data`. The decision/VOI cell nevertheless said `Fill: a mechanistic bridge to disease`.

### GLP1R / obesity
The card was `GO — clinically validated`. The surface cell said cell-membrane localization "opens antibody, ADC and T-cell-engager formats". Best-fit modality was `small molecule, antibody, splice-switching ASO`. The safety cell called `pancreatitis` and `fatal` named class on-target liabilities across exenatide, liraglutide and semaglutide. The biomarker cell proposed 6 pathogenic variants plus restricted expression in 0 tissues as responder stratifiers.

### TYK2 / psoriasis
The card was `GO — convergent`. The safety cell assigned thrombosis, serious/opportunistic infection and mortality as boxed-warning class liabilities across delgocitinib, deucravacitinib and tofacitinib. The biomarker cell proposed 62 pathogenic variants as a responder stratifier.

### GBA / Parkinson disease
The card was `GATE` and correctly selected `agonize/restore`, but it called GBA cell-surface accessible and proposed small molecule, antibody and splice-switching ASO.

### TREM2 / Alzheimer disease
The card was `GO — genetics-led` and selected `agonize/restore`; clinical validation and engagement were both insufficient, and attrition was only `one terminated trial — readout unverified (business reason)`.

### CETP / coronary artery disease
The card was `GATE`; modality was antibody plus splice-switching ASO. Its phenotype/outcome text said five Phase-3 mechanisms, zero approvals represented a favorable surrogate that did not translate to a hard clinical outcome.

### IL1B / coronary artery disease
The card was `ACQUIRE-EVIDENCE`; direction was inhibit, but disease-specific clinical validation was insufficient. The modality output was small molecule, PROTAC and splice-switching ASO.

### BRD9 / synovial sarcoma
The card was `CLINICALLY_DISCONFIRMED`, scoped to refractory monotherapy. The header asserted FHD-609 engaged BRD9 but produced no clinical response, and used CFT8634 as an independent adequate-engagement failure. The ordinary attrition cell separately read `one terminated trial — readout unverified (business reason)`. Molecular-disease definition was insufficient.

### FLT1 / preeclampsia
The card was `ACQUIRE-EVIDENCE`; genetic link was suggestive; modality was small molecule, antibody and splice-switching ASO. The attrition cell called an approved mechanism in class de-risking, while the clinical-validation cell correctly treated numerous oncology VEGFR drugs as off-indication context rather than preeclampsia validation.

### KRAS / pancreatic cancer
The card was `GATE`. The surface cell said KRAS was cell-surface accessible and therefore opened antibody, ADC and T-cell-engager formats. Best-fit modality was small molecule, antibody and ADC. The safety gate cited NCT06876142 as a safety stop even though its displayed reason was: `Administrative hold pending non-safety related changes to the study design.` Clinical validation mentioned only adagrasib and sotorasib at Phase 2 in pancreatic cancer.

## Cross-case coverage

- `genetic_link`: answered 8/10.
- `direction`: answered 9/10.
- `modality`: answered 10/10.
- `engagement`: answered 0/10.
- `on_target_tox`: answered 10/10.
- `therapeutic_window`: answered 10/10.
- `biomarker`: answered 10/10.
- `molecular_disease`: answered 0/10.
- `clinical_validation`: answered 3/10.
- `attrition`: answered 10/10.
- `competitive_ip`: answered 10/10.
- `population`: answered 10/10.
- `decision`: answered 10/10.

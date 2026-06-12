# TargetVal v3 — Interface Design (Insight-Card front-end)

Net-new design (the two spec docs mandate `reporter.py` + FastAPI + the `InsightCard` schema, but
not a web UI). The gateway's `/chat` page was loved for: free-text input, a resolved-entity line,
async with a time budget, expandable raw drill-down, **key-free by default**, mobile-first, and a
printed report. KEEP all of that. But the v3 page must be **Insight-Card-centric**, not
coverage-centric — it surfaces the epistemic output, not fetch status.

## The shift (why not just reuse the gateway page)
- Gateway page answers: "which of N sources returned data?" (plumbing / coverage).
- v3 page answers: "is this a credible target, and is the evidence *mechanistically connected*?"
- So the centerpiece is the **InsightCard** (see `evidence/models.py`), and specifically the
  causal paths + two-pass convergence. Coverage is demoted to a secondary provenance panel.

## Architecture
- The page is the front-end of `synthesis/reporter.py`. The FastAPI `/api/v1/validate` endpoint
  returns the `InsightCard` JSON; the page renders that object. No separate synthesis path —
  the card IS the output. The key-free deterministic synthesizer generates `conclusion` /
  `confidence_context`; the LLM (Claude) is optional narrative polish only (bounded-LLM principle).
- Serve a single self-contained HTML page (as in the gateway) from FastAPI, or a small SPA — but
  the data contract is the InsightCard, so keep it thin.

## SCORELESS visual language (hard constraint)
NO 0-100 score, no stars, no composite number anywhere. Visual vocabulary is categorical only:
- **Tier chips:** T1 / T2 / T3.
- **Convergence states:** Signal (all-claims) and Mechanism (path-filtered), each a categorical
  label (none / tier3 / tier2 / tier1 / tier2x2 …).
- **Path quality:** strong / moderate / weak.
- **Gap presence**, **contradiction flags**, **mode** (SEARCH/SETTLE).

## Page components (top to bottom)
1. **Input** — free-text "target + indication" box; mode toggle **SEARCH | SETTLE**; submit.
   Reuse the gateway's resolver (alias table, mygene symbol→Ensembl/UniProt, OLS4 condition→EFO,
   Open Targets drug→target). Show the resolved-entity line: `drug X → gene Y (ENSG…, UniProt…) ·
   indication Z (EFO…)`.
2. **Verdict header (scoreless).** One qualitative sentence from `conclusion` + `confidence_context`.
   e.g. "Supported with caveats — strong genetic causality, mechanistically connected, one safety gap."
3. **Two-pass convergence strip — THE SIGNATURE VISUAL.** Show both side by side:
   `Signal (all claims): ● {convergence_all_claims}` and
   `Mechanism (path-filtered): {convergence_path_filtered} · coverage {mechanism_coverage}`.
   When they DISAGREE (Fixture 5: tier1 vs none, coverage 0.0) → loud ⚠ "strong individual claims
   but not mechanistically connected." When they AGREE (Fixture 1) → green "mechanistically validated."
   This disagreement view is the single most important thing the gateway could not show.
4. **Causal paths — the heart.** Render each `supporting_paths[]` entry as a node→edge chain:
   `Variant ─[relation/tier]→ Gene ─…→ Disease`, color-coded by edge tier, labeled with
   `grammar_matched`, `cascade_depth`, `path_quality`. Tap any node/edge → provenance popover
   (source, accession, DOI/PMID, effect_size, p_value, fetched_at). This is what a scientist wants:
   the mechanism, not a bar chart.
5. **Evidence independence (EDG).** `effective_independent_count` with a one-line explanation of the
   discount ("4 raw sources → 3.2 effective; discounted for shared UK Biobank cohort").
6. **Gaps as action cards.** Each `gaps[]` entry = a card with its `description` + `recommendation`
   (domain / cascade: / gpmap: prefixes styled distinctly). This is the typed successor to the
   gateway's "next experiments."
7. **Contradictions.** `contradicting_evidence[]` surfaced explicitly (e.g. safety-vs-efficacy),
   never hidden.
8. **GPMap cross-vector panel** (when `cross_vector_assessment` present): the disease's mechanistic
   arms, which arm(s) the target sits on, `arm_coverage_fraction`, `pleiotropy_bucket`,
   `tissue_specificity_index`, cleanest arm. Only shown when GPMap data exists.
9. **Coverage / provenance panel (DEMOTED, collapsible).** The gateway-style per-domain
   OK/NO_DATA/ERROR rollup, kept as a secondary "what was fetched" view — useful for debugging
   and trust, but no longer the headline.
10. **Raw drill-down** per claim/source (collapsible `<details>`), as in the gateway.

## SEARCH vs SETTLE in the UI
- **SETTLE** → the full rigorous card above (convergence required, tiers gate, paths validated).
- **SEARCH** → faster, broader "candidates + gaps + hypotheses" view; relaxed validation; multiple
  starts encouraged. This is also where v3 MAY call the existing gateway for breadth (see
  BUILD_CONTEXT — gateway is the SEARCH/scouting path, direct typed adapters are the SETTLE path).

## Operational reuse from the gateway page (carry over verbatim-ish)
- Pre-warm the service before the heavy call; hard wall-clock budget on the request so it always
  returns valid JSON (no worker-kill → HTML → JSON.parse crash); defensive parse with a friendly
  retry message; "quick" vs "full" toggle.
- Self-contained HTML, dark theme, mobile-first, example-query links.

## Print/share
Keep a clean "print report" affordance (the user explicitly valued this). An InsightCard prints
naturally as: verdict → convergence → paths (as text chains) → independence → gaps → provenance.

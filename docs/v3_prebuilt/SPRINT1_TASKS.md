# TargetVal v3 — Sprint 1 Task Checklist (Genetics Vertical Slice)

Goal: prove the epistemic core end-to-end on the **Genetics** domain with a few sources, and
**make gold Fixtures 1 (PCSK9) and 5 (disconnected pile) pass**. If those two pass, the hardest
ideas in the spec (typed claims, validation, evidence graph, causal paths, two-pass convergence,
EDG) are proven. Everything else is replication.

Authoritative contract: `docs/TargetVal_v3_Repo_Spec_SPECFREEZE_v2.docx`. This checklist maps that
contract to concrete files + acceptance checks for the slice. Counts in the spec's CANONICAL COUNTS
block are the single source of truth.

Pre-built drafts to drop in from `docs/v3_prebuilt/`:
- `src/entities.py` → `src/targetval/canonical/entities.py`
- `src/models.py` → `src/targetval/evidence/models.py`
- `registry/*.yaml` → `src/targetval/sources/registry/` (all 12 Wave-1)

---

## 0. Repo skeleton (SPECFREEZE Part VI) — REQUIRED before anything else
- [ ] `pyproject.toml` (uv, Python 3.12+), deps: pydantic v2, httpx, networkx, pyyaml,
      pytest, pytest-asyncio, anyio. (Hold Kùzu/Neo4j, LangGraph, postgres, redis until later.)
- [ ] Create the full `src/targetval/...` tree from Part VI. Every file exists, tagged
      REQUIRED / STUB / DEFERRED. STUBs have correct signatures + docstrings, may return placeholders.
- [ ] `tests/{unit,integration,fixtures}/` exist. `tests/fixtures/` holds the 8 gold fixtures
      (only 1 and 5 must pass this sprint) + mocked API payloads.
- [ ] `mypy`/`ruff` config; CI optional but recommended.

## 1. Canonical layer — REQUIRED
- [ ] Drop in `entities.py` (11 entities + enums). Verify field-for-field vs spec §4.
- [ ] `canonical/resolver.py` — `IdentityResolver.resolve_gene/disease` with precedence
      (HGNC→Ensembl→MyGene→NCBI; MONDO→EFO→MeSH→DOID), ambiguity→QUARANTINE, `resolution_confidence`.
      Sprint-1 minimum: genes + diseases only. Reuse the gateway's mygene + OLS4 logic (see BUILD_CONTEXT §6).
- [ ] **Round-trip test:** every model serializes to JSON and back (acceptance criterion #1).

## 2. Sources / adapters (genetics subset) — REQUIRED
- [ ] Drop in the 12 registry YAMLs; validate them against the §16 schema (acceptance #2).
- [ ] `sources/adapters/base.py` — `SourceAdapter` Protocol, `FetchOrchestrator` (async, concurrent),
      `CircuitBreaker`, `TokenBucket`, YAML registry loader.
- [ ] Genetics adapters for the slice: `opentargets.py` (l2g/coloc/pqtl), `open_gwas.py` (mr),
      `clinvar.py` (rare), `gtex.py` (sqtl). Adapters ONLY auth/request/paginate/retry/parse —
      they do NOT interpret evidence or assign tiers (spec §17).
- [ ] Adapter emits `RawRecord`s from **mocked** payloads (acceptance #3) — use httpx mock transport;
      no live network needed for fixtures.

## 3. Normalization — REQUIRED
- [ ] `evidence/models.py` (drop in `models.py`). Verify vs spec §5.
- [ ] `evidence/relations.py` — the 19-relation catalog with subject→object constraints, tier
      defaults, family bindings, disambiguation rule (relation + module_key + qualifiers).
- [ ] `evidence/normalizers/genetics.py` — RawRecord → `TypedClaim`s. Source-specific field mapping.
      Extract L2G/V2G component features → `EvidenceContext.cascade_layers`. Store cis-QTL β as metadata.
      **This is the real work of the sprint** — most bugs live here. (acceptance #5)

## 4. Validation — REQUIRED (genetics chain only this sprint)
- [ ] `validation/validators.py` — implement at least: 5 mechanical (schema, identifiers, provenance,
      dedup, range) + genetics method validators (MR thresholds, coloc PP.H4, GWAS p-value) +
      the QTL cascade validators. Each `(TypedClaim, EvidenceContext) -> ValidationResult`.
      (Full set is 33; this sprint needs the genetics-relevant subset working — spec acceptance #6/#7.)
- [ ] `validation/chains.py` — `GENETICS_CHAIN` runs its validators in order; `compose()` helper.
- [ ] `validation/quarantine.py` — quarantine store + appeal logic. PASS → graph; QUARANTINE → audit.

## 5. Evidence graph — REQUIRED (in-process this sprint)
- [ ] `graph/schema.py` — node (11 types) + edge (19 relation types) definitions.
- [ ] `graph/store.py` — **networkx-backed** implementation of the store interface (typed edges,
      provenance, simple temporal versioning). Keep the interface clean so Kùzu/Neo4j can swap in later.
- [ ] `graph/algorithms.py` — shortest causal paths / subgraph extraction (3-hop conclusion scope).

## 6. Synthesis core — REQUIRED (the proof)
- [ ] `synthesis/tiers.py` — `assign_tier()` + `cascade_depth_bonus()`; `check_tiered_convergence()`
      for ALL claims and for PATH-ELIGIBLE claims; `compute_mechanism_coverage()`. QTL→cascade-layer map.
- [ ] `synthesis/causal_paths.py` — `CausalPathValidator.find_valid_paths()`. Implement **Grammars
      1A/1B/1C/1D** (genetics cascade by depth) this sprint. A valid path: crosses ≥3 entity types,
      ≥1 mechanistic bridge edge, anchored by a causal/genetic edge. `path_quality` strong/moderate/weak.
      Path-search rules from spec §14a: 3-hop scope, max 6 edges, DAG-only (break cycles by higher tier),
      retain top-5 per grammar, bridge_tier = min(tier of bridge edges), dedup by entity-id order.
- [ ] `synthesis/dependency_auditor.py` — build EDG per conclusion; `effective_independent_count`;
      6-level discount schedule; automated pairwise detection from shared DOI/dataset/cohort/consortium.
- [ ] `synthesis/gaps.py` — `detect_gaps()`. This sprint: the 6 domain-level + 6 cascade-level gaps.
- [ ] `synthesis/coordinator.py` — the 10-step flow (spec §12). **Two passes are first-class steps:**
      Step 3 broad convergence (all claims → `convergence_all_claims`), Step 5 path-filtered convergence
      (path-eligible only → `convergence_path_filtered`, `mechanism_coverage`). Order-invariant aggregation.
      (Defer Step 4.5 cross-vector / GPMap to the GPMap vertical.)
- [ ] `synthesis/reporter.py` — non-binding `InsightCard` narrative.

## 7. The acceptance tests (this is the sprint's definition of done)
- [ ] **Fixture 1 — PCSK9 + cardiovascular:** strong card. Build mocked claims = 3×Tier1
      (MR, Mendelian, clinical trial) + 3×Tier2 + 3×Tier3, cascade depth 3+ on a genetics path.
      ASSERT: Grammar 1C satisfied, `path_quality='strong'`, `convergence_all_claims='tier1'`,
      `convergence_path_filtered='tier1'`, `mechanism_coverage>=0.85`, only gap == safety_gap.
- [ ] **Fixture 5 — disconnected evidence pile:** MR Tier1 + expression Tier2 + pathway Tier3 but
      NO valid path connecting them. ASSERT: `convergence_all_claims='tier1'` (MR alone passes Pass 1),
      `convergence_path_filtered='none'` (no claim in any valid path), `mechanism_coverage=0.0`,
      card emitted WITH `mechanism_gap` + high-severity warning. **This fixture is why the two-pass
      design exists — do not collapse the passes.**
- [ ] **Order-invariance smoke test:** shuffle domain/claim order; assert <10% variance on the card's
      convergence fields for Fixtures 1 and 5 (spec acceptance #18).

## 8. Definition of done for Sprint 1
All of: models round-trip (✔#1), YAMLs validate (✔#2), an adapter emits RawRecords from mocks (✔#3),
gene/disease resolution works (✔#4), genetics normalizer produces packets (✔#5), genetics validators
run in order (✔#7), causal paths found for 1A–1D and rejected when invalid (✔#10), cascade depth
counted (✔#11), two-pass convergence correct on fixtures (✔#13/#14/#15), Fixtures 1 & 5 pass,
order-invariance passes (✔#18). Then: replicate the pattern to the other 5 domains; add GPMap
(Grammar 7 + Step 4.5) and perturbation (Grammar 5) as the next two verticals.

## 9. Deliberately deferred (do NOT build this sprint)
Kùzu/Neo4j, full LangGraph multi-agent topology, GPMap cross-vector + Grammars 6/7, X-Atlas/Pisces
perturbation, the full 33-validator set, monitoring suite, MCP+FastAPI surface, Wave-2/3 sources,
direction classifier, dose anchoring. All are in the spec; none are needed to prove the core.

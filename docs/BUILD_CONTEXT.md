# TargetVal v3 — Build Handoff & Context

**Purpose of this file.** This is the durable memory for building **TargetVal v3.0** in the
`aureten/targetval03` repository. It exists because the work was designed in a Claude Code
session bound to a *different* repo (`aureten/Targetval-gateway`), and a fresh session on
`targetval03` starts with no memory of that conversation. **A new session should read this file
first, then the two spec documents.** Everything here that isn't in the specs (decisions +
live-validated source knowledge) was learned the hard way and is not recoverable elsewhere.

---

## 0. Read order for a new session
1. This file (`docs/BUILD_CONTEXT.md`) — decisions, status, and source knowledge.
2. `docs/TargetVal_v3_Repo_Spec_SPECFREEZE_v2.docx` — **the normative engineering contract.**
   Authoritative for every model, validator, relation, YAML schema, coordinator rule, repo tree,
   and acceptance criterion. When in doubt, this wins.
3. `docs/TargetVal_3_0_Blueprint_Integrated_GPMap.docx` — the 7-layer architecture + scientific
   rationale (QTL cascade, GPMap cross-vector, contamination control, sprint plan).
> If the two `.docx` files are not yet in `docs/`, ask the user to commit them (GitHub web upload,
> or hand them to the session as attachments). They are the source of truth.

The SPECFREEZE's **CANONICAL COUNTS** block is the single source of truth for totals:
11 canonical entities · 19 relation types · 33 validators · 9 grammar labels (8 patterns) ·
16 gap types · 84 modules · 32 acceptance criteria · 8 gold fixtures · 28 dependency pairs ·
12 Wave-1 sources · 10 coordinator steps.

---

## 1. Locked-in decisions (do not relitigate without the user)
- **Fresh build, not a fork.** `targetval03` is built to the spec's repo tree (Part VI). The old
  `Targetval-gateway` is NOT cloned — its code structure is incompatible. We reuse *knowledge*
  (the source registry below), not code.
- **Strangler approach.** `Targetval-gateway` stays deployed as a Layer-1 fetch backbone, a
  live API-validation harness, and a demo UI while v3 is built clean beside it.
- **Genetics-vertical-first, not big-bang.** Do NOT attempt all 84 modules at once. Prove the
  epistemic core on one domain end-to-end, then replicate. (See §3.)
- **Defer the heavy infra and Wave-3 until the core is proven:** start with an in-process
  `networkx` graph (swap to Kùzu/Neo4j later), defer full multi-agent LangGraph, GPMap
  cross-vector, and X-Atlas/Pisces to later verticals.
- **Timelines are explicitly irrelevant to the user.** Optimize for correctness against the
  spec, not speed. The spec's "8-week / 4-sprint" framing is aspirational; ignore the calendar.
- **Tech stack (from Blueprint 15.2):** Python 3.12+, Pydantic v2, httpx (async), LangGraph,
  KùzúDB(dev)/Neo4j(prod), PostgreSQL 16, Redis 7, FastAPI + MCP (SSE), pytest + pytest-asyncio,
  `uv` for packaging, Docker, Render→AWS. Claude API used ONLY for bounded semantic validation
  and narrative reporting (never as a primary evidence source or scorer).

---

## 2. Why the design is worth building (the core ideas to protect)
- **Causal path is the unit of knowledge**, not the isolated claim. Two-pass convergence:
  Pass 1 over ALL validated claims (any signal), Pass 2 over PATH-ELIGIBLE claims only
  (mechanistically connected). This is the heart of the system — Fixture 5 (disconnected
  evidence pile: strong individual claims, no valid path → `convergence_path_filtered='none'`,
  `mechanism_coverage=0.0`) is the acid test. Implement both passes as first-class coordinator
  steps; never collapse them.
- **Scoreless + provenance-first.** No composite/numeric user-facing score. Categorical tiers
  (1/2/3) only. Every claim traces to source/record/accession/DOI-PMID/method/timestamp.
- **Dependency-aware independence (EDG).** ≥2 *orthogonal* (dependency-discounted) evidence
  families required to emit an Insight Card. Discount shared dataset/cohort/consortium/ontology.
- **QTL cascade depth** grades genetics by biological layers traversed (Grammars 1A–1D).
- **Contamination control:** domain builders are isolated (no cross-reading); order-invariance
  (<10% variance on gold fixtures when domain order is shuffled).

---

## 3. Build plan & current status
**STATUS: nothing built in `targetval03` yet. This file is commit #1 of the context.**

### Sprint 0 — Source registry (research, not code) — START HERE
Produce `src/targetval/sources/registry/*.yaml` for the 12 Wave-1 sources, per the SPECFREEZE
§16 schema. **§5 of this file already contains the live-validated data for most of them** — this
is the head start. For each source confirm: endpoint, api_type, auth, rate limits, retry,
evidence_families, modules_served, dependency_tags.

### Sprint 1 — Genetics vertical slice (the proof)
Build end-to-end on Genetics only, with a handful of sources:
`pyproject.toml`(+uv, py3.12) → canonical entity models (§4.x of spec) → `TypedClaim`/`CausalPath`/
`InsightCard`/`GapReport`/`Provenance`/`DependencyTag`/`EvidenceContext` models → genetics
normalizer → genetics validators → in-process `networkx` evidence graph → `CausalPathValidator`
matching Grammars 1A–1D → coordinator two-pass convergence + EDG + gap detection → one InsightCard.
**Acceptance = two gold fixtures pass:**
- **Fixture 1 (PCSK9 + cardiovascular):** strong card, Grammar 1C, cascade depth 3+,
  `path_quality='strong'`, `convergence_all_claims='tier1'`, `convergence_path_filtered='tier1'`,
  `mechanism_coverage≥0.85`, only a safety gap.
- **Fixture 5 (disconnected pile):** MR Tier1 + expr Tier2 + pathway Tier3 but NO valid path →
  `convergence_all_claims='tier1'`, `convergence_path_filtered='none'`, `mechanism_coverage=0.0`,
  card emitted with `mechanism_gap` + high-severity warning. This fixture validates the two-pass
  architecture specifically.
If those two pass, the hardest ideas in the whole spec are proven. Then replicate to the other
5 domains, then add GPMap (Grammar 7, cross-vector Step 4.5) and perturbation (Grammar 5).

### Later
Wave-2/3 sources, Kùzu/Neo4j swap, full LangGraph multiagent, monitoring suite, MCP+FastAPI,
Sprint 5+ (Grammar 6 genetics-perturbation convergence, direction classifier, dose anchoring).

---

## 4. Open questions to confirm with the user BEFORE committing the relevant pieces
- **Data access / licensing:** X-Atlas/Pisces (large HuggingFace h5ad, CC BY-NC-SA = non-commercial,
  needs pre-indexing to per-gene signatures); DrugBank (commercial license); GPMap API
  (`gpmap.opengwas.io` — new, stability unproven). Treat as PENDING until confirmed.
- **API keys (free, user to obtain):** `OPENGWAS_JWT` (api.opengwas.io) and a BioGRID access key.
  These gate several genetics + perturbation sources.
- **Hosting:** the v3 stack (graph DB + Postgres + Redis + LangGraph + per-query LLM) outgrows a
  Render Starter instance quickly. Decide hosting before the production milestone.
- **Hard-coded scientific constants** (GPMap RS=0.93 / 1.8-vs-3.1 drug-success; 14.5% FN / 4% FP
  recovery; H4≥0.8; connectedness≥0.70; F1≥0.75/0.5 tiers): store as cited, versioned constants,
  never silently embedded.

---

## 5. LIVE-VALIDATED SOURCE REGISTRY KNOWLEDGE (the crown jewel — only exists here)
Learned by debugging all ~74 adapters of `Targetval-gateway` against the live APIs in mid-2026.
These corrections are NOT in the spec and will save Sprint 0 from repeating dead ends.

### Confirmed-good endpoints / contracts
| Source | Correct endpoint & call contract |
|---|---|
| **Open Targets** | `https://api.platform.opentargets.org/api/v4/graphql` (POST GraphQL). The old `api.opentargets.io/v3` + `gene(ensemblId:)` schema is **RETIRED** — use `target(ensemblId:){ approvedSymbol associatedDiseases(page:{index:0,size:N}){ count rows{ disease{id name} score datatypeScores{id score} } } }`. Genetic-association evidence lives in the `genetic_association` datatype score. Drug→target lookup: `search(queryString:$q, entityNames:["drug"]){hits{object{... on Drug{mechanismsOfAction{rows{targets{approvedSymbol}}}}}}}`. |
| **GTEx** | `https://gtexportal.org/api/v2` — the **v1 REST API is decommissioned**. Expression: `/api/v2/expression/geneExpression?gencodeId=<VERSIONED ensembl id>`. sQTL: `/api/v2/association/singleTissueEqtl`. NOTE: v2 wants a *versioned* gencodeId (ENSG…​.NN) — unversioned may fail; a resolve step is likely needed. |
| **UniProt** | `https://rest.uniprot.org/uniprotkb/search` — query by `accession:<UNIPROT>` plus a `fields=` list and `format=json`. Old Lucene `annotation:(type:PTM)` syntax is INVALID. For structure xrefs: `fields=xref_pdb,xref_alphafolddb,structure_3d`. |
| **ClinicalTrials.gov** | v2 API `https://clinicaltrials.gov/api/v2/studies` uses **`query.cond` / `query.term`** params (NOT `cond`/`term`). |
| **openFDA** | `https://api.fda.gov/drug/event.json`. **404 means "no records" → treat as NO_DATA**, not error. `count` param must be a *field name*, not a number. FAERS indexes drug names, not gene targets — a bare gene query is semantically empty (use `patient.drug.openfda.substance_name:"X"`). |
| **gnomAD** | `https://gnomad.broadinstitute.org/api` is **GraphQL (POST)** — needs a real query, e.g. `query($s:String!){gene(gene_symbol:$s,reference_genome:GRCh38){gnomad_constraint{pli oe_lof lof_z mis_z syn_z}}}`. An empty POST 400s. |
| **Ensembl VEP** | `https://rest.ensembl.org/vep/human/region` is **variant-level** — needs an rsid/region. A gene-only query has no input → should be NO_DATA, not an error. |
| Reliable as-is | NCBI E-utilities (`eutils.ncbi.nlm.nih.gov` — send a descriptive User-Agent), EuropePMC (`ebi.ac.uk/europepmc/webservices/rest/search`), STRING (`string-db.org/api`), OmniPath (`omnipathdb.org/interactions`), Reactome ContentService, EBI ProteomicsDB/GXA/ComplexPortal, PatentsView (returned data in testing), Pharos (`pharos-api.ncats.io/graphql`), Expression Atlas. |

### Sources that need an API key/token (configure via env; no-op until set)
- **OpenGWAS** (`gwas-api.mrcieu.ac.uk`) now requires a **JWT** (`Authorization: Bearer $OPENGWAS_JWT`).
  Unauthenticated requests hang/time out. Affects MR, consortia, phewas modules.
- **BioGRID ORCS** (`webservice.thebiogrid.org/ORCS`) requires an `accesskey=$BIOGRID_API_KEY` query param.
  Affects functional-genomics / CRISPR-screen / dependency modules.

### Known-fragile / needs-validation upstreams (don't trust blind)
PatentsView old `api.patentsview.org` may be migrating to `search.patentsview.org`; MetaboLights
(`ebi.ac.uk/metabolights/ws`), PRIDE, Human Cell Atlas (`azul.data.humancellatlas.org`), CELLxGENE,
HuBMAP, IEDB tools API, MaveDB/ClinicalGenome GraphQL, CellModelPassports, iLINCS/LINCS portal,
HPO/Monarch (API v3 path changes), CADD, WHO ICTRP (not a JSON API). Validate each individually.

### Wave-1 12-source manifest (from SPECFREEZE §18, cross-checked above)
opentargets(gql) · open_gwas(rest, needs JWT) · clinvar(rest, eutils, key) · gtex(rest v2) ·
expression_atlas(rest) · chembl(rest) · pharos(gql) · reactome(rest) · intact(psicquic) ·
europe_pmc(rest) · openfda(rest) · clinicaltrials(rest v2).
Dependency tags (discounts) per spec: opentargets↔gwas_catalog(0.8)/efo(0.2); open_gwas↔gwas_catalog(0.6);
gtex↔eqtl_catalogue(0.8); chembl↔drugbank(0.6); pharos↔chembl(0.4); reactome↔pathway_commons(0.9);
intact↔string(0.6); europe_pmc↔pubmed(0.9); openfda↔drugcentral(0.6).

---

## 6. Reusable assets from `Targetval-gateway` (reference, not copy)
- The corrected adapter URLs/params above (Sprint-0 head start).
- Gene/EFO/drug→target resolution logic (maps to Layer-2 identity resolution): alias table for
  common targets, `mygene.info` for symbol→Ensembl/UniProt, EBI OLS4 for condition→EFO,
  Open Targets MoA for drug→target.
- The deterministic rule-based synthesizer concept (coverage→narrative) as a fallback reporter.
- `aureten/targetval-mcp-server` exists and aligns with the spec's required MCP interface.
- The gateway's `/chat` UI pattern is a usable Insight-Card front-end later.

---

## 7. First actions for the new session (concrete)
1. Confirm the two `.docx` specs are in `docs/`. If not, ask the user to add them.
2. Scaffold the repo skeleton from SPECFREEZE Part VI (`src/targetval/...` tree, `pyproject.toml`
   with uv + py3.12, every file as REQUIRED/STUB/DEFERRED per the spec).
3. Write the Wave-1 `sources/registry/*.yaml` using §5 above.
4. Implement the Sprint-1 genetics vertical and make Fixtures 1 and 5 pass (`tests/fixtures/`).
5. Keep this file updated with a running "where we are" status as the build progresses.

## 8. PRE-BUILT DRAFTS (in `docs/v3_prebuilt/` — drop straight in, then verify vs spec)
These were drafted in the gateway session with full context, to save the new session time. Treat as
faithful drafts, not gospel — reconcile against the SPECFREEZE before freezing.
- `docs/v3_prebuilt/src/entities.py`  → `src/targetval/canonical/entities.py` (11 entities + all enums)
- `docs/v3_prebuilt/src/models.py`    → `src/targetval/evidence/models.py` (TypedClaim, CausalPath,
  InsightCard, CrossVectorAssessment, etc.)
- `docs/v3_prebuilt/registry/*.yaml`  → `src/targetval/sources/registry/` (all 12 Wave-1 sources,
  with the live-validated endpoints + auth + dependency tags from §5 already filled in)
- `docs/v3_prebuilt/SPRINT1_TASKS.md` → the concrete Sprint-1 task checklist + the two gold-fixture
  acceptance assertions. **Start here.**
- `docs/v3_prebuilt/INTERFACE_DESIGN.md` -> the Insight-Card web UI design (the v3 successor to the gateway's /chat page): two-pass convergence strip, causal-path visualization, EDG independence, typed gap cards, SEARCH/SETTLE modes, scoreless visual language.


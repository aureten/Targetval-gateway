# Targetval Gateway

Biologic target validation API gateway — robust, key-enforced, and wired to **live public data feeds** (no stubs, no placeholders).

- **Live service:** https://targetval-gateway.onrender.com  
- **Interactive OpenAPI UI:** add `/docs` to the base URL (FastAPI default).  

This gateway fronts 30+ programmatic endpoints for **target–disease evidence aggregation** across genetics, expression, pathways, tractability, safety, and IP.  

---

## ✨ Features

- ✅ **Real API key enforcement** via middleware (`app/middleware/api_key.py`).  
- ✅ **Environment-variable config** with `.env.example` checked in.  
- ✅ **EFO fallback:** supports both `?efo=EFO_…` and free-text `?condition=asthma`.  
- ✅ **Bounded retries & backoff:** resilient HTTP client for upstream APIs.  
- ✅ **Structured `Evidence` payloads:** with `status`, `fetched_n`, `citations`, and timestamp.  
- ✅ **NO_DATA vs ERROR:** upstream empties return `status="NO_DATA"`, true failures → `ERROR`.  

---

## 🚀 Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env  # edit values

# Run the app (FastAPI "app" in app/main.py):
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
Render/Heroku-style deploys: use the included Procfile.
🔑 Authentication
All non-public paths require an API key.
Header (recommended):
X-API-Key: <your_key>
Bearer:
Authorization: Bearer <your_key>
Query (fallback):
?api_key=<your_key>
Env vars:
ENFORCE_API_KEY=true
ALLOWED_API_KEYS=dev123,another_key
ALLOW_ANONYMOUS_PATHS=/ /docs /redoc /openapi.json /healthz
🌐 EFO resolution
Free-text ?condition= is translated to an ontology ID:
EFO_RESOLVE_STRATEGY=ols        # ols | opentargets | none
EFO_RESOLVE_REQUIRED=true       # error if resolution fails
ols (default): EMBL-EBI OLS4 REST API
opentargets: OpenTargets disease resolver
none: strict mode (must pass ?efo=)
🧪 Example queries
Genetics
# Locus-to-gene (GWAS Catalog, enriched target)
curl "$BASE/genetics/l2g?gene=EGFR&efo=EFO_0003071&limit=20"

# Mendelian (Monarch, use NCBI Gene ID)
curl "$BASE/genetics/mendelian?gene=NCBIGene:1956&limit=20"

# miRNA interactions (ENCORI fallback)
curl "$BASE/genetics/mirna?gene=EGFR&limit=50"

# sQTL (use Ensembl ID)
curl "$BASE/genetics/sqtl?gene=ENSG00000146648&limit=20"

# Epigenetic tracks (ENCODE, prefer Ensembl ID)
curl "$BASE/genetics/epigenetics?gene=ENSG00000146648&limit=20"
Associations
# Bulk RNA (GTEx: controlled vocab tissues)
curl "$BASE/assoc/bulk-rna?condition=Whole_Blood&limit=25"

# Bulk protein (ProteomicsDB/PRIDE)
curl "$BASE/assoc/bulk-prot?condition=non-small cell lung carcinoma&limit=25"

# Single-cell signal (HPA)
curl "$BASE/assoc/sc?condition=T cell&limit=50"
Expression & Localization
curl "$BASE/expr/baseline?symbol=EGFR&limit=25"
curl "$BASE/expr/localization?symbol=EGFR&limit=25"
curl "$BASE/expr/inducibility?symbol=EGFR&limit=25"
Mechanistic Wiring
curl "$BASE/mech/pathways?symbol=EGFR&limit=25"
curl "$BASE/mech/ppi?symbol=EGFR&cutoff=0.7&limit=50"
curl "$BASE/mech/ligrec?symbol=EGFR&limit=50"
Tractability
# Known drugs (OpenTargets first, DGIdb fallback)
curl "$BASE/tract/drugs?symbol=EGFR&limit=50"

# Small molecules, antibodies, oligos
curl "$BASE/tract/ligandability-sm?symbol=EGFR&limit=25"
curl "$BASE/tract/ligandability-ab?symbol=EGFR&limit=25"
curl "$BASE/tract/ligandability-oligo?symbol=EGFR&limit=25"

# Integrated modality scoring
curl "$BASE/tract/modality?symbol=EGFR"
Clinical
# Clinical endpoints (Ct.Gov v2 + v1 fallback)
curl "$BASE/clin/endpoints?condition=non-small cell lung carcinoma&limit=3"

# Real-world evidence (FAERS + observational CTs)
curl "$BASE/clin/rwe?condition=asthma&limit=50"

# Safety signals (FAERS)
curl "$BASE/clin/safety?symbol=ADALIMUMAB&limit=50"

# Pipeline (Inxight + fallback to OT/DGIdb)
curl "$BASE/clin/pipeline?symbol=EGFR&limit=50"
Competition & IP
# Patent intensity
curl "$BASE/comp/intensity?symbol=EGFR&condition=lung carcinoma&limit=50"

# Freedom-to-operate
curl "$BASE/comp/freedom?symbol=EGFR&limit=50"
🌟 Good starter queries
These are known to return rich results across most modules:
Gene symbols: EGFR, BRAF, JAK2, TNF, PCSK9
Ensembl IDs: ENSG00000146648 (EGFR), ENSG00000157764 (BRAF)
NCBI Gene IDs: NCBIGene:1956 (EGFR), NCBIGene:673 (BRAF)
Diseases: "non-small cell lung carcinoma" (EFO_0003071), "asthma" (EFO_0000270)
Tissues: Lung, Whole_Blood, Brain_Cortex, Muscle_Skeletal
🛠 Environment variables
See .env.example for a ready-to-use template.
# API key enforcement
ENFORCE_API_KEY=true
ALLOWED_API_KEYS=dev123,another_key
ALLOW_ANONYMOUS_PATHS=/ /docs /redoc /openapi.json /healthz

# EFO resolution
EFO_RESOLVE_STRATEGY=ols
EFO_RESOLVE_REQUIRED=true
REQUEST_TIMEOUT_SECONDS=10

# Upstream degraded mode
ALLOW_UPSTREAM_DEGRADED=true

# Optional: BioGRID ORCS key for perturbation endpoints
ORCS_ACCESS_KEY=<your_biogrid_key>
📦 Deploy on Render
See docs/DEPLOY_RENDER.md for full details.
Build command: pip install -r requirements.txt
Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT or use Procfile
Healthcheck: /healthz
🧭 Notes on upstreams
The gateway proxies many public life-science APIs:
OpenTargets GraphQL/REST (targets, drugs, genetics)
OLS4 (EFO) ontology lookup
GTEx, ProteomicsDB, PRIDE, HPA, STRING, ENCODE, Monarch, PDBe, ChEMBL
openFDA FAERS, ClinicalTrials.gov v1+v2, PatentsView
Each call is wrapped with retry/backoff and classified as OK, NO_DATA, or ERROR.
🤝 Contributing
Keep endpoints small and stateless.
Guard IO with strict timeouts and caching.
Fail early with actionable errors.
PRs welcome for:
extra ontology resolvers (MONDO, DOID)
more upstream fallbacks
richer scoring heuristics
📄 License
Apache-2.0 (same as most scientific infra; adjust if your repo specifies otherwise).
📚 References
Open Targets Platform API
OLS4 (EFO) REST
GTEx Portal API
STRING API
ClinicalTrials.gov API
Last updated: 2025-09-24

---

✅ Drop this in as `README.md`. It’s structured, enriched, has tuned examples, and a clear starter section so users avoid confusion with `NO_DATA`.  

Would you like me to also create a **`docs/EXAMPLES.md`** with even more cut-and-paste curls (grouped by bucket) so your README stays compact, and advanced users can dive deeper?

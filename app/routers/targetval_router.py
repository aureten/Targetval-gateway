❌ Not Exposed (Hidden from Public API)
genetics-3d-maps – The 3D chromatin interaction (Hi-C) module is not in the public schema.
→ Fix: Expose /genetics/3d-maps endpoint in the YAML registry; link to OpenTargets GraphQL “chromatin_interaction” or ENCODE Hi-C dataset via a proxy microservice.
genetics-annotation – Genomic feature annotation layer removed from public builds.
→ Fix: Re-enable the variant_annotation index in the registry YAML and connect it to OpenTargets “variant_annotation_summary” GraphQL field.
genetics-caqtl-lite – Chromatin accessibility QTL module excluded for performance.
→ Fix: Add CAQTL dataset reference (ATAC-seq QTL) from GTEx or eQTL Catalogue; include in module list.
genetics-chromatin-contacts – Promoter–enhancer Hi-C links unavailable.
→ Fix: Reinstate genetics-chromatin-contacts module using ENCODE or Activity-by-Contact (ABC) contact matrices; register the endpoint.
genetics-functional – Functional variant enrichment module not deployed.
→ Fix: Map to OpenTargets GraphQL functionalVariantAnnotation query or internal ML-based functional enrichment API.
genetics-lncrna – Long non-coding RNA mapping module hidden.
→ Fix: Enable lncRNA–gene overlap dataset (from GENCODE or LNCipedia) and register under genetics-lncrna.
genetics-mavedb – MAVEdb (multiplex assay) integration turned off.
→ Fix: Activate MAVEdb REST API calls; include variant effect scoring ingestion.
genetics-mirna – microRNA–target interaction module missing.
→ Fix: Add miRTarBase or TargetScan mapping source; define module key genetics-mirna in registry.
genetics-pathogenicity-priors – Variant pathogenicity priors not exposed.
→ Fix: Link to CADD/REVEL scoring datasets; enable backend endpoint /genetics/pathogenicity-priors.
⚠️ Errored / Deprecated
genetics-consortia-summary – 404 “Not Found.” Deprecated internal endpoint.
→ Fix: Re-point to https://api.platform.opentargets.org/api/v4/graphql and query consortiaStudySummary field; update endpoint URL in config.
genetics-l2g – Upstream 599 error due to schema mismatch (id field removed).
→ Fix: Update GraphQL query to drop the id field under l2GPredictions; use gene { id symbol } score structure.
⚙️ Not Implemented
genetics-nmd-inference – Module stub present but composite logic not wired.
→ Fix: Implement combined sQTL + coding variant inference logic; define nmd_inference function pulling from GTEx + Ensembl VEP.
🔒 Auth-locked (for completeness)
genetics-mr – Mendelian Randomization (OpenGWAS) endpoint JWT-protected.
→ Fix: Add JWT token header support or route through an authenticated proxy

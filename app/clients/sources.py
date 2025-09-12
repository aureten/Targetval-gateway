# app/clients/sources.py
from __future__ import annotations
from typing import Optional, Any
import urllib.parse, json
from app.utils.http import get_json, post_json

# ================
# BUCKET 1 SOURCES
# ================

# Open Targets Genetics (GraphQL) — L2G colocalization
async def ot_genetics_l2g(ensembl_gene_id: str, efo_id: str) -> dict:
    gql = "https://genetics.opentargets.org/graphql"
    payload = {
        "query": """
        query Q($geneId:String!, $efoId:String!){
          target(ensemblId:$geneId){ id approvedSymbol }
          disease(efoId:$efoId){ id name }
          colocalisationByGeneAndDisease(geneId:$geneId, efoId:$efoId){
            studyId phenotypeId geneId diseaseId yProbaModel yProbaCc hasColoc hasColocConsensus
          }
        }""",
        "variables": {"geneId": ensembl_gene_id, "efoId": efo_id}
    }
    body = await post_json(gql, payload)
    return {"coloc": ((body.get("data", {}) or {}).get("colocalisationByGeneAndDisease") or []), "citations": [gql]}

# gnomAD GraphQL — constraint
async def gnomad_constraint(symbol: str) -> dict:
    gql = "https://gnomad.broadinstitute.org/api"
    q = {
        "query": """
        query Rare($symbol:String!){
          gene(gene_symbol:$symbol, reference_genome:GRCh38){
            gene_id gene_symbol
            constraint{ lof_z n_lof expected_lof pLI }
          }
        }""",
        "variables": {"symbol": symbol}
    }
    body = await post_json(gql, q)
    return {"gene": (body.get("data", {}) or {}).get("gene"), "citations": [gql]}

# Monarch — Mendelian associations (symbol→Entrez is fuzzy; you’ll refine later)
async def monarch_mendelian(symbol_or_entrez: str) -> dict:
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol_or_entrez)}/diseases"
    try:
        js = await get_json(url)
        return {"associations": js.get("associations", []), "citations": [url]}
    except Exception:
        return {"associations": [], "citations": [url]}

# IEU OpenGWAS — placeholder (requires compute to run MR at scale)
def ieu_mr_stub(exposure_id: str, outcome_id: str) -> dict:
    return {"assumptions": "MR compute not executed in gateway", "exposure_id": exposure_id, "outcome_id": outcome_id,
            "citations": ["https://gwas.mrcieu.ac.uk/"]}

# lncRNA/circRNA — stub (browser/file-first)
def lncrna_stub(symbol: str) -> dict:
    return {"note": "NONCODE/LncBook/circBase not API-native", "symbol": symbol,
            "citations": ["http://www.noncode.org/", "https://bigd.big.ac.cn/lncbook/", "http://www.circbase.org/"]}

# miRNA — stub
def mirna_stub(symbol: str) -> dict:
    return {"note": "miRTarBase/TargetScan not used programmatically here", "symbol": symbol,
            "citations": ["https://mirtarbase.cuhk.edu.cn/", "https://www.targetscan.org/"]}

# eQTL Catalogue — gene-level
async def eqtl_catalogue_gene(symbol: str) -> dict:
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    js = await get_json(url)
    return {"results": js if isinstance(js, list) else [], "citations": [url]}

# ENCODE — epigenetics (ChIP-seq metadata)
async def encode_chipseq(symbol: str) -> dict:
    q = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{q}&format=json"
    js = await get_json(url)
    return {"experiments": js.get("@graph", []) if isinstance(js, dict) else [], "citations": [url]}

# ==============================
# BUCKET 2 — disease association
# ==============================
async def expression_atlas_experiments(condition: str) -> dict:
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    js = await get_json(url)
    return {"experiments": js.get("experiments", []) if isinstance(js, dict) else [], "citations": [url]}

async def pride_projects(condition: str) -> dict:
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    js = await get_json(url)
    return {"projects": js if isinstance(js, list) else [], "citations": [url]}

def cellxgene_stub(condition: str) -> dict:
    return {"note": "Use cellxgene/HCA portals; not API-native here", "citations": [
        "https://cellxgene.cziscience.com/", "https://data.humancellatlas.org/"]}

def perturb_stub(symbol: str) -> dict:
    return {"note": "BioGRID-ORCS/DepMap UI-first; wire later", "citations": [
        "https://orcs.thebiogrid.org/", "https://depmap.org/portal/"]}

# ============================================
# BUCKET 3 — expression/specificity/localizing
# ============================================
async def expression_atlas_gene(symbol: str) -> dict:
    url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    js = await get_json(url)
    return {"raw": js, "citations": [url]}

async def uniprot_localization(symbol: str) -> dict:
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}

def inducibility_stub(symbol: str, stimulus: Optional[str]) -> dict:
    return {"note": "GEO time-courses need matrix download; omitted here", "citations": ["https://www.ncbi.nlm.nih.gov/geo/"]}

# =================================
# BUCKET 4 — wiring & interactomics
# =================================
async def reactome_search(symbol: str) -> dict:
    url = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}

async def string_map_and_network(symbol: str) -> dict:
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    ids = await get_json(map_url)
    if not ids:
        return {"string_id": None, "edges": [], "citations": [map_url]}
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    net = await get_json(net_url)
    return {"string_id": sid, "edges": net if isinstance(net, list) else [], "citations": [map_url, net_url]}

async def omnipath_ligrec(symbol: str) -> dict:
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    js = await get_json(url)
    return {"rows": js if isinstance(js, list) else [], "citations": [url]}

# ==============================
# BUCKET 5 — tractability stack
# ==============================
async def ot_platform_known_drugs(symbol: str) -> dict:
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    body = await post_json(gql, {
        "query": """
        query Q($sym:String!){
          target(approvedSymbol:$sym){
            id approvedSymbol
            knownDrugs{ rows{ drugType drug{ id name } disease{ id name } phase } count }
          }
        }""",
        "variables": {"sym": symbol}
    })
    t = (body.get("data", {}) or {}).get("target") or {}
    kd = (t.get("knownDrugs") or {})
    return {"knownDrugs": kd.get("rows", []), "count": kd.get("count"), "citations": [gql]}

async def pdb_search(symbol: str) -> dict:
    url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps({
        "query": {"type":"terminal","service":"text","parameters":{"value":symbol}},
        "request_options":{"return_all_hits":True},"return_type":"entry"
    }))
    js = await get_json(url)
    return {"hits": js.get("result_set", []) if isinstance(js, dict) else [], "citations": ["https://www.rcsb.org/"]}

async def uniprot_topology(symbol: str) -> dict:
    q = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={q}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}

def rnacentral_stub(symbol: str) -> dict:
    return {"note": "Oligo design requires RNACentral/TargetScan programmatic mapping later",
            "citations": ["https://rnacentral.org/", "https://www.targetscan.org/"]}

def modality_stub(symbol: str) -> dict:
    return {"note": "Combine UniProt + SURFY/CSPA rules in later iteration", "citations": ["https://rest.uniprot.org/"]}

def iedb_stub(symbol: str) -> dict:
    return {"note": "IEDB API available; wire detailed params later", "citations": ["https://www.iedb.org/"]}

# ===============================
# BUCKET 6 — clinical & safety
# ===============================
async def ctgov_studies_outcomes(condition: str) -> dict:
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&fields=protocolSection.outcomesModule"
    js = await get_json(url)
    return {"studies": js.get("studies", []) if isinstance(js, dict) else [], "citations": [url]}

def rwe_stub(condition: str) -> dict:
    return {"note": "RWE sources (Sentinel/N3C/SEER) are access-controlled", "citations": [
        "https://www.sentinelinitiative.org/", "https://covid.cd2h.org/N3C", "https://seer.cancer.gov/"]}

async def openfda_faers_reactions(drug_name: str) -> dict:
    url = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug_name)}&count=patient.reaction.reactionmeddrapt.exact"
    try:
        js = await get_json(url, tries=2)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}

# ===============================
# BUCKET 7 — competition & IP
# ===============================
async def ot_platform_known_drugs_count(symbol: str) -> dict:
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    body = await post_json(gql, {
        "query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }",
        "variables": {"sym": symbol}
    })
    count = (((body.get("data", {}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
    return {"count": count, "citations": [gql]}

async def ctgov_trial_count(condition: Optional[str]) -> dict:
    if not condition:
        return {"totalStudies": None, "citations": []}
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&countTotal=true"
    js = await get_json(url)
    return {"totalStudies": js.get("totalStudies"), "citations": [url]}

def ip_stub(query: str) -> dict:
    return {"note": "Use The Lens / Espacenet APIs or gateway later", "citations": [
        "https://www.lens.org/", "https://worldwide.espacenet.com/"]}

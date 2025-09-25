"""
Client layer for calling upstream services used by the TargetVal gateway.

This module proxies public REST/GraphQL APIs only — **no synthetic payloads**.
On success, helpers return minimal, normalized dicts that always include a
`citations` list of the exact URLs hit. On transient failures, helpers return
empty results with citations where possible; for endpoints that require
credentials (e.g., BioGRID ORCS), we raise a clear RuntimeError if the required
environment variables are missing.

Key updates in this version:
- Added a robust EFO resolver (id/uri/label) and EFO-aware filtering helpers
  for GWAS and DisGeNET.
- Updated MR wrapper to use the shared EFO resolution and echo the resolved
  outcome in the payload.
- Refined modality scoring: adds UniProt/Proteins topology parsing to estimate
  extracellular domain length and integrates IEDB epitope/T-cell burden.
  Exposes optional per-modality weights.
- Added thin wrappers (COMPARTMENTS, PDBe, GTEx, ProteomicsDB, eQTL sQTLs)
  used by routers and scoring.

Upstream services touched here:
- Open Targets Genetics & Platform (GraphQL)
- GWAS Catalog (REST)
- DisGeNET (REST)
- gnomAD (GraphQL)
- Monarch Initiative (REST)
- EpiGraphDB (REST)
- RNAcentral (REST)
- miRNet 2.0 (REST, POST)
- eQTL Catalogue (REST)
- ENCODE (REST)
- Expression Atlas (REST)
- PRIDE Archive (REST)
- Human Protein Atlas search API (REST)
- BioGRID ORCS (REST) — requires ORCS_ACCESS_KEY
- NCBI GEO E-utilities (REST)
- Reactome (REST)
- STRING (REST)
- OmniPath (REST)
- PDBe & RCSB PDB (REST)
- UniProt / Proteins API (REST)
- IEDB IQ-API (PostgREST)
- openFDA FAERS (REST)
- ClinicalTrials.gov v2 (REST)
- PatentsView (REST)
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional

from app.utils.http import get_json, post_json


# =============================================================================
# EFO normalisation & resolution
# =============================================================================

def _norm_efo_id(raw: str) -> str:
    x = (raw or "").strip()
    if not x:
        return x
    x = x.replace(":", "_").upper()
    return x


async def resolve_efo(efo_or_label: str) -> Dict[str, Any]:
    """
    Resolve an EFO id/label to {efo_id, efo_uri, label, citations}.
    Uses EpiGraphDB /ontology/disease-efo (fuzzy match).
    Always returns a payload (falls back to best-effort normalisation).
    """
    term = (efo_or_label or "").strip()
    url = f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(term)}&fuzzy=true"
    try:
        js = await get_json(url)
        rows = js.get("results", []) if isinstance(js, dict) else []
        if rows:
            top = rows[0]
            eid = top.get("efo_id") or (top.get("disease") or {}).get("id") or _norm_efo_id(term)
            label = top.get("disease_label") or (top.get("disease") or {}).get("label") or term
            uri = top.get("efo_uri") or (top.get("disease") or {}).get("uri") or f"http://www.ebi.ac.uk/efo/{_norm_efo_id(eid)}"
            return {
                "efo_id": _norm_efo_id(eid),
                "efo_uri": uri,
                "label": label,
                "citations": [url],
            }
    except Exception:
        pass
    # Fallback: return something consistent even if resolver failed
    eid = _norm_efo_id(term)
    return {
        "efo_id": eid,
        "efo_uri": f"http://www.ebi.ac.uk/efo/{eid}" if eid.startswith("EFO_") else None,
        "label": term,
        "citations": [url],
    }


def _assoc_matches_efo(assoc: Dict[str, Any], efo_id: str, efo_uri: Optional[str], label: str) -> bool:
    """
    Best-effort association→EFO match across GWAS/DisGeNET shapes.
    Checks URI, short form, id, and label substrings defensively.
    """
    targets = {efo_id.lower(), (efo_uri or "").lower(), label.lower()}

    # GWAS shapes: efoTraits[] elements may contain uri, shortForm, label/trait
    traits: List[Dict[str, Any]] = []
    for k in ("efoTraits", "efoTrait", "_embedded"):
        v = assoc.get(k)
        if isinstance(v, list):
            traits.extend(v)
        elif isinstance(v, dict):
            traits.extend(v.get("efoTraits", []))
    for t in traits:
        vals = [
            str(t.get("uri", "")).lower(),
            str(t.get("efoURI", "")).lower(),
            str(t.get("shortForm", "")).lower().replace(":", "_"),
            str(t.get("efoId", "")).lower().replace(":", "_"),
            str(t.get("trait", "")).lower(),
            str(t.get("label", "")).lower(),
        ]
        if any(val and val in targets for val in vals):
            return True

    # DisGeNET: match on disease name/URI substrings
    maybe = [
        assoc.get("disease_name"),
        assoc.get("diseaseid"),
        assoc.get("disease_name_upper"),
        assoc.get("diseaseid_db"),
    ]
    return any(isinstance(x, str) and label.lower() in x.lower() for x in maybe if x)


# =============================================================================
# Human genetics & causality
# =============================================================================

async def ot_genetics_l2g(ensembl_gene_id: str, efo_id: str) -> Dict[str, Any]:
    """Open Targets Genetics colocalisation for a gene–disease pair (live GraphQL)."""
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
        "variables": {"geneId": ensembl_gene_id, "efoId": efo_id},
    }
    body = await post_json(gql, payload)
    return {
        "coloc": ((body.get("data", {}) or {}).get("colocalisationByGeneAndDisease") or []),
        "citations": [gql],
    }


async def gwas_associations_by_gene(gene: str, size: int = 200) -> Dict[str, Any]:
    url = f"https://www.ebi.ac.uk/gwas/rest/api/associations?geneName={urllib.parse.quote(gene)}&size={size}"
    try:
        js = await get_json(url)
        if isinstance(js, dict):
            hits = js.get("_embedded", {}).get("associations", [])
        elif isinstance(js, list):
            hits = js
        else:
            hits = []
        return {"associations": hits, "citations": [url]}
    except Exception:
        return {"associations": [], "citations": [url]}


async def gwas_gene_efo_filtered(gene: str, efo: str, size: int = 200) -> Dict[str, Any]:
    e = await resolve_efo(efo)
    gw = await gwas_associations_by_gene(gene, size=size)
    filt = [a for a in (gw["associations"] or []) if _assoc_matches_efo(a, e["efo_id"], e["efo_uri"], e["label"])]
    return {"efo": e, "associations": filt, "citations": gw["citations"] + e["citations"]}


async def disgenet_gda_by_gene(gene: str) -> Dict[str, Any]:
    url = f"https://www.disgenet.org/api/gda/gene/{urllib.parse.quote(gene)}?format=json"
    try:
        rows = await get_json(url)
        return {"rows": rows if isinstance(rows, list) else [], "citations": [url]}
    except Exception:
        return {"rows": [], "citations": [url]}


async def disgenet_gene_efo_filtered(gene: str, efo: str) -> Dict[str, Any]:
    e = await resolve_efo(efo)
    dg = await disgenet_gda_by_gene(gene)
    filt = [r for r in (dg["rows"] or []) if _assoc_matches_efo(r, e["efo_id"], e["efo_uri"], e["label"])]
    return {"efo": e, "rows": filt, "citations": dg["citations"] + e["citations"]}


async def gnomad_constraint(symbol: str) -> Dict[str, Any]:
    """gnomAD gene constraint (pLI/LOEUF) via GraphQL (GRCh38)."""
    gql = "https://gnomad.broadinstitute.org/api"
    query = {
        "query": """
            query Rare($symbol:String!){
              gene(gene_symbol:$symbol, reference_genome:GRCh38){
                gene_id gene_symbol
                constraint{ lof_z n_lof expected_lof pLI }
              }
            }""",
        "variables": {"symbol": symbol},
    }
    body = await post_json(gql, query)
    return {"gene": (body.get("data", {}) or {}).get("gene"), "citations": [gql]}


async def monarch_mendelian(symbol_or_entrez: str) -> Dict[str, Any]:
    """Mendelian gene–disease associations from Monarch Initiative (live REST)."""
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol_or_entrez)}/diseases"
    try:
        js = await get_json(url)
        return {"associations": js.get("associations", []), "citations": [url]}
    except Exception:
        return {"associations": [], "citations": [url]}


async def ieu_mr(exposure_id: str, outcome_id: str) -> Dict[str, Any]:
    """
    Multi‑SNP MR via EpiGraphDB (xQTL). exposure_id: gene symbol; outcome_id: EFO id or label.
    Returns: {"mr": [...], "outcome_trait": <resolved label>, "efo": {...}, "citations": [...]}
    """
    e = await resolve_efo(outcome_id)
    mr_url = (
        "https://api.epigraphdb.org/xqtl/multi-snp-mr"
        f"?exposure_gene={urllib.parse.quote(exposure_id)}&outcome_trait={urllib.parse.quote(e['label'])}"
    )
    try:
        mr = await get_json(mr_url)
        rows = mr.get("results", []) if isinstance(mr, dict) else (mr or [])
        return {"mr": rows, "outcome_trait": e["label"], "efo": e, "citations": [mr_url] + e["citations"]}
    except Exception:
        return {"mr": [], "outcome_trait": e["label"], "efo": e, "citations": [mr_url] + e["citations"]}


# =============================================================================
# RNA / expression / functional genomics
# =============================================================================

async def lncrna(symbol: str, limit: int = 50) -> Dict[str, Any]:
    """lncRNA/circRNA sequences from RNAcentral for a gene symbol (live REST)."""
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(symbol)}&page_size={limit}"
    try:
        js = await get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return {"lncRNAs": results[:limit], "citations": [url]}
    except Exception:
        return {"lncRNAs": [], "citations": [url]}


async def mirna(symbol: str, limit: int = 100) -> Dict[str, Any]:
    """
    miRNA–gene interactions via miRNet 2.0 (live POST).
    Returns simplified rows with miRNA, target, evidence and PMID when present.
    """
    url = "https://api.mirnet.ca/table/gene"
    payload = {"org": "hsa", "idOpt": "symbol", "myList": symbol, "selSource": "All"}
    try:
        r = await post_json(url, payload)
        rows = r.get("data", []) if isinstance(r, dict) else []
        out: List[Dict[str, Any]] = []
        for it in rows[:limit]:
            out.append({
                "miRNA": it.get("miRNA") or it.get("mirna") or it.get("ID"),
                "target": it.get("Target") or it.get("Gene") or symbol,
                "evidence": it.get("Category") or it.get("Evidence") or it.get("Source"),
                "pmid": it.get("PMID") or it.get("PubMedID"),
                "source_db": it.get("Source") or "miRNet",
            })
        return {"symbol": symbol, "interactions": out, "citations": [url]}
    except Exception:
        return {"symbol": symbol, "interactions": [], "citations": [url]}


async def eqtl_catalogue_gene(symbol: str) -> Dict[str, Any]:
    """Gene‑level eQTL Catalogue data (live REST)."""
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    try:
        js = await get_json(url)
        return {"results": js if isinstance(js, list) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def eqtl_catalogue_sqtls(gene: str) -> Dict[str, Any]:
    """sQTLs for a gene from eQTL Catalogue (live REST)."""
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(gene)}/sqtls"
    try:
        js = await get_json(url)
        return {"sqtls": js.get("sqtls", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"sqtls": [], "citations": [url]}


async def encode_chipseq(symbol: str) -> Dict[str, Any]:
    """Epigenetics (ChIP‑seq) metadata from ENCODE for a gene (live REST)."""
    query = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{query}&format=json"
    try:
        js = await get_json(url)
        return {"experiments": js.get("@graph", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"experiments": [], "citations": [url]}


async def expression_atlas_experiments(condition: str) -> Dict[str, Any]:
    """Expression Atlas experiments matching a condition term (live REST)."""
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    try:
        js = await get_json(url)
        return {"experiments": js.get("experiments", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"experiments": [], "citations": [url]}


async def pride_projects(condition: str) -> Dict[str, Any]:
    """PRIDE proteomics projects matching a condition keyword (live REST)."""
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    try:
        js = await get_json(url)
        return {"projects": js if isinstance(js, list) else [], "citations": [url]}
    except Exception:
        return {"projects": [], "citations": [url]}


async def gtex_genes_by_tissue(tissue: str) -> Dict[str, Any]:
    """GTEx genes by tissue site (live REST)."""
    url = f"https://gtexportal.org/api/v2/association/genesByTissue?tissueSiteDetail={urllib.parse.quote(tissue)}"
    try:
        js = await get_json(url)
        return {"genes": js.get("genes", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"genes": [], "citations": [url]}


async def proteomicsdb_search_proteins(keyword: str) -> Dict[str, Any]:
    """ProteomicsDB proteins search (live REST)."""
    url = f"https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search?search={urllib.parse.quote(keyword)}"
    try:
        js = await get_json(url)
        if isinstance(js, dict):
            recs = js.get("items", js.get("proteins", js.get("results", [])))
        elif isinstance(js, list):
            recs = js
        else:
            recs = []
        return {"records": recs, "citations": [url]}
    except Exception:
        return {"records": [], "citations": [url]}


async def cellxgene(condition: str, limit: int = 100) -> Dict[str, Any]:
    """
    Practical single‑cell proxy via Human Protein Atlas search API:
    surfaces records that include single‑cell fields for a condition keyword.
    """
    hpa_url = (
        "https://www.proteinatlas.org/api/search_download.php"
        f"?format=json&columns=ensembl,gene,cell_type,rna_cell_type,rna_tissue,rna_gtex&search={urllib.parse.quote(condition)}"
    )
    try:
        js = await get_json(hpa_url)
        rows = js if isinstance(js, list) else []
        out: List[Dict[str, Any]] = []
        for r in rows:
            if any(k in r and r[k] for k in ("cell_type", "rna_cell_type")):
                out.append({
                    "gene": r.get("gene"),
                    "cell_type": r.get("cell_type") or r.get("rna_cell_type"),
                    "rna_gtex": r.get("rna_gtex"),
                    "tissue": r.get("rna_tissue"),
                })
        cell_types = sorted({str(x.get("cell_type")) for x in out if x.get("cell_type")})
        return {"condition": condition, "cell_types": cell_types[:limit], "records": out[:limit], "citations": [hpa_url]}
    except Exception:
        return {"condition": condition, "cell_types": [], "records": [], "citations": [hpa_url]}


async def perturb(condition: str, limit: int = 100) -> Dict[str, Any]:
    """
    CRISPR perturbation screens from BioGRID ORCS (live REST).
    Requires env var ORCS_ACCESS_KEY. Raises RuntimeError if missing.
    """
    access_key = os.getenv("ORCS_ACCESS_KEY")
    if not access_key:
        raise RuntimeError("Missing ORCS_ACCESS_KEY for BioGRID ORCS access.")

    base = "https://orcsws.thebiogrid.org/screens/"
    list_url = (
        f"{base}?accesskey={urllib.parse.quote(access_key)}&format=json&organismID=9606"
        f"&conditionName={urllib.parse.quote(condition)}&start=0&max=50"
    )
    try:
        screens = await get_json(list_url)
        screens_list = screens if isinstance(screens, list) else []
        screens_list = screens_list[: min(3, len(screens_list))]

        hits_collected: List[Dict[str, Any]] = []
        for sc in screens_list:
            sc_id = sc.get("SCREEN_ID") or sc.get("ID") or sc.get("SCREENID")
            if not sc_id:
                continue
            res_url = (
                f"https://orcsws.thebiogrid.org/results/"
                f"?accesskey={urllib.parse.quote(access_key)}&format=json&screenID={urllib.parse.quote(str(sc_id))}&start=0&max=500"
            )
            try:
                hits = await get_json(res_url)
                for h in hits if isinstance(hits, list) else []:
                    if h.get("HIT") in (True, "true", "YES", "Yes", "yes"):
                        hits_collected.append({
                            "screen_id": sc_id,
                            "gene": h.get("OFFICIAL_SYMBOL"),
                            "effect": h.get("PHENOTYPE") or "hit",
                        })
            except Exception:
                pass

        return {"condition": condition, "screens_n": len(screens_list), "top_hits": hits_collected[:limit], "citations": [list_url]}
    except Exception:
        return {"condition": condition, "screens_n": 0, "top_hits": [], "citations": [list_url]}


async def expression_atlas_gene(symbol: str) -> Dict[str, Any]:
    """Baseline gene expression from Expression Atlas (live REST)."""
    url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    try:
        js = await get_json(url)
        return {"raw": js, "citations": [url]}
    except Exception:
        return {"raw": {}, "citations": [url]}


async def uniprot_localization(symbol: str) -> Dict[str, Any]:
    """Subcellular localisation from UniProt (live REST)."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def inducibility(symbol: str, limit: int = 50) -> Dict[str, Any]:
    """GEO dataset IDs mentioning the gene via E-utilities (live REST)."""
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=gds&term={urllib.parse.quote(symbol)}%5Bgene%5D&retmode=json"
    )
    try:
        js = await get_json(url)
        ids = (js.get("esearchresult", {}) or {}).get("idlist", [])
        return {"symbol": symbol, "datasets": ids[:limit], "citations": [url]}
    except Exception:
        return {"symbol": symbol, "datasets": [], "citations": [url]}


# =============================================================================
# Mechanistic wiring & networks
# =============================================================================

async def reactome_search(symbol: str) -> Dict[str, Any]:
    """Reactome pathway search hits for a gene (live REST)."""
    url = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def string_map_and_network(symbol: str) -> Dict[str, Any]:
    """STRING mapping + network edges for a gene (live REST)."""
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    try:
        ids = await get_json(map_url)
    except Exception:
        ids = []
    if not ids:
        return {"string_id": None, "edges": [], "citations": [map_url]}
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    try:
        net = await get_json(net_url)
        return {"string_id": sid, "edges": net if isinstance(net, list) else [], "citations": [map_url, net_url]}
    except Exception:
        return {"string_id": sid, "edges": [], "citations": [map_url, net_url]}


async def omnipath_ligrec(symbol: str) -> Dict[str, Any]:
    """Ligand–receptor interactions from OmniPath (live REST)."""
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    try:
        js = await get_json(url)
        return {"rows": js if isinstance(js, list) else [], "citations": [url]}
    except Exception:
        return {"rows": [], "citations": [url]}


# =============================================================================
# Tractability & modality
# =============================================================================

async def ot_platform_known_drugs(symbol: str) -> Dict[str, Any]:
    """Open Targets Platform known drug rows for a gene (live GraphQL)."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
            query Q($sym:String!){
              target(approvedSymbol:$sym){
                id approvedSymbol
                knownDrugs{ rows{ drugType drug{ id name } disease{ id name } phase } count }
              }
            }""",
        "variables": {"sym": symbol},
    }
    body = await post_json(gql, query)
    t = (body.get("data", {}) or {}).get("target") or {}
    kd = (t.get("knownDrugs") or {})
    return {"knownDrugs": kd.get("rows", []), "count": kd.get("count"), "citations": [gql]}


async def ot_platform_search_drugs(symbol: str) -> Dict[str, Any]:
    """Open Targets Platform search() fallback for drugs by target (live GraphQL)."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
            query Q($q:String!){
              search(queryString:$q){ drugs{ id name } }
            }""",
        "variables": {"q": f"target:{symbol}"},
    }
    try:
        body = await post_json(gql, query)
        drugs = (body.get("data", {}) or {}).get("search", {}).get("drugs", []) or []
        rows = [{"drugId": d.get("id"), "drugName": d.get("name")} for d in drugs]
        return {"rows": rows, "citations": [gql]}
    except Exception:
        return {"rows": [], "citations": [gql]}


async def pdb_search(symbol: str) -> Dict[str, Any]:
    """RCSB PDB text search for entries matching a symbol (live REST)."""
    q = {
        "query": {"type": "terminal", "service": "text", "parameters": {"value": symbol}},
        "request_options": {"return_all_hits": True},
        "return_type": "entry",
    }
    url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps(q))
    try:
        js = await get_json(url)
        return {"hits": js.get("result_set", []) if isinstance(js, dict) else [], "citations": ["https://www.rcsb.org/"]}
    except Exception:
        return {"hits": [], "citations": ["https://www.rcsb.org/"]}


async def uniprot_topology(symbol: str) -> Dict[str, Any]:
    """Protein topology info from UniProt (REST)."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def proteins_api_features(symbol: str) -> Dict[str, Any]:
    """
    UniProt Proteins API features for a human gene symbol.
    Used to estimate extracellular domain length.
    """
    url = f"https://www.ebi.ac.uk/proteins/api/proteins?offset=0&size=1&gene={urllib.parse.quote(symbol)}&organism=9606"
    try:
        js = await get_json(url)
        entry = js[0] if isinstance(js, list) and js else {}
        feats = entry.get("features", []) if isinstance(entry, dict) else []
        length = entry.get("sequence", {}).get("length")
        return {"features": feats, "length": length, "citations": [url]}
    except Exception:
        return {"features": [], "length": None, "citations": [url]}


def _sum_extracellular_len(features: List[Dict[str, Any]]) -> int:
    total = 0
    for f in features or []:
        if f.get("type") == "TOPOLOGICAL DOMAIN" and str(f.get("description", "")).lower().startswith("extracellular"):
            try:
                b = int((f.get("begin") or {}).get("position") or f.get("begin"))
                e = int((f.get("end") or {}).get("position") or f.get("end"))
                if b and e and e >= b:
                    total += (e - b + 1)
            except Exception:
                continue
    return total


async def rnacentral_oligo(symbol: str, limit: int = 100) -> Dict[str, Any]:
    """
    Legacy name retained. Proxies Ribocentre Aptamer API to surface nucleic‑acid
    binders related to the symbol (live REST).
    """
    api_url = f"https://aptamer.ribocentre.org/api/?search={urllib.parse.quote(symbol)}"
    try:
        js = await get_json(api_url)
        items = js.get("results") or js.get("items") or js.get("data") or js.get("entries") or []
        out: List[Dict[str, Any]] = []
        for it in items:
            out.append({
                "ligand": it.get("Ligand") or it.get("Target") or it.get("ligand"),
                "type": it.get("Type") or it.get("type"),
                "source": it.get("Source") or it.get("source"),
                "id": it.get("ID") or it.get("id"),
            })
        return {"symbol": symbol, "oligos": out[:limit], "citations": [api_url]}
    except Exception:
        return {"symbol": symbol, "oligos": [], "citations": [api_url]}


async def compartments_locations(symbol: str) -> Dict[str, Any]:
    url = f"https://compartments.jensenlab.org/Service?gene_names={urllib.parse.quote(symbol)}&format=json"
    try:
        js = await get_json(url)
        return {"locations": js.get(symbol, []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"locations": [], "citations": [url]}


async def pdbe_proteins_for_symbol(symbol: str) -> Dict[str, Any]:
    url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(symbol)}"
    try:
        js = await get_json(url)
        hits: List[Any] = []
        if isinstance(js, dict):
            for _, vals in js.items():
                if isinstance(vals, list):
                    hits.extend(vals)
        return {"entries": hits, "citations": [url]}
    except Exception:
        return {"entries": [], "citations": [url]}


async def iedb_immunogenicity(symbol: str, limit: int = 50) -> Dict[str, Any]:
    """
    Immunogenicity via IEDB IQ‑API (PostgREST) — live call.
    Pulls epitope_search and tcell_search filtered by antigen name containing SYMBOL.
    """
    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    tc_url = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    try:
        epi = await get_json(epi_url)
        tc = await get_json(tc_url)
        epi_list = epi if isinstance(epi, list) else []
        tc_list = tc if isinstance(tc, list) else []
        hla_counts: Dict[str, int] = {}
        for r in tc_list:
            allele = r.get("mhc_allele_name") or r.get("assay_mhc_allele_name") or r.get("mhc_name")
            if allele:
                hla_counts[allele] = hla_counts.get(allele, 0) + 1
        return {
            "symbol": symbol,
            "epitopes_n": len(epi_list),
            "tcell_assays_n": len(tc_list),
            "hla_breakdown": sorted([[k, v] for k, v in hla_counts.items()], key=lambda kv: kv[1], reverse=True)[:25],
            "examples": {"epitopes": epi_list[: min(10, limit)], "tcell_assays": tc_list[: min(10, limit)]},
            "citations": [epi_url, tc_url],
        }
    except Exception:
        return {
            "symbol": symbol,
            "epitopes_n": 0,
            "tcell_assays_n": 0,
            "hla_breakdown": [],
            "examples": {"epitopes": [], "tcell_assays": []},
            "citations": [epi_url, tc_url],
        }


async def modality(symbol: str, w_sm: float = 1.0, w_ab: float = 1.0, w_oligo: float = 1.0) -> Dict[str, Any]:
    """
    Refined heuristic modality assessment integrating:
      - COMPARTMENTS: localization flags (membrane/secreted/cytosol/nucleus)
      - ChEMBL: presence of targets (small-molecule precedent)
      - PDBe: structure availability
      - UniProt/Proteins API: extracellular domain length
      - IEDB IQ-API: epitope/T-cell assay burden (immunogenicity penalty)
    Optional per-modality weights (w_sm, w_ab, w_oligo) allow user tuning.
    """
    comp = await compartments_locations(symbol)

    chem_url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    try:
        chem = await get_json(chem_url)
        chem_targets = chem.get("targets", []) if isinstance(chem, dict) else []
    except Exception:
        chem_targets = []

    pdbe = await pdbe_proteins_for_symbol(symbol)
    prot = await proteins_api_features(symbol)
    ecd_len = _sum_extracellular_len(prot["features"])

    imm = await iedb_immunogenicity(symbol, limit=50)
    epi_burden = (imm.get("epitopes_n") or 0) + (imm.get("tcell_assays_n") or 0)

    loc_terms = " ".join([str(x) for x in (comp["locations"] or [])]).lower()
    is_extracellular = any(t in loc_terms for t in ["secreted", "extracellular", "extracellular space"])
    is_membrane = any(t in loc_terms for t in ["plasma membrane", "cell membrane", "membrane"])
    has_chembl = len(chem_targets) > 0
    has_structure = len(pdbe["entries"] or []) > 0

    # Heuristic contributions
    ab_bonus_ecd = min(0.2, (ecd_len or 0) / 600.0)       # +0..+0.2 up to ~600 aa ECD
    ab_penalty_immun = min(0.15, epi_burden / 1000.0)     # -0..-0.15 with heavy burden
    sm_bonus_pocket = 0.25 if has_structure else 0.0
    sm_penalty_secreted = 0.15 if (is_extracellular and not is_membrane) else 0.0
    oligo_bonus_access = 0.25 if ("nucleus" in loc_terms or "cytosol" in loc_terms) else 0.0

    sm_score = (0.6 if has_chembl else 0.0) + sm_bonus_pocket - sm_penalty_secreted
    ab_score = (0.6 if (is_membrane or is_extracellular) else 0.0) + (0.25 if has_structure else 0.0) + ab_bonus_ecd - ab_penalty_immun
    oligo_score = (0.5 if (is_extracellular or "nucleus" in loc_terms or "cytosol" in loc_terms) else 0.0) + (0.1 if has_structure else 0.0) + (0.05 if has_chembl else 0.0) + oligo_bonus_access

    def clamp(x: float) -> float:
        return max(0.0, min(1.0, round(x, 3)))

    rec = [
        ("small_molecule", clamp(sm_score * w_sm)),
        ("antibody", clamp(ab_score * w_ab)),
        ("oligo", clamp(oligo_score * w_oligo)),
    ]
    rec.sort(key=lambda kv: kv[1], reverse=True)

    return {
        "symbol": symbol,
        "recommendation": rec,
        "rationale": {
            "is_extracellular": is_extracellular,
            "is_membrane": is_membrane,
            "chembl_targets_n": len(chem_targets),
            "pdbe_structures_n": len(pdbe["entries"] or []),
            "ecd_len": ecd_len,
            "iedb_burden": epi_burden,
        },
        "snippets": {
            "compartments": (comp["locations"] or [])[:25],
            "chembl_targets": (chem_targets or [])[:10],
            "pdbe_entries": (pdbe["entries"] or [])[:10],
            "uniprot_features_sample": (prot["features"] or [])[:10],
        },
        "citations": [*comp["citations"], chem_url, *pdbe["citations"], *prot["citations"]],
    }


# =============================================================================
# Clinical translation & safety
# =============================================================================

async def openfda_faers_reactions(drug_name: str) -> Dict[str, Any]:
    """FAERS reaction term counts for a drug via openFDA (live REST)."""
    url = (
        f"https://api.fda.gov/drug/event.json?"
        f"search=patient.drug.medicinalproduct:{urllib.parse.quote(drug_name)}&count=patient.reaction.reactionmeddrapt.exact"
    )
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def ot_platform_known_drugs_count(symbol: str) -> Dict[str, Any]:
    """Known drugs count from Open Targets Platform (live GraphQL)."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }",
        "variables": {"sym": symbol},
    }
    body = await post_json(gql, query)
    count = (((body.get("data", {}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
    return {"count": count, "citations": [gql]}


async def ctgov_trial_count(condition: Optional[str]) -> Dict[str, Any]:
    """Total number of trials for a condition from ClinicalTrials.gov v2 (live REST)."""
    if not condition:
        return {"totalStudies": None, "citations": []}
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&countTotal=true"
    try:
        js = await get_json(url)
        return {"totalStudies": js.get("totalStudies"), "citations": [url]}
    except Exception:
        return {"totalStudies": None, "citations": [url]}


# =============================================================================
# Competition & IP
# =============================================================================

async def ip(query: str, limit: int = 100) -> Dict[str, Any]:
    """Patent search via PatentsView (live REST). Returns simple identifiers+titles."""
    pv_query = {"_or": [{"patent_title": {"_text_any": query}}, {"patent_abstract": {"_text_any": query}}]}
    q_str = urllib.parse.quote(json.dumps(pv_query))
    fields = urllib.parse.quote(json.dumps(["patent_id", "patent_title", "patent_date"]))
    url = f"https://api.patentsview.org/patents/query?q={q_str}&f={fields}"
    try:
        js = await get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        out = [{"patent_id": p.get("patent_id"), "title": p.get("patent_title"), "date": p.get("patent_date")} for p in patents]
        return {"query": query, "patents": out[:limit], "citations": [url]}
    except Exception:
        return {"query": query, "patents": [], "citations": [url]}

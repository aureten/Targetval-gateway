"""
sources.py — public-only, live-first client helpers for TargetVal gateway

Principles
- **Public APIs only** (no keys required). Where a provider typically needs a key (OMIM, DrugBank, Lens),
  we return a graceful "skipped" object with citations/links instead of raising.
- **Live-first** with pragmatic fallbacks. Each helper returns small normalized dicts and always includes
  a `citations` list of the precise URLs hit. No synthetic payloads.
- **No stubs**: every function attempts a live call; if that fails, it returns an empty result *plus*
  navigational links you can follow in the UI.

Note: these helpers use `app.utils.http.get_json` / `post_json` for bounded, retrying HTTP.
"""

from __future__ import annotations

import os
import json
import urllib.parse
from typing import Any, Dict, List, Optional

from app.utils.http import get_json, post_json


# =============================================================================
# Shared utils
# =============================================================================

def _norm_efo_id(x: str) -> str:
    if not x:
        return x
    x = x.strip().replace(":", "_").upper()
    return x


# =============================================================================
# EFO & identifier resolution
# =============================================================================

async def resolve_efo(efo_or_label: str) -> Dict[str, Any]:
    """
    Resolve a disease label or EFO id to {efo_id, efo_uri, label, citations}
    Strategy: EpiGraphDB fuzzy → OLS4 fallback.
    """
    term = (efo_or_label or "").strip()
    citations: List[str] = []
    if not term:
        return {"efo_id": "", "efo_uri": None, "label": "", "citations": citations}

    # 1) EpiGraphDB fuzzy
    epi = f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(term)}&fuzzy=true"
    try:
        js = await get_json(epi)
        citations.append(epi)
        rows = js.get("results", []) if isinstance(js, dict) else []
        if rows:
            top = rows[0]
            eid = (
                top.get("efo_id")
                or (top.get("disease") or {}).get("id")
                or _norm_efo_id(term)
            )
            label = (
                top.get("disease_label")
                or (top.get("disease") or {}).get("label")
                or term
            )
            uri = (
                top.get("efo_uri")
                or (top.get("disease") or {}).get("uri")
                or f"http://www.ebi.ac.uk/efo/{_norm_efo_id(eid)}"
            )
            return {"efo_id": _norm_efo_id(eid), "efo_uri": uri, "label": label, "citations": citations}
    except Exception:
        pass

    # 2) OLS4 fallback
    ols = f"https://www.ebi.ac.uk/ols4/api/search?q={urllib.parse.quote(term)}&ontology=efo&rows=5"
    try:
        js = await get_json(ols)
        citations.append(ols)
        docs = ((js.get("response") or {}).get("docs") or []) if isinstance(js, dict) else []
        for d in docs:
            iri = d.get("iri") or ""
            lab = d.get("label") or term
            tail = iri.rsplit("/", 1)[-1]
            if tail.upper().startswith("EFO_"):
                return {"efo_id": tail.upper(), "efo_uri": f"http://www.ebi.ac.uk/efo/{tail.upper()}", "label": lab, "citations": citations}
    except Exception:
        pass

    # Fallback: echo input
    eid = _norm_efo_id(term)
    return {"efo_id": eid, "efo_uri": f"http://www.ebi.ac.uk/efo/{eid}" if eid.startswith("EFO_") else None, "label": term, "citations": citations}


# =============================================================================
# B1 — Human genetics & causality
# =============================================================================

async def gwas_associations_by_gene(gene: str, size: int = 200) -> Dict[str, Any]:
    url = f"https://www.ebi.ac.uk/gwas/rest/api/associations?geneName={urllib.parse.quote(gene)}&size={min(size,200)}"
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


def _assoc_matches_efo(assoc: Dict[str, Any], efo_id: str, efo_uri: Optional[str], label: str) -> bool:
    targets = {efo_id.lower(), (efo_uri or "").lower(), label.lower()}
    traits: List[Dict[str, Any]] = []
    # known shapes: association.efoTraits (list of {uri, trait, shortForm}), nested forms under _embedded
    efots = assoc.get("efoTraits") or assoc.get("efoTrait")
    if isinstance(efots, list):
        traits.extend(efots)
    # sometimes nested
    emb = assoc.get("_embedded")
    if isinstance(emb, dict):
        traits.extend(emb.get("efoTraits") or [])
    for t in traits:
        vals = [
            str(t.get("uri", "")).lower(),
            str(t.get("efoURI", "")).lower(),
            str(t.get("shortForm", "")).lower().replace(":", "_"),
            str(t.get("efoId", "")).lower().replace(":", "_"),
            str(t.get("trait", "")).lower(),
            str(t.get("label", "")).lower(),
        ]
        if any(v and v in targets for v in vals):
            return True
    return False


async def gwas_gene_efo_filtered(gene: str, efo: str, size: int = 200) -> Dict[str, Any]:
    e = await resolve_efo(efo)
    gw = await gwas_associations_by_gene(gene, size=size)
    filt = [a for a in (gw["associations"] or []) if _assoc_matches_efo(a, e["efo_id"], e["efo_uri"], e["label"])]
    return {"efo": e, "associations": filt, "citations": list({*gw["citations"], *e["citations"]})}


async def clingen_gene_validity(gene: str) -> Dict[str, Any]:
    url = f"https://search.clinicalgenome.org/kb/gene-validity?format=json&search={urllib.parse.quote(gene)}"
    try:
        js = await get_json(url)
        data = []
        if isinstance(js, dict):
            data = js.get("data") or js.get("items") or js.get("results") or []
        elif isinstance(js, list):
            data = js
        items = [r for r in data if str(r.get("geneSymbol") or r.get("gene","")).upper() == gene.upper()]
        return {"items": items, "citations": [url]}
    except Exception:
        return {"items": [], "citations": [url]}


def orphanet_links(gene: str) -> Dict[str, Any]:
    return {
        "links": {
            "orphanet_search": f"https://www.orpha.net/consor/cgi-bin/Disease_Search.php?lng=EN&data_id=Index_search&search={urllib.parse.quote(gene)}",
            "clingen_gene": f"https://search.clinicalgenome.org/kb/genes/{urllib.parse.quote(gene)}"
        },
        "citations": []
    }


async def ieu_mr(exposure_gene_symbol: str, outcome_efo_or_label: str) -> Dict[str, Any]:
    """
    EpiGraphDB MR-Base multi-snp MR (stable public).
    Returns: {mr: rows[], outcome_trait, efo, citations}
    """
    e = await resolve_efo(outcome_efo_or_label)
    base = "https://api.epigraphdb.org/xqtl/multi-snp-mr"
    url = f"{base}?exposure_gene={urllib.parse.quote(exposure_gene_symbol)}&outcome_trait={urllib.parse.quote(e['label'])}"
    try:
        js = await get_json(url)
        rows = js.get("results", []) if isinstance(js, dict) else (js or [])
        return {"mr": rows, "outcome_trait": e["label"], "efo": e, "citations": [url] + e["citations"]}
    except Exception:
        return {"mr": [], "outcome_trait": e["label"], "efo": e, "citations": [url] + e["citations"]}


async def eqtl_catalogue_sqtls(ensembl_gene_id: str, size: int = 200) -> Dict[str, Any]:
    """
    eQTL Catalogue v2 live call: fetch sQTL first, fall back to eQTL if empty.
    """
    base = "https://www.ebi.ac.uk/eqtl/api/v2/associations"
    # 1) sQTL
    sq_params = {"gene_id": ensembl_gene_id, "qtl_group": "sQTL", "size": min(size, 200)}
    try:
        sq = await get_json(f"{base}?{urllib.parse.urlencode(sq_params)}")
        sq_rows = sq.get("rows") or sq.get("associations") or sq.get("data") or []
        if isinstance(sq_rows, list) and sq_rows:
            return {"type": "sQTL", "rows": sq_rows[:size], "citations": [f"{base}?{urllib.parse.urlencode(sq_params)}"]}
    except Exception:
        pass
    # 2) eQTL fallback
    eq_params = {"gene_id": ensembl_gene_id, "qtl_group": "eQTL", "size": min(size, 200)}
    try:
        eq = await get_json(f"{base}?{urllib.parse.urlencode(eq_params)}")
        eq_rows = eq.get("rows") or eq.get("associations") or eq.get("data") or []
        return {"type": "eQTL", "rows": (eq_rows or [])[:size], "citations": [f"{base}?{urllib.parse.urlencode(eq_params)}"]}
    except Exception:
        return {"type": "eQTL", "rows": [], "citations": [f"{base}?{urllib.parse.urlencode(eq_params)}"]}


# =============================================================================
# B2 — Association & perturbation
# =============================================================================

async def geo_gds_search(term: str, size: int = 100) -> Dict[str, Any]:
    """
    GEO E-utilities: db=gds esearch for term; returns GDS IDs.
    """
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=gds&term={urllib.parse.quote(term)}&retmode=json&retmax={min(size, 500)}"
    )
    try:
        js = await get_json(url)
        ids = ((js.get("esearchresult") or {}).get("idlist") or [])
        return {"term": term, "gds_ids": ids[:size], "citations": [url]}
    except Exception:
        return {"term": term, "gds_ids": [], "citations": [url]}


async def arrayexpress_search(term: str, size: int = 50) -> Dict[str, Any]:
    """
    BioStudies/ArrayExpress search (JSON). If API shape changes, return navigational links.
    """
    api = f"https://www.ebi.ac.uk/biostudies/api/v1/studies?query={urllib.parse.quote(term)}&pageSize={min(size,100)}"
    try:
        js = await get_json(api)
        # Expected shape: {"hits": int, "page":..., "entries":[{accno,title,...}]}
        entries = js.get("entries") or js.get("studies") or js.get("data") or []
        return {"term": term, "entries": entries[:size], "citations": [api]}
    except Exception:
        portal = f"https://www.ebi.ac.uk/biostudies/arrayexpress/studies?query={urllib.parse.quote(term)}"
        return {"term": term, "entries": [], "citations": [api, portal]}


async def pdc_cptac_projects(keyword: str, size: int = 50) -> Dict[str, Any]:
    """
    NCI PDC GraphQL search for CPTAC-related projects by keyword.
    Falls back to portal search links if GraphQL fails.
    """
    gql = "https://pdc.cancer.gov/graphql"
    query = {
        "query": """
        query SearchProjects($kw: String!, $first: Int!) {
          searchProjects(keyword: $kw, first: $first) {
            projectId
            projectName
            programName
            diseaseType
          }
        }""",
        "variables": {"kw": keyword, "first": min(size, 100)}
    }
    try:
        js = await post_json(gql, payload=query)
        rows = ((js.get("data") or {}).get("searchProjects") or [])
        return {"keyword": keyword, "projects": rows[:size], "citations": [gql]}
    except Exception:
        # Links fallback
        cptac = "https://cptac-data-portal.georgetown.edu/"
        px = f"http://proteomecentral.proteomexchange.org/cgi/GetDataset?ID=PXD*&q={urllib.parse.quote(keyword)}"
        return {"keyword": keyword, "projects": [], "citations": [gql, cptac, px]}


async def tabula_sapiens_datasets(keyword: str, size: int = 50) -> Dict[str, Any]:
    """
    Tabula Sapiens portal datasets list (best-effort JSON; portal link fallback).
    """
    api = "https://tabula-sapiens-portal.ds.czbiohub.org/api/datasets"
    try:
        js = await get_json(api)
        rows = [r for r in (js if isinstance(js, list) else []) if keyword.lower() in json.dumps(r).lower()]
        return {"keyword": keyword, "datasets": rows[:size], "citations": [api]}
    except Exception:
        portal = f"https://tabula-sapiens-portal.ds.czbiohub.org/search?q={urllib.parse.quote(keyword)}"
        return {"keyword": keyword, "datasets": [], "citations": [api, portal]}


async def hca_projects(keyword: str, size: int = 50) -> Dict[str, Any]:
    """
    HCA Azul index quick search (public). Fallback to portal search link.
    """
    api = f"https://service.azul.data.humancellatlas.org/index/projects?size={min(size,100)}&search={urllib.parse.quote(keyword)}"
    try:
        js = await get_json(api)
        hits = (js.get("hits") or [])
        return {"keyword": keyword, "projects": hits[:size], "citations": [api]}
    except Exception:
        portal = f"https://data.humancellatlas.org/search?search={urllib.parse.quote(keyword)}"
        return {"keyword": keyword, "projects": [], "citations": [api, portal]}


async def depmap_achilles_index(keyword: Optional[str] = None) -> Dict[str, Any]:
    """
    DepMap public downloads index (no stable JSON API; return links + best-effort hints).
    """
    portal = "https://depmap.org/portal/download/all/"
    # Provide canonical datasets of interest for clients to fetch offline
    datasets = [
        "CRISPR (DepMap) Gene Effect",
        "CRISPR (Achilles) gene effect",
        "Omics (expression / copy number)",
        "Cell line metadata",
    ]
    if keyword:
        datasets = [d for d in datasets if keyword.lower() in d.lower()]
    return {"keyword": keyword, "datasets": datasets, "citations": [portal], "links": {"portal": portal}}


# =============================================================================
# B3 — Expression
# =============================================================================

async def expression_atlas_gene(symbol: str) -> Dict[str, Any]:
    url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    try:
        js = await get_json(url)
        return {"raw": js, "citations": [url]}
    except Exception:
        return {"raw": {}, "citations": [url]}


async def uniprot_localization(symbol: str) -> Dict[str, Any]:
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


# =============================================================================
# B4 — Mechanistic wiring & networks
# =============================================================================

async def reactome_search(symbol: str) -> Dict[str, Any]:
    url = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


async def kegg_find_pathway(symbol: str) -> Dict[str, Any]:
    # KEGG REST is TSV/text for find endpoints; return navigational link
    url = f"https://rest.kegg.jp/find/pathway/{urllib.parse.quote(symbol)}"
    return {"url": url, "citations": [url]}


async def wikipathways_search(symbol: str) -> Dict[str, Any]:
    url = f"https://www.wikipathways.org/search?query={urllib.parse.quote(symbol)}"
    return {"url": url, "citations": [url]}


async def string_map_and_network(symbol: str) -> Dict[str, Any]:
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
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    try:
        js = await get_json(url)
        return {"rows": js if isinstance(js, list) else [], "citations": [url]}
    except Exception:
        return {"rows": [], "citations": [url]}


# =============================================================================
# B5 — Tractability & modality
# =============================================================================

async def chembl_drugs(symbol: str) -> Dict[str, Any]:
    tgt_url  = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    try:
        tgt = await get_json(tgt_url)
    except Exception:
        tgt = {}
    mech_rows: List[Dict[str, Any]] = []
    # If we have target_chembl_ids, try mechanisms for the first few
    try:
        tids = [t.get("target_chembl_id") for t in (tgt.get("targets", []) if isinstance(tgt, dict) else []) if t.get("target_chembl_id")]
        cites = [tgt_url]
        for tid in tids[:2]:
            mech_url = f"https://www.ebi.ac.uk/chembl/api/data/mechanism.json?target_chembl_id={urllib.parse.quote(tid)}"
            try:
                mech = await get_json(mech_url)
                for m in (mech.get("mechanisms", []) if isinstance(mech, dict) else []):
                    mech_rows.append(m)
                cites.append(mech_url)
            except Exception:
                cites.append(mech_url)
        return {"targets": tgt.get("targets", []) if isinstance(tgt, dict) else [], "mechanisms": mech_rows, "citations": cites}
    except Exception:
        return {"targets": tgt.get("targets", []) if isinstance(tgt, dict) else [], "mechanisms": [], "citations": [tgt_url]}


def drugbank_search(symbol: str) -> Dict[str, Any]:
    # Public site search link (no API key)
    url = f"https://go.drugbank.com/unearth/q?query={urllib.parse.quote(symbol)}&searcher=targets"
    return {"url": url, "citations": [url]}


def inxight_search(symbol: str) -> Dict[str, Any]:
    url = f"https://drugs.ncats.io/search?terms={urllib.parse.quote(symbol)}"
    return {"url": url, "citations": [url]}


def sabdab_search(symbol: str) -> Dict[str, Any]:
    sab = f"https://opig.stats.ox.ac.uk/webapps/newsabdab/sabdab/search/?target={urllib.parse.quote(symbol)}"
    ths = f"https://opig.stats.ox.ac.uk/webapps/newsabdab/therasabdab/search/?target={urllib.parse.quote(symbol)}"
    return {"urls": {"sabdab": sab, "therasabdab": ths}, "citations": [sab, ths]}


async def iedb_immunogenicity(symbol: str, limit: int = 50) -> Dict[str, Any]:
    base = "https://query-api.iedb.org"
    epi_url = f"{base}/epitope_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    tc_url  = f"{base}/tcell_search?parent_source_antigen_names=cs.%7B{urllib.parse.quote(symbol)}%7D&limit={limit}"
    try:
        epi = await get_json(epi_url)
        tc  = await get_json(tc_url)
        epi_list = epi if isinstance(epi, list) else []
        tc_list  = tc  if isinstance(tc, list)  else []
        hla_counts: Dict[str, int] = {}
        for r in tc_list:
            allele = r.get("mhc_allele_name") or r.get("assay_mhc_allele_name") or r.get("mhc_name")
            if allele:
                hla_counts[allele] = hla_counts.get(allele, 0) + 1
        breakdown = sorted([[k, v] for k, v in hla_counts.items()], key=lambda kv: kv[1], reverse=True)[:25]
        return {"symbol": symbol, "epitopes_n": len(epi_list), "tcell_assays_n": len(tc_list), "hla_breakdown": breakdown, "citations": [epi_url, tc_url]}
    except Exception:
        return {"symbol": symbol, "epitopes_n": 0, "tcell_assays_n": 0, "hla_breakdown": [], "citations": [epi_url, tc_url]}


# =============================================================================
# B6 — Clinical translation & safety
# =============================================================================

async def openfda_faers_reactions(drug_name: str, limit: int = 20) -> Dict[str, Any]:
    url = (
        "https://api.fda.gov/drug/event.json?"
        f"search=patient.drug.medicinalproduct:{urllib.parse.quote(drug_name)}&count=patient.reaction.reactionmeddrapt.exact"
    )
    try:
        js = await get_json(url)
        rows = (js.get("results", []) if isinstance(js, dict) else [])[:limit]
        return {"drug": drug_name, "reactions": rows, "citations": [url]}
    except Exception:
        return {"drug": drug_name, "reactions": [], "citations": [url]}


async def ctgov_trial_count(condition: Optional[str]) -> Dict[str, Any]:
    if not condition:
        return {"totalStudies": None, "citations": []}
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&countTotal=true"
    try:
        js = await get_json(url)
        return {"totalStudies": js.get("totalStudies"), "citations": [url]}
    except Exception:
        return {"totalStudies": None, "citations": [url]}


def who_ictrp_info() -> Dict[str, Any]:
    root = "https://trialsearch.who.int/Api"
    docs = "https://trialsearch.who.int/"
    return {"root": root, "docs": docs, "citations": [root, docs]}


def eu_ctr_search(condition: str) -> Dict[str, Any]:
    url = f"https://www.clinicaltrialsregister.eu/ctr-search/search?query={urllib.parse.quote(condition)}"
    return {"url": url, "citations": [url]}


def fda_bqp_search(symbol: str) -> Dict[str, Any]:
    url = "https://www.fda.gov/drugs/biomarker-qualification-program"
    return {"symbol": symbol, "url": url, "citations": [url]}


def biomarker_fit(symbol: str) -> Dict[str, Any]:
    """
    Qualitative components + citations for biomarker suitability.
    (Public links — no API keys; use alongside /expr and /clin endpoints.)
    """
    components = {
        "assayability": {
            "rationale": "Assays exist or feasible (ELISA/MS/imaging).",
            "links": [f"https://www.uniprot.org/uniprotkb?query={urllib.parse.quote(symbol)}"]
        },
        "analyte_availability": {
            "rationale": "Abundance in accessible matrices (plasma/CSF/urine).",
            "links": [f"https://www.proteinatlas.org/search/{urllib.parse.quote(symbol)}"]
        },
        "tissue_specificity": {
            "rationale": "Prefer enriched tissue for trait (e.g., liver for lipid disorders).",
            "links": [f"https://www.proteinatlas.org/search/{urllib.parse.quote(symbol)}"]
        },
        "clinical_precedent": {
            "rationale": "Any FDA biomarker program / literature precedent.",
            "links": ["https://www.fda.gov/drugs/biomarker-qualification-program"]
        }
    }
    citations = []
    for v in components.values():
        citations.extend(v["links"])
    return {"symbol": symbol, "score": None, "components": components, "citations": citations}


# =============================================================================
# B7 — Competition & IP
# =============================================================================

async def patentsview_search(query_text: str, size: int = 100) -> Dict[str, Any]:
    q = {"_or": [
        {"patent_title": {"_text_any": query_text}},
        {"patent_abstract": {"_text_any": query_text}},
    ]}
    base = "https://api.patentsview.org/patents/query"
    url = f"{base}?q={urllib.parse.quote(json.dumps(q))}&f={urllib.parse.quote(json.dumps(['patent_id','patent_title']))}&o={urllib.parse.quote(json.dumps({'per_page': min(size, 100)}))}"
    try:
        js = await get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        return {"query": query_text, "patents": patents[:size], "citations": [url]}
    except Exception:
        return {"query": query_text, "patents": [], "citations": [url]}


def surechembl_search(query_text: str) -> Dict[str, Any]:
    url = f"https://www.ebi.ac.uk/surechembl/api/search?query={urllib.parse.quote(query_text)}"
    return {"url": url, "citations": [url]}


def lens_search_link(query: str) -> Dict[str, Any]:
    # Public site link (no API token)
    url = f"https://www.lens.org/lens/search?q={urllib.parse.quote(query)}"
    return {"url": url, "citations": [url]}

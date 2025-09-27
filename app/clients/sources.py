
""" 
Client layer for calling upstream services used by the TargetVal gateway.

This module proxies public REST/GraphQL APIs only — **no synthetic payloads**.
On success, helpers return minimal, normalized dicts that always include a
`citations` list of the exact URLs hit. On transient failures, helpers return
empty results with citations where possible; for endpoints that require
credentials (e.g., BioGRID ORCS), we raise a clear RuntimeError if the required
environment variables are missing.

v1.6.0 — aligned with latest router/main:
- B1: L2G via GWAS Catalog; Mendelian via OMIM (primary) with Orphanet/ClinGen links; MR via MR-Base/IEU; sQTL via eQTL Catalogue v2.
- B2: Disease association from GEO/ArrayExpress; proteomics via CPTAC/ProteomeXchange; scRNA via Tabula Sapiens/HCA; perturbation via DepMap/Achilles (ORCS optional).
- B3: HPA/UniProt/GXA maintained.
- B4: Pathways include Reactome + KEGG + WikiPathways; ligrec via OmniPath.
- B5: Drugs via ChEMBL/DrugBank/Inxight; antibody ligandability via SAbDab/Thera‑SAbDab; immunogenicity via IEDB IQ‑API.
- B6: Clinical endpoints via CT.gov v2 + WHO ICTRP + EU CTR; NEW biomarker_fit helper.
- B7: IP search via Lens.org.

Upstream services touched here:
- GWAS Catalog (REST)
- EpiGraphDB (EFO resolver; MR multi‑SNP)
- DisGeNET (REST) — retained for optional use
- OMIM (REST, requires OMIM_API_KEY) + Orphanet/ClinGen links
- RNAcentral (REST)
- miRNet 2.0 (REST, POST)
- eQTL Catalogue v2 (REST)
- GEO / ArrayExpress (REST/links)
- CPTAC / ProteomeXchange (links)
- Tabula Sapiens / Human Cell Atlas (links)
- Reactome (REST), KEGG REST, WikiPathways
- STRING (REST)
- OmniPath (REST)
- UniProt / Proteins API (REST)
- ChEMBL (REST), DrugBank (site search link), Inxight Drugs (site search link)
- IEDB IQ‑API (PostgREST)
- openFDA FAERS (REST)
- ClinicalTrials.gov v2 (REST), WHO ICTRP (docs/API), EU CTR (site search)
- Lens.org (site search link)
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
    """Resolve label/id → {efo_id, efo_uri, label, citations} via EpiGraphDB (fuzzy)."""
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
            return {"efo_id": _norm_efo_id(eid), "efo_uri": uri, "label": label, "citations": [url]}
    except Exception:
        pass
    eid = _norm_efo_id(term)
    return {"efo_id": eid, "efo_uri": f"http://www.ebi.ac.uk/efo/{eid}" if eid.startswith("EFO_") else None, "label": term, "citations": [url]}


def _assoc_matches_efo(assoc: Dict[str, Any], efo_id: str, efo_uri: Optional[str], label: str) -> bool:
    targets = {efo_id.lower(), (efo_uri or "").lower(), label.lower()}
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
    maybe = [assoc.get("disease_name"), assoc.get("diseaseid"), assoc.get("disease_name_upper"), assoc.get("diseaseid_db")]
    return any(isinstance(x, str) and label.lower() in x.lower() for x in maybe if x)


# =============================================================================
# B1 — Human genetics & causality
# =============================================================================

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


async def omim_gene_map(gene: str) -> Dict[str, Any]:
    """OMIM geneMap slice (requires OMIM_API_KEY)."""
    api_key = os.getenv("OMIM_API_KEY")
    if not api_key:
        raise RuntimeError("OMIM_API_KEY not set. Cannot call OMIM.")
    url = (
        "https://api.omim.org/api/entry/search"
        f"?search={urllib.parse.quote(gene)}&include=geneMap&format=json&apiKey={urllib.parse.quote(api_key)}"
    )
    try:
        js = await get_json(url)
        entries = ((js.get("omim", {}) or {}).get("searchResponse", {}) or {}).get("entryList", [])
        return {"entries": entries, "citations": [url]}
    except Exception:
        return {"entries": [], "citations": [url]}


def orphanet_links(gene: str) -> Dict[str, Any]:
    return {
        "links": {
            "orphanet_search": f"https://www.orpha.net/consor/cgi-bin/Disease_Search.php?lng=EN&data_id=Index_search&search={urllib.parse.quote(gene)}",
            "clingen_gene": f"https://search.clinicalgenome.org/kb/genes/{urllib.parse.quote(gene)}"
        },
        "citations": []
    }


async def ieu_mr(exposure_gene_symbol: str, outcome_efo_or_label: str) -> Dict[str, Any]:
    """Two-sample MR via EpiGraphDB MR-Base multi-snp API (stable)."""
    e = await resolve_efo(outcome_efo_or_label)
    base = "https://api.epigraphdb.org/xqtl/multi-snp-mr"
    url = f"{base}?exposure_gene={urllib.parse.quote(exposure_gene_symbol)}&outcome_trait={urllib.parse.quote(e['label'])}"
    try:
        js = await get_json(url)
        rows = js.get("results", []) if isinstance(js, dict) else (js or [])
        return {"mr": rows, "outcome_trait": e["label"], "efo": e, "citations": [url] + e["citations"]}
    except Exception:
        return {"mr": [], "outcome_trait": e["label"], "efo": e, "citations": [url] + e["citations"]}


async def eqtl_catalogue_sqtls(gene: str, size: int = 200) -> Dict[str, Any]:
    base = "https://www.ebi.ac.uk/eqtl/api/v2/associations"
    sq = {"gene_id": gene, "qtl_group": "sQTL", "size": min(size, 200)}
    eq = {"gene_id": gene, "qtl_group": "eQTL", "size": min(size, 200)}
    return {"params": {"sqtl": sq, "eqtl": eq}, "citations": [base]}


# =============================================================================
# B2 — Disease association & perturbation
# =============================================================================

async def geo_arrayexpress(condition: str) -> Dict[str, Any]:
    geo = f"https://www.ncbi.nlm.nih.gov/gds/?term=liver%20AND%20({urllib.parse.quote(condition)})"
    ae  = f"https://www.ebi.ac.uk/biostudies/arrayexpress/studies?query=liver%20{urllib.parse.quote(condition)}"
    return {"condition": condition, "links": {"geo": geo, "arrayexpress": ae}, "citations": [geo, ae]}


async def cptac_proteomics(condition: str) -> Dict[str, Any]:
    cptac = "https://cptac-data-portal.georgetown.edu/"
    px    = f"http://proteomecentral.proteomexchange.org/cgi/GetDataset?ID=PXD*&q=liver%20{urllib.parse.quote(condition)}"
    return {"condition": condition, "links": {"cptac": cptac, "proteomexchange": px}, "citations": [cptac, px]}


async def tabula_hca(condition: str) -> Dict[str, Any]:
    ts = "https://tabula-sapiens-portal.ds.czbiohub.org/"
    hca = "https://data.humancellatlas.org/"
    return {"condition": condition, "links": {"tabula_sapiens": ts, "hca": hca}, "citations": [ts, hca]}


async def depmap_achilles(condition: str) -> Dict[str, Any]:
    depmap = "https://depmap.org/portal/"
    ach    = "https://depmap.org/portal/download/all/"
    return {"condition": condition, "links": {"depmap_portal": depmap, "achilles_downloads": ach}, "citations": [depmap, ach]}


async def orcs_perturb(condition: str, limit: int = 100) -> Dict[str, Any]:
    """Optional BioGRID ORCS live pull; requires ORCS_ACCESS_KEY."""
    access_key = os.getenv("ORCS_ACCESS_KEY")
    if not access_key:
        raise RuntimeError("Missing ORCS_ACCESS_KEY for BioGRID ORCS access.")
    base = "https://orcsws.thebiogrid.org/screens/"
    list_url = f"{base}?accesskey={urllib.parse.quote(access_key)}&format=json&organismID=9606&conditionName={urllib.parse.quote(condition)}&start=0&max=50"
    try:
        screens = await get_json(list_url)
        screens_list = screens if isinstance(screens, list) else []
        screens_list = screens_list[: min(3, len(screens_list))]
        hits_collected: List[Dict[str, Any]] = []
        for sc in screens_list:
            sc_id = sc.get("SCREEN_ID") or sc.get("ID") or sc.get("SCREENID")
            if not sc_id:
                continue
            res_url = f"https://orcsws.thebiogrid.org/results/?accesskey={urllib.parse.quote(access_key)}&format=json&screenID={urllib.parse.quote(str(sc_id))}&start=0&max=500"
            try:
                hits = await get_json(res_url)
                for h in hits if isinstance(hits, list) else []:
                    if h.get("HIT") in (True, "true", "YES", "Yes", "yes"):
                        hits_collected.append({"screen_id": sc_id, "gene": h.get("OFFICIAL_SYMBOL"), "effect": h.get("PHENOTYPE") or "hit"})
            except Exception:
                pass
        return {"condition": condition, "screens_n": len(screens_list), "top_hits": hits_collected[:limit], "citations": [list_url]}
    except Exception:
        return {"condition": condition, "screens_n": 0, "top_hits": [], "citations": [list_url]}


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


async def hpa_search(symbol: str) -> Dict[str, Any]:
    url = f"https://www.proteinatlas.org/search/{urllib.parse.quote(symbol)}"
    return {"url": url, "citations": [url]}


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
    mech_url = f"https://www.ebi.ac.uk/chembl/api/data/mechanism.json?target_chembl_id={urllib.parse.quote(symbol)}"
    tgt_url  = f"https://www.ebi.ac.uk/chembl/api/data/target.json?search={urllib.parse.quote(symbol)}"
    try:
        mech = await get_json(mech_url)
    except Exception:
        mech = {}
    try:
        tgt = await get_json(tgt_url)
    except Exception:
        tgt = {}
    return {"mechanism": mech, "targets": tgt, "citations": [mech_url, tgt_url]}


def drugbank_search(symbol: str) -> Dict[str, Any]:
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

async def openfda_faers_reactions(drug_name: str) -> Dict[str, Any]:
    url = (
        "https://api.fda.gov/drug/event.json?"
        f"search=patient.drug.medicinalproduct:{urllib.parse.quote(drug_name)}&count=patient.reaction.reactionmeddrapt.exact"
    )
    try:
        js = await get_json(url)
        return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}
    except Exception:
        return {"results": [], "citations": [url]}


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
    # Programmatic endpoints are limited; provide canonical API root/docs.
    root = "https://trialsearch.who.int/Api"
    docs = "https://trialsearch.who.int/"
    return {"root": root, "docs": docs, "citations": [root, docs]}


def eu_ctr_search(condition: str) -> Dict[str, Any]:
    url = f"https://www.clinicaltrialsregister.eu/ctr-search/search?query={urllib.parse.quote(condition)}"
    return {"url": url, "citations": [url]}


def biomarker_fit(symbol: str) -> Dict[str, Any]:
    """Return qualitative components and citations for biomarker suitability."""
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
    return {"symbol": symbol, "score": None, "components": components, "citations": sum([v["links"] for v in components.values()], [])}


# =============================================================================
# B7 — Competition & IP
# =============================================================================

def lens_search(query: str) -> Dict[str, Any]:
    url = f"https://www.lens.org/lens/search?q={urllib.parse.quote(query)}"
    return {"url": url, "citations": [url]}

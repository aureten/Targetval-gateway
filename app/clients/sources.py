"""
Client layer for calling upstream services used by the TargetVal gateway.

This module proxies public REST/GraphQL APIs only — **no synthetic payloads**.
On success, helpers return minimal, normalized dicts that always include a
`citations` list of the exact URLs hit. On transient failures, helpers return
empty results with citations where possible; for endpoints that require
credentials (e.g., BioGRID ORCS), we raise a clear RuntimeError if the required
environment variables are missing.

Legacy helpers that previously returned fabricated data have been either
removed or re-implemented to call live services. Where a legacy function name
is kept for compatibility, its docstring notes any semantics change.

Upstream services touched here:
- Open Targets Genetics & Platform (GraphQL)
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
- UniProt (REST)
- IEDB IQ-API (PostgREST)
- openFDA FAERS (REST)
- ClinicalTrials.gov v2 (REST)
- PatentsView (REST)

NOTE: These helpers are optional. The current router endpoints already call
most of these sources directly with retry/backoff.
"""

from __future__ import annotations

import os
import json
import urllib.parse
from typing import Optional, Any, Dict, List

from app.utils.http import get_json, post_json


# ---------------------------------------------------------------------------
# Human genetics & causality
# ---------------------------------------------------------------------------

async def ot_genetics_l2g(ensembl_gene_id: str, efo_id: str) -> dict:
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


async def gnomad_constraint(symbol: str) -> dict:
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


async def monarch_mendelian(symbol_or_entrez: str) -> dict:
    """Mendelian gene–disease associations from Monarch Initiative (live REST)."""
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol_or_entrez)}/diseases"
    try:
        js = await get_json(url)
        return {"associations": js.get("associations", []), "citations": [url]}
    except Exception:
        return {"associations": [], "citations": [url]}


async def ieu_mr(exposure_id: str, outcome_id: str) -> dict:
    """
    Legacy name retained. Now performs multi‑SNP MR via EpiGraphDB's live API.

    Usage:
      - `exposure_id`: pass a human gene symbol (e.g., "IL6")
      - `outcome_id`: pass an EFO ID ("EFO_0000637") or a disease/trait label ("asthma")
    We fuzzy‑map the outcome via EpiGraphDB /ontology/disease-efo, then call
    /xqtl/multi-snp-mr.

    Returns:
      { "mr": [ ...rows... ], "outcome_trait": <resolved>, "citations": [ ... ] }
    """
    # Resolve outcome label (tolerate either EFO or free text)
    disease_label = outcome_id
    map_url = f"https://api.epigraphdb.org/ontology/disease-efo?efo_term={urllib.parse.quote(outcome_id)}&fuzzy=true"
    try:
        mapping = await get_json(map_url)
        results = mapping.get("results", []) if isinstance(mapping, dict) else []
        if results:
            top = results[0]
            disease_label = top.get("disease_label") or (top.get("disease") or {}).get("label") or disease_label
    except Exception:
        pass

    mr_url = f"https://api.epigraphdb.org/xqtl/multi-snp-mr?exposure_gene={urllib.parse.quote(exposure_id)}&outcome_trait={urllib.parse.quote(disease_label)}"
    try:
        mr = await get_json(mr_url)
        rows = mr.get("results", []) if isinstance(mr, dict) else (mr or [])
        return {"mr": rows, "outcome_trait": disease_label, "citations": [mr_url, map_url]}
    except Exception:
        return {"mr": [], "outcome_trait": disease_label, "citations": [mr_url, map_url]}


# ---------------------------------------------------------------------------
# RNA / expression / functional genomics
# ---------------------------------------------------------------------------

async def lncrna(symbol: str, limit: int = 50) -> dict:
    """lncRNA/circRNA sequences from RNAcentral for a gene symbol (live REST)."""
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(symbol)}&page_size={limit}"
    try:
        js = await get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return {"lncRNAs": results[:limit], "citations": [url]}
    except Exception:
        return {"lncRNAs": [], "citations": [url]}


async def mirna(symbol: str, limit: int = 100) -> dict:
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


async def eqtl_catalogue_gene(symbol: str) -> dict:
    """Gene‑level eQTL Catalogue data (live REST)."""
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    js = await get_json(url)
    return {"results": js if isinstance(js, list) else [], "citations": [url]}


async def encode_chipseq(symbol: str) -> dict:
    """Epigenetics (ChIP‑seq) metadata from ENCODE for a gene (live REST)."""
    query = urllib.parse.quote(f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}")
    url = f"https://www.encodeproject.org/{query}&format=json"
    js = await get_json(url)
    return {"experiments": js.get("@graph", []) if isinstance(js, dict) else [], "citations": [url]}


async def expression_atlas_experiments(condition: str) -> dict:
    """Expression Atlas experiments matching a condition term (live REST)."""
    url = f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    js = await get_json(url)
    return {"experiments": js.get("experiments", []) if isinstance(js, dict) else [], "citations": [url]}


async def pride_projects(condition: str) -> dict:
    """PRIDE proteomics projects matching a condition keyword (live REST)."""
    url = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    js = await get_json(url)
    return {"projects": js if isinstance(js, list) else [], "citations": [url]}


async def cellxgene(condition: str, limit: int = 100) -> dict:
    """
    Legacy name retained. Uses Human Protein Atlas search API as a practical
    single‑cell proxy by surfacing records that include single‑cell fields.
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
        return {
            "condition": condition,
            "cell_types": cell_types[:limit],
            "records": out[:limit],
            "citations": [hpa_url],
        }
    except Exception:
        return {"condition": condition, "cell_types": [], "records": [], "citations": [hpa_url]}


async def perturb(condition: str, limit: int = 100) -> dict:
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

        return {
            "condition": condition,
            "screens_n": len(screens_list),
            "top_hits": hits_collected[:limit],
            "citations": [list_url],
        }
    except Exception:
        return {"condition": condition, "screens_n": 0, "top_hits": [], "citations": [list_url]}


async def expression_atlas_gene(symbol: str) -> dict:
    """Baseline gene expression from Expression Atlas (live REST)."""
    url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    js = await get_json(url)
    return {"raw": js, "citations": [url]}


async def uniprot_localization(symbol: str) -> dict:
    """Subcellular localisation from UniProt (live REST)."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}


async def inducibility(symbol: str, limit: int = 50) -> dict:
    """
    Legacy name retained. Now returns GEO dataset IDs (GSE/GDS) mentioning the gene
    via E-utilities search (live REST).
    """
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


# ---------------------------------------------------------------------------
# Mechanistic wiring & networks
# ---------------------------------------------------------------------------

async def reactome_search(symbol: str) -> dict:
    """Reactome pathway search hits for a gene (live REST)."""
    url = f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}


async def string_map_and_network(symbol: str) -> dict:
    """STRING mapping + network edges for a gene (live REST)."""
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    ids = await get_json(map_url)
    if not ids:
        return {"string_id": None, "edges": [], "citations": [map_url]}
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    net = await get_json(net_url)
    return {"string_id": sid, "edges": net if isinstance(net, list) else [], "citations": [map_url, net_url]}


async def omnipath_ligrec(symbol: str) -> dict:
    """Ligand–receptor interactions from OmniPath (live REST)."""
    url = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    js = await get_json(url)
    return {"rows": js if isinstance(js, list) else [], "citations": [url]}


# ---------------------------------------------------------------------------
# Tractability & modality
# ---------------------------------------------------------------------------

async def ot_platform_known_drugs(symbol: str) -> dict:
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


async def pdb_search(symbol: str) -> dict:
    """RCSB PDB text search for entries matching a symbol (live REST)."""
    q = {
        "query": {"type": "terminal", "service": "text", "parameters": {"value": symbol}},
        "request_options": {"return_all_hits": True},
        "return_type": "entry",
    }
    url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps(q))
    js = await get_json(url)
    return {"hits": js.get("result_set", []) if isinstance(js, dict) else [], "citations": ["https://www.rcsb.org/"]}


async def uniprot_topology(symbol: str) -> dict:
    """Protein topology info from UniProt (live REST)."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    js = await get_json(url)
    return {"results": js.get("results", []) if isinstance(js, dict) else [], "citations": [url]}


async def rnacentral_oligo(symbol: str, limit: int = 100) -> dict:
    """
    Legacy name retained. Now proxies Ribocentre Aptamer API to surface nucleic‑acid
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


async def modality(symbol: str) -> dict:
    """
    Live heuristic modality assessment integrating COMPARTMENTS, ChEMBL and PDBe.
    Mirrors router logic but as a reusable helper.
    """
    comp_url = f"https://compartments.jensenlab.org/Service?gene_names={urllib.parse.quote(symbol)}&format=json"
    chembl_url = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(symbol)}&format=json"
    pdbe_url = f"https://www.ebi.ac.uk/pdbe/api/proteins/{urllib.parse.quote(symbol)}"

    try:
        comp = await get_json(comp_url)
    except Exception:
        comp = {}
    try:
        chem = await get_json(chembl_url)
    except Exception:
        chem = {}
    try:
        pdbe = await get_json(pdbe_url)
    except Exception:
        pdbe = {}

    compres = comp.get(symbol, []) if isinstance(comp, dict) else []
    chemblres = chem.get("targets", []) if isinstance(chem, dict) else []
    pdbe_entries: List[Any] = []
    if isinstance(pdbe, dict):
        for _, vals in pdbe.items():
            if isinstance(vals, list):
                pdbe_entries.extend(vals)

    # Simple transparent scoring
    loc_terms = " ".join([str(x) for x in compres]).lower()
    is_extracellular = any(t in loc_terms for t in ["secreted", "extracellular", "extracellular space"])
    is_membrane = any(t in loc_terms for t in ["plasma membrane", "cell membrane", "membrane"])
    has_chembl = len(chemblres) > 0
    has_structure = len(pdbe_entries) > 0

    sm_score = (0.6 if has_chembl else 0.0) + (0.25 if has_structure else 0.0) - (0.15 if (is_extracellular and not is_membrane) else 0.0)
    ab_score = (0.6 if (is_membrane or is_extracellular) else 0.0) + (0.25 if has_structure else 0.0) - (0.1 if (has_chembl and not (is_membrane or is_extracellular)) else 0.0)
    oligo_score = (0.5 if (is_extracellular or ("cytosol" in loc_terms) or ("nucleus" in loc_terms)) else 0.0) + (0.1 if has_structure else 0.0) + (0.05 if has_chembl else 0.0)

    def clamp(x: float) -> float:
        return max(0.0, min(1.0, round(x, 3)))

    recommendation = sorted(
        [("small_molecule", clamp(sm_score)), ("antibody", clamp(ab_score)), ("oligo", clamp(oligo_score))],
        key=lambda kv: kv[1],
        reverse=True,
    )

    return {
        "symbol": symbol,
        "recommendation": recommendation,
        "rationale": {
            "is_extracellular": is_extracellular,
            "is_membrane": is_membrane,
            "chembl_targets_n": len(chemblres),
            "pdbe_structures_n": len(pdbe_entries),
        },
        "snippets": {
            "compartments": compres[:25],
            "chembl_targets": chemblres[:10],
            "pdbe_entries": pdbe_entries[:10],
        },
        "citations": [comp_url, chembl_url, pdbe_url],
    }


async def iedb_immunogenicity(symbol: str, limit: int = 50) -> dict:
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
        return {"symbol": symbol, "epitopes_n": 0, "tcell_assays_n": 0, "hla_breakdown": [], "examples": {"epitopes": [], "tcell_assays": []}, "citations": [epi_url, tc_url]}


# ---------------------------------------------------------------------------
# Clinical translation & safety
# ---------------------------------------------------------------------------

async def openfda_faers_reactions(drug_name: str) -> dict:
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


async def ot_platform_known_drugs_count(symbol: str) -> dict:
    """Known drugs count from Open Targets Platform (live GraphQL)."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {"query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }", "variables": {"sym": symbol}}
    body = await post_json(gql, query)
    count = (((body.get("data", {}) or {}).get("target") or {}).get("knownDrugs") or {}).get("count")
    return {"count": count, "citations": [gql]}


async def ctgov_trial_count(condition: Optional[str]) -> dict:
    """Total number of trials for a condition from ClinicalTrials.gov v2 (live REST)."""
    if not condition:
        return {"totalStudies": None, "citations": []}
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&countTotal=true"
    js = await get_json(url)
    return {"totalStudies": js.get("totalStudies"), "citations": [url]}


# ---------------------------------------------------------------------------
# Competition & IP
# ---------------------------------------------------------------------------

async def ip(query: str, limit: int = 100) -> dict:
    """
    Patent search via PatentsView (live REST). Returns simple identifiers+titles.
    """
    pv_query = {"_or": [{"patent_title": {"_text_any": query}}, {"patent_abstract": {"_text_any": query}}]}
    q_str = urllib.parse.quote(json.dumps(pv_query))
    fields = urllib.parse.quote(json.dumps(["patent_id", "patent_title", "patent_date"]))
    url = f"https://api.patentsview.org/patents/query?q={q_str}&f={fields}"
    try:
        js = await get_json(url)
        patents = js.get("patents", []) if isinstance(js, dict) else []
        # Normalize a compact view
        out = [{"patent_id": p.get("patent_id"), "title": p.get("patent_title"), "date": p.get("patent_date")} for p in patents]
        return {"query": query, "patents": out[:limit], "citations": [url]}
    except Exception:
        return {"query": query, "patents": [], "citations": [url]}

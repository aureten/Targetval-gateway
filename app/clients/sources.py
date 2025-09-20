"""
Client layer for calling upstream services used by the TargetVal gateway.

This module replaces the earlier stubbed implementations with thin wrappers
around public REST APIs.  Functions here should never return hard‑coded
data structures; instead they either proxy a live API or fall back to
returning empty results with appropriate citations when a remote call
fails.  A simple in‑memory cache is provided via ``get_json`` and
``post_json`` in ``app.utils.http`` to avoid hammering third‑party
services on repeat calls.

You can add new clients here as additional modules come online.  See
``app/routers/targetval_router.py`` for how these client functions are
composed into Evidence responses.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional, Any, Dict, List

from app.utils.http import get_json, post_json

# ---------------------------------------------------------------------------
# Human genetics & causality
# ---------------------------------------------------------------------------

async def ot_genetics_l2g(ensembl_gene_id: str, efo_id: str) -> dict:
    """Query Open Targets Genetics (L2G) for gene–disease colocalisation.

    Returns a dictionary with a list of colocalisation hits and the URL used
    as a citation.  Any exceptions are propagated upwards to the router
    layer where they are handled uniformly.
    """
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
    """Retrieve gnomAD constraint scores (pLI/LOEUF) for the given gene.

    Uses the public gnomAD GraphQL API.  Returns the raw gene constraint
    structure plus a citation to the API endpoint.
    """
    gql = "https://gnomad.broadinstitute.org/api"
    query = {
        "query": """
            query Rare($symbol:String!){
              gene(gene_symbol:$symbol, reference_genome:GRCh38){
                gene_id gene_symbol
                constraint{ lof_z n_lof expected_lof pLI }
              }
            }
        """,
        "variables": {"symbol": symbol},
    }
    body = await post_json(gql, query)
    return {
        "gene": (body.get("data", {}) or {}).get("gene"),
        "citations": [gql],
    }


async def monarch_mendelian(symbol_or_entrez: str) -> dict:
    """Fetch Mendelian gene–disease associations from Monarch Initiative.

    Returns an empty list on error rather than raising.  Always includes
    the URL called in the citations list.
    """
    url = f"https://api.monarchinitiative.org/api/bioentity/gene/NCBIGene:{urllib.parse.quote(symbol_or_entrez)}/diseases"
    try:
        js = await get_json(url)
        return {"associations": js.get("associations", []), "citations": [url]}
    except Exception:
        return {"associations": [], "citations": [url]}


async def ieu_mr(exposure_id: str, outcome_id: str) -> dict:
    """Placeholder for Mendelian randomisation using OpenGWAS/IEU resources.

    The IEU MR API requires compute resources and is not fully exposed for
    simple HTTP requests.  Until a dedicated service is available this
    function returns an explanatory note along with the input IDs.  Downstream
    callers should interpret a missing MR signal as a lack of evidence rather
    than an error.
    """
    return {
        "assumptions": "MR compute not executed; integrate with IEU when available",
        "exposure_id": exposure_id,
        "outcome_id": outcome_id,
        "citations": ["https://gwas.mrcieu.ac.uk/"],
    }


async def lncrna(symbol: str, limit: int = 50) -> dict:
    """Fetch lncRNA/circRNA sequences from RNAcentral matching a gene symbol.

    If no results are returned or the API fails, an empty list is returned.
    """
    url = f"https://rnacentral.org/api/v1/rna?q={urllib.parse.quote(symbol)}&page_size={limit}"
    try:
        js = await get_json(url)
        results = js.get("results", []) if isinstance(js, dict) else []
        return {"lncRNAs": results[:limit], "citations": [url]}
    except Exception:
        return {"lncRNAs": [], "citations": [url]}


async def mirna(symbol: str, limit: int = 50) -> dict:
    """Placeholder for miRNA interactions.

    TarBase/miRTarBase APIs are not publicly available for high‑throughput
    queries.  This function returns a note and citations to the appropriate
    resources.  When a programmatic API becomes available, replace this
    implementation with a real call.
    """
    return {
        "note": "miRTarBase/TargetScan not used programmatically here",
        "symbol": symbol,
        "citations": ["https://mirtarbase.cuhk.edu.cn/", "https://www.targetscan.org/"],
    }


async def eqtl_catalogue_gene(symbol: str) -> dict:
    """Return gene‑level eQTL Catalogue data for the given gene symbol."""
    url = f"https://www.ebi.ac.uk/eqtl/api/genes/{urllib.parse.quote(symbol)}"
    js = await get_json(url)
    return {
        "results": js if isinstance(js, list) else [],
        "citations": [url],
    }


async def encode_chipseq(symbol: str) -> dict:
    """Return epigenetics (ChIP‑seq) metadata from ENCODE for a gene."""
    query = urllib.parse.quote(
        f"search/?type=Experiment&assay_slims=ChIP-seq&searchTerm={symbol}"
    )
    url = f"https://www.encodeproject.org/{query}&format=json"
    js = await get_json(url)
    return {
        "experiments": js.get("@graph", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def expression_atlas_experiments(condition: str) -> dict:
    """Return expression atlas experiment metadata for a disease/condition."""
    url = (
        f"https://www.ebi.ac.uk/gxa/experiments?query={urllib.parse.quote(condition)}&species=Homo%20sapiens&format=json"
    )
    js = await get_json(url)
    return {
        "experiments": js.get("experiments", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def pride_projects(condition: str) -> dict:
    """Return proteomics project identifiers from PRIDE for a condition."""
    url = (
        f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}"
    )
    js = await get_json(url)
    return {
        "projects": js if isinstance(js, list) else [],
        "citations": [url],
    }


async def cellxgene(condition: str) -> dict:
    """Placeholder for cellxgene single‑cell expression queries.

    The cellxgene census API requires authentication and large data downloads.  Until
    a simplified endpoint is exposed, this function returns a note with
    citations.
    """
    return {
        "note": "Use cellxgene/HCA portals; not API‑native here",
        "citations": [
            "https://cellxgene.cziscience.com/",
            "https://data.humancellatlas.org/",
        ],
    }


async def perturb(condition: str) -> dict:
    """Placeholder for CRISPR perturbation evidence.

    BioGRID ORCS and DepMap currently provide user‑interface‑first portals; there
    is no public API for bulk downloads.  This function returns explanatory
    notes and citations.  When APIs become available, replace with a call.
    """
    return {
        "note": "BioGRID‑ORCS/DepMap UI‑first; wire later",
        "citations": [
            "https://orcs.thebiogrid.org/",
            "https://depmap.org/portal/",
        ],
    }


async def expression_atlas_gene(symbol: str) -> dict:
    """Return baseline gene expression data for a gene from Expression Atlas."""
    url = f"https://www.ebi.ac.uk/gxa/genes/{urllib.parse.quote(symbol)}.json"
    js = await get_json(url)
    return {
        "raw": js,
        "citations": [url],
    }


async def uniprot_localization(symbol: str) -> dict:
    """Return subcellular localisation data from UniProt for a gene."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = (
        f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,protein_name,genes,cc_subcellular_location&format=json&size=1"
    )
    js = await get_json(url)
    return {
        "results": js.get("results", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def inducibility(symbol: str, stimulus: Optional[str] = None) -> dict:
    """Placeholder for inducibility evidence (GEO time courses)."""
    return {
        "note": "GEO time‑course matrix downloads are omitted here",
        "citations": ["https://www.ncbi.nlm.nih.gov/geo/"],
    }


async def reactome_search(symbol: str) -> dict:
    """Return pathway search results from Reactome for a gene."""
    url = (
        f"https://reactome.org/ContentService/search/query?query={urllib.parse.quote(symbol)}&species=Homo%20sapiens"
    )
    js = await get_json(url)
    return {
        "results": js.get("results", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def string_map_and_network(symbol: str) -> dict:
    """Return STRING network information for a gene.

    First obtains the STRING identifier for the gene, then returns the network
    edges.  If no identifiers are found, returns empty results.
    """
    map_url = f"https://string-db.org/api/json/get_string_ids?identifiers={urllib.parse.quote(symbol)}&species=9606"
    ids = await get_json(map_url)
    if not ids:
        return {"string_id": None, "edges": [], "citations": [map_url]}
    sid = ids[0].get("stringId")
    net_url = f"https://string-db.org/api/json/network?identifiers={sid}&species=9606"
    net = await get_json(net_url)
    return {
        "string_id": sid,
        "edges": net if isinstance(net, list) else [],
        "citations": [map_url, net_url],
    }


async def omnipath_ligrec(symbol: str) -> dict:
    """Return ligand–receptor interactions for a gene from OmniPath."""
    url = (
        f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(symbol)}&organisms=9606&fields=sources,dorothea_level"
    )
    js = await get_json(url)
    return {
        "rows": js if isinstance(js, list) else [],
        "citations": [url],
    }


async def ot_platform_known_drugs(symbol: str) -> dict:
    """Return known drug interactions for a gene from the Open Targets Platform."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": """
            query Q($sym:String!){
              target(approvedSymbol:$sym){
                id approvedSymbol
                knownDrugs{ rows{ drugType drug{ id name } disease{ id name } phase } count }
              }
            }
        """,
        "variables": {"sym": symbol},
    }
    body = await post_json(gql, query)
    t = (body.get("data", {}) or {}).get("target") or {}
    kd = (t.get("knownDrugs") or {})
    return {
        "knownDrugs": kd.get("rows", []),
        "count": kd.get("count"),
        "citations": [gql],
    }


async def pdb_search(symbol: str) -> dict:
    """Search the Protein Data Bank (PDB) for entries matching a gene symbol."""
    import json as _json
    url = (
        "https://search.rcsb.org/rcsbsearch/v2/query?json="
        + urllib.parse.quote(
            _json.dumps(
                {
                    "query": {
                        "type": "terminal",
                        "service": "text",
                        "parameters": {"value": symbol},
                    },
                    "request_options": {"return_all_hits": True},
                    "return_type": "entry",
                }
            )
        )
    )
    js = await get_json(url)
    return {
        "hits": js.get("result_set", []) if isinstance(js, dict) else [],
        "citations": ["https://www.rcsb.org/"],
    }


async def uniprot_topology(symbol: str) -> dict:
    """Return protein topology information from UniProt for a gene."""
    query = urllib.parse.quote(f"gene_exact:{symbol}+AND+organism_id:9606")
    url = (
        f"https://rest.uniprot.org/uniprotkb/search?query={query}&fields=accession,cc_subcellular_location,cc_topology&format=json&size=1"
    )
    js = await get_json(url)
    return {
        "results": js.get("results", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def rnacentral_oligo(symbol: str) -> dict:
    """Placeholder for oligo design evidence (RiboAPT/RNACentral)."""
    return {
        "note": "Oligo design requires RNACentral/TargetScan programmatic mapping later",
        "citations": ["https://rnacentral.org/", "https://www.targetscan.org/"],
    }


async def modality(symbol: str) -> dict:
    """Placeholder for therapeutic modality assessment."""
    return {
        "note": "Combine UniProt + SURFY/CSPA rules in later iteration",
        "citations": ["https://rest.uniprot.org/"],
    }


async def iedb_immunogenicity(symbol: str) -> dict:
    """Placeholder for immunogenicity evidence via IEDB."""
    return {
        "note": "IEDB API available; wire detailed params later",
        "citations": ["https://www.iedb.org/"],
    }


async def ctgov_studies_outcomes(condition: str) -> dict:
    """Return clinical outcomes from ClinicalTrials.gov for a condition."""
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&fields=protocolSection.outcomesModule"
    js = await get_json(url)
    return {
        "studies": js.get("studies", []) if isinstance(js, dict) else [],
        "citations": [url],
    }


async def rwe(condition: str) -> dict:
    """Placeholder for real‑world evidence queries."""
    return {
        "note": "RWE sources (Sentinel/N3C/SEER) are access‑controlled",
        "citations": [
            "https://www.sentinelinitiative.org/",
            "https://covid.cd2h.org/N3C",
            "https://seer.cancer.gov/",
        ],
    }


async def openfda_faers_reactions(drug_name: str) -> dict:
    """Return adverse event reaction counts from openFDA for a drug."""
    url = (
        f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug_name)}&count=patient.reaction.reactionmeddrapt.exact"
    )
    try:
        js = await get_json(url, tries=2)
        return {
            "results": js.get("results", []) if isinstance(js, dict) else [],
            "citations": [url],
        }
    except Exception:
        return {"results": [], "citations": [url]}


async def ot_platform_known_drugs_count(symbol: str) -> dict:
    """Return count of known drugs for a gene using the Open Targets Platform."""
    gql = "https://api.platform.opentargets.org/api/v4/graphql"
    query = {
        "query": "query Q($sym:String!){ target(approvedSymbol:$sym){ knownDrugs{ count } } }",
        "variables": {"sym": symbol},
    }
    body = await post_json(gql, query)
    count = (
        ((body.get("data", {}) or {}).get("target") or {}).get("knownDrugs") or {}
    ).get("count")
    return {
        "count": count,
        "citations": [gql],
    }


async def ctgov_trial_count(condition: Optional[str]) -> dict:
    """Return total number of trials for a condition from ClinicalTrials.gov."""
    if not condition:
        return {"totalStudies": None, "citations": []}
    url = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&countTotal=true"
    js = await get_json(url)
    return {
        "totalStudies": js.get("totalStudies"),
        "citations": [url],
    }


async def ip(query: str) -> dict:
    """Placeholder for intellectual property evidence via patent databases."""
    return {
        "note": "Use The Lens / Espacenet APIs or gateway later",
        "citations": ["https://www.lens.org/", "https://worldwide.espacenet.com/"],
    }

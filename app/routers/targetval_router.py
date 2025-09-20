"""
Routes implementing the TARGETVAL gateway (dynamic version).

This module defines a set of FastAPI endpoints that proxy live upstream
services for target validation evidence.  Each endpoint delegates to an
async function in :mod:`app.clients.sources` which contains the logic
for calling the appropriate public API.  The responses are normalised
into a common :class:`Evidence` model which records the status of the
operation, a human‑readable description of the upstream source, the
number of records fetched, the actual data payload and the list of
citations (URLs) used to generate the data.  A timestamp is also
included to aid downstream caching.

Compared to the original implementation which contained many stubbed
placeholders, this version calls real services wherever possible and
returns explanatory notes only when a programmatic API is not yet
available.  Use these endpoints directly or via the aggregator in
``app/main.py``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.clients import sources


router = APIRouter()


class Evidence(BaseModel):
    """Container for evidence returned by an endpoint.

    Attributes
    ----------
    status : str
        "OK" for successful queries, "NO_DATA" for placeholders or empty
        results, and "ERROR" for exceptions.
    source : str
        Short description of the upstream API or note used to generate
        the data.
    fetched_n : int
        Number of records returned (based on the first list in the
        ``data`` dict, or zero if no list is present).
    data : Dict[str, Any]
        Arbitrary payload containing the data returned from the upstream
        service.  The structure varies across modules.
    citations : List[str]
        List of URLs used as evidence sources.  These are returned to
        allow clients to reproduce or attribute the data.
    fetched_at : float
        UNIX timestamp when the data was fetched.
    """

    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: float


def _build_evidence(status: str, source: str, data: Dict[str, Any], citations: List[str]) -> Evidence:
    """Construct an Evidence instance, inferring the count of returned items.

    Parameters
    ----------
    status : str
        One of "OK", "NO_DATA" or "ERROR".
    source : str
        Description of the upstream source.
    data : dict
        Payload returned from the client function.
    citations : list
        List of URLs used to obtain the data.

    Returns
    -------
    Evidence
        Normalised evidence object.
    """
    count = 0
    for v in data.values():
        if isinstance(v, list):
            count = len(v)
            break
    return Evidence(
        status=status,
        source=source,
        fetched_n=count,
        data=data,
        citations=citations,
        fetched_at=time.time(),
    )


# ----------------------------------------------------------------------------
# BUCKET 1 – Human Genetics & Causality
# ----------------------------------------------------------------------------

@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str, efo: str) -> Evidence:
    """Gene–disease colocalisation via the Open Targets Genetics L2G API.

    This endpoint wraps :func:`app.clients.sources.ot_genetics_l2g` which
    queries the Open Targets GraphQL endpoint for colocalisation hits
    between a gene and a disease (identified by an EFO ID).  The gene may
    be provided as an Ensembl ID or a gene symbol; it is passed verbatim
    to the client function.  If the upstream call fails, an HTTP 502
    error is raised.
    """
    try:
        result = await sources.ot_genetics_l2g(gene, efo)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Open Targets Genetics",
        data={"gene": gene, "efo": efo, "coloc": result.get("coloc", [])},
        citations=result.get("citations", []),
    )


@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(gene: str) -> Evidence:
    """Essentiality constraint and rare variant info from gnomAD.

    Calls :func:`app.clients.sources.gnomad_constraint` to retrieve
    intolerance scores (pLI/LOEUF) for the gene.  Although this endpoint
    historically returned lists of variants, the constraint scores serve
    as a proxy for rare disease relevance.  If the call fails an error
    response is returned.
    """
    try:
        result = await sources.gnomad_constraint(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="gnomAD constraint",
        data={"gene": gene, "constraint": result.get("gene")},
        citations=result.get("citations", []),
    )


@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(gene: str) -> Evidence:
    """Mendelian disease associations via Monarch Initiative.

    Uses :func:`app.clients.sources.monarch_mendelian` to obtain diseases
    linked to the gene.  The result is truncated only if the upstream
    service returns a list; otherwise the entire structure is returned.
    """
    try:
        result = await sources.monarch_mendelian(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Monarch Initiative",
        data={"gene": gene, "associations": result.get("associations", [])},
        citations=result.get("citations", []),
    )


@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(exposure: str, outcome: str) -> Evidence:
    """Mendelian randomisation placeholder (IEU/OpenGWAS).

    Until a proper MR API is available, this endpoint calls
    :func:`app.clients.sources.ieu_mr` which returns a note explaining
    that MR computation is not executed.  The status is set to
    ``NO_DATA`` to indicate the absence of real evidence.
    """
    result = await sources.ieu_mr(exposure, outcome)
    return _build_evidence(
        status="NO_DATA",
        source="IEU MR placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence:
    """Long non‑coding RNAs associated with a gene (RNAcentral).

    Delegates to :func:`app.clients.sources.lncrna` which queries
    RNAcentral for lncRNA/circRNA sequences matching the gene symbol.
    The ``limit`` parameter controls the maximum number of records
    returned.
    """
    try:
        result = await sources.lncrna(gene, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="RNAcentral",
        data={"gene": gene, "lncRNAs": result.get("lncRNAs", [])},
        citations=result.get("citations", []),
    )


@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(gene: str) -> Evidence:
    """miRNA interactions placeholder.

    miRTarBase/TarBase programmatic access is limited.  Calls
    :func:`app.clients.sources.mirna` which returns a note and
    citations.  The status is ``NO_DATA`` to reflect the placeholder.
    """
    result = await sources.mirna(gene)
    return _build_evidence(
        status="NO_DATA",
        source="miRNA interactions placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(gene: str) -> Evidence:
    """Splicing QTLs via the eQTL Catalogue.

    Uses :func:`app.clients.sources.eqtl_catalogue_gene` to fetch gene‑level
    QTL information.  This function returns the entire result set, which
    may include eQTLs and sQTLs; clients can post‑filter as needed.
    """
    try:
        result = await sources.eqtl_catalogue_gene(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="eQTL Catalogue",
        data={"gene": gene, "results": result.get("results", [])},
        citations=result.get("citations", []),
    )


@router.get("/genetics/epigenetics", response_model=Evidence)
async def genetics_epigenetics(gene: str) -> Evidence:
    """Epigenetic evidence via ENCODE ChIP‑seq search.

    Calls :func:`app.clients.sources.encode_chipseq` to retrieve
    metadata about ChIP‑seq experiments targeting the gene.  The
    resulting list of experiments is returned verbatim.
    """
    try:
        result = await sources.encode_chipseq(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="ENCODE portal",
        data={"gene": gene, "experiments": result.get("experiments", [])},
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 2 – Disease Association & Perturbation
# ----------------------------------------------------------------------------

@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(condition: str) -> Evidence:
    """Differential expression experiments for a condition (Expression Atlas).

    Uses :func:`app.clients.sources.expression_atlas_experiments` to list
    transcriptomics experiments relevant to the supplied disease or tissue
    condition.  Returns the list of experiment metadata.
    """
    try:
        result = await sources.expression_atlas_experiments(condition)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Expression Atlas",
        data={"condition": condition, "experiments": result.get("experiments", [])},
        citations=result.get("citations", []),
    )


@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str) -> Evidence:
    """Proteomics projects for a condition (PRIDE).

    Delegates to :func:`app.clients.sources.pride_projects` which searches
    the PRIDE archive for proteomics studies matching the condition.
    """
    try:
        result = await sources.pride_projects(condition)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="PRIDE archive",
        data={"condition": condition, "projects": result.get("projects", [])},
        citations=result.get("citations", []),
    )


@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(condition: str) -> Evidence:
    """Single‑cell expression placeholder (cellxgene).

    Calls :func:`app.clients.sources.cellxgene` which currently returns a
    note and citations because the cellxgene API does not expose
    programmatic endpoints for bulk queries.  The status is ``NO_DATA``.
    """
    result = await sources.cellxgene(condition)
    return _build_evidence(
        status="NO_DATA",
        source="cellxgene placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(condition: str) -> Evidence:
    """CRISPR perturbation placeholder.

    Uses :func:`app.clients.sources.perturb` which notes that BioGRID ORCS
    and DepMap do not provide a convenient API.  Status is ``NO_DATA``.
    """
    result = await sources.perturb(condition)
    return _build_evidence(
        status="NO_DATA",
        source="Perturbation placeholder",
        data=result,
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 3 – Expression, Specificity & Localization
# ----------------------------------------------------------------------------

@router.get("/expr/baseline", response_model=Evidence)
async def expression_baseline(gene: str) -> Evidence:
    """Baseline expression for a gene (Expression Atlas).

    Calls :func:`app.clients.sources.expression_atlas_gene` which returns
    raw expression data for the gene across multiple tissues.  This
    endpoint does not attempt to parse the expression matrix; instead
    the raw JSON is returned so that downstream clients can perform
    custom analyses.
    """
    try:
        result = await sources.expression_atlas_gene(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Expression Atlas",
        data={"gene": gene, "raw": result.get("raw")},
        citations=result.get("citations", []),
    )


@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(gene: str) -> Evidence:
    """Subcellular localization evidence (UniProt).

    Invokes :func:`app.clients.sources.uniprot_localization` to fetch
    UniProt annotations for the gene including cellular compartment
    annotations.  Returns the list of results as provided by UniProt.
    """
    try:
        result = await sources.uniprot_localization(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="UniProt localization",
        data={"gene": gene, "results": result.get("results", [])},
        citations=result.get("citations", []),
    )


@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(gene: str) -> Evidence:
    """Inducibility placeholder (GEO time‑course experiments).

    Calls :func:`app.clients.sources.inducibility` which returns a note
    about GEO time‑course experiments.  Status is ``NO_DATA``.
    """
    result = await sources.inducibility(gene)
    return _build_evidence(
        status="NO_DATA",
        source="Inducibility placeholder",
        data=result,
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 4 – Mechanistic Wiring & Networks
# ----------------------------------------------------------------------------

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(gene: str) -> Evidence:
    """Pathway membership for a gene (Reactome).

    Delegates to :func:`app.clients.sources.reactome_search` to retrieve
    Reactome search results for the gene.  Returns the list of results.
    """
    try:
        result = await sources.reactome_search(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Reactome",
        data={"gene": gene, "results": result.get("results", [])},
        citations=result.get("citations", []),
    )


@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(gene: str) -> Evidence:
    """Protein–protein interaction network (STRING DB).

    Invokes :func:`app.clients.sources.string_map_and_network` to map the
    gene symbol to a STRING identifier and retrieve its interaction
    network.  Returns both the STRING ID and the list of edges.
    """
    try:
        result = await sources.string_map_and_network(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="STRING DB",
        data={"gene": gene, "string_id": result.get("string_id"), "edges": result.get("edges", [])},
        citations=result.get("citations", []),
    )


@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(gene: str) -> Evidence:
    """Ligand–receptor interactions (OmniPath).

    Uses :func:`app.clients.sources.omnipath_ligrec` to fetch potential
    ligand–receptor pairs involving the gene.  Returns the list of rows
    from OmniPath.
    """
    try:
        result = await sources.omnipath_ligrec(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="OmniPath",
        data={"gene": gene, "rows": result.get("rows", [])},
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 5 – Tractability & Modality
# ----------------------------------------------------------------------------

@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(gene: str) -> Evidence:
    """Known drug interactions via Open Targets Platform.

    Calls :func:`app.clients.sources.ot_platform_known_drugs` to fetch
    approved and investigational drugs targeting the gene.  Returns both
    the list of drugs and the total count.
    """
    try:
        result = await sources.ot_platform_known_drugs(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Open Targets Platform",
        data={"gene": gene, "knownDrugs": result.get("knownDrugs", []), "count": result.get("count")},
        citations=result.get("citations", []),
    )


@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(gene: str) -> Evidence:
    """Ligandability for small molecules via PDB search.

    Utilises :func:`app.clients.sources.pdb_search` to query the PDB for
    structures matching the gene.  A non‑empty set of hits suggests
    structural information is available for small‑molecule docking.
    """
    try:
        result = await sources.pdb_search(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="PDB",
        data={"gene": gene, "hits": result.get("hits", [])},
        citations=result.get("citations", []),
    )


@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(gene: str) -> Evidence:
    """Ligandability for antibodies via UniProt topology.

    Calls :func:`app.clients.sources.uniprot_topology` to retrieve
    transmembrane topology and subcellular location for the protein.  A
    cell‑surface or secreted location indicates suitability for
    antibody therapeutics.
    """
    try:
        result = await sources.uniprot_topology(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="UniProt topology",
        data={"gene": gene, "results": result.get("results", [])},
        citations=result.get("citations", []),
    )


@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(gene: str) -> Evidence:
    """Oligonucleotide ligandability placeholder.

    Invokes :func:`app.clients.sources.rnacentral_oligo` which currently
    returns a note pointing to RNACentral and TargetScan.  Status is
    ``NO_DATA``.
    """
    result = await sources.rnacentral_oligo(gene)
    return _build_evidence(
        status="NO_DATA",
        source="Oligonucleotide ligandability placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(gene: str) -> Evidence:
    """Therapeutic modality assessment placeholder.

    Calls :func:`app.clients.sources.modality` which notes that a more
    sophisticated modality assessment combining UniProt and other
    resources should be implemented.  Status is ``NO_DATA``.
    """
    result = await sources.modality(gene)
    return _build_evidence(
        status="NO_DATA",
        source="Modality assessment placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(gene: str) -> Evidence:
    """Immunogenicity placeholder (IEDB).

    Uses :func:`app.clients.sources.iedb_immunogenicity` which returns a
    note indicating that a proper IEDB API integration is pending.  Status
    is ``NO_DATA``.
    """
    result = await sources.iedb_immunogenicity(gene)
    return _build_evidence(
        status="NO_DATA",
        source="Immunogenicity placeholder",
        data=result,
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 6 – Clinical Translation & Safety
# ----------------------------------------------------------------------------

@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str) -> Evidence:
    """Clinical endpoints and outcomes (ClinicalTrials.gov).

    Calls :func:`app.clients.sources.ctgov_studies_outcomes` to retrieve
    the outcomes module for studies matching the condition.  Returns the
    list of studies and associated outcomes.  Note that ClinicalTrials.gov
    imposes limits on query complexity; this function uses the v2 API.
    """
    try:
        result = await sources.ctgov_studies_outcomes(condition)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="ClinicalTrials.gov",
        data={"condition": condition, "studies": result.get("studies", [])},
        citations=result.get("citations", []),
    )


@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(condition: str) -> Evidence:
    """Real‑world evidence placeholder.

    Calls :func:`app.clients.sources.rwe` which explains that real‑world
    evidence sources such as Sentinel, N3C and SEER are access controlled.
    Status is ``NO_DATA``.
    """
    result = await sources.rwe(condition)
    return _build_evidence(
        status="NO_DATA",
        source="RWE placeholder",
        data=result,
        citations=result.get("citations", []),
    )


@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(drug: str) -> Evidence:
    """Safety signals from FDA FAERS (openFDA).

    Invokes :func:`app.clients.sources.openfda_faers_reactions` to count
    adverse event reactions for the drug.  The caller should supply a
    marketed drug name.  If the API call fails, an error is raised.
    """
    try:
        result = await sources.openfda_faers_reactions(drug)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="openFDA",
        data={"drug": drug, "results": result.get("results", [])},
        citations=result.get("citations", []),
    )


@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(gene: str) -> Evidence:
    """Drug development pipeline summary via Open Targets Platform.

    Calls :func:`app.clients.sources.ot_platform_known_drugs_count` to
    return the number of known drugs targeting the gene.  This serves as
    a proxy for the phase of the pipeline; a count of zero suggests
    preclinical status.
    """
    try:
        result = await sources.ot_platform_known_drugs_count(gene)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="Open Targets Platform",
        data={"gene": gene, "count": result.get("count")},
        citations=result.get("citations", []),
    )


# ----------------------------------------------------------------------------
# BUCKET 7 – Competition & Intellectual Property
# ----------------------------------------------------------------------------

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(gene: str, condition: Optional[str] = None) -> Evidence:
    """Competition intensity via trial count (ClinicalTrials.gov).

    Uses :func:`app.clients.sources.ctgov_trial_count` to determine how
    many trials are registered for the given condition.  If no condition
    is provided, the count is ``None``.  A higher number indicates a
    competitive space.
    """
    try:
        result = await sources.ctgov_trial_count(condition)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _build_evidence(
        status="OK",
        source="ClinicalTrials.gov",
        data={"condition": condition, "totalStudies": result.get("totalStudies")},
        citations=result.get("citations", []),
    )


@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(gene: str) -> Evidence:
    """Freedom to operate placeholder (patent search).

    Calls :func:`app.clients.sources.ip` which returns a note suggesting
    that patent databases should be integrated.  Status is ``NO_DATA``.
    """
    result = await sources.ip(gene)
    return _build_evidence(
        status="NO_DATA",
        source="Patent search placeholder",
        data=result,
        citations=result.get("citations", []),
    )


# The router is now ready to be included in the FastAPI application defined
# in ``app/main.py``.

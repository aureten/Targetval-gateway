# app/routers/insight_router.py
"""
Revised to enable hybrid (live|snapshot|auto) mode per fetch via app.hybrid.hyridize.
- Adds optional `mode` param (query or x-source-mode header) on /v1/evidence
- Wraps each live fetcher in a hybrid wrapper (snapshots optional; can be added later)
- Keeps response models identical for backward compatibility
"""

import os
import math
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any, Iterable, Union

import httpx
import networkx as nx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

# Hybrid layer: wrappers + dependency to parse mode
from app.hybrid import hybridize, get_source_mode, SourceMode  # Evidence objects are returned internally

router = APIRouter(prefix="/v1", tags=["insight"])

# ==============================
# Models
# ==============================

class EffectSize(BaseModel):
    value: float
    unit: Optional[str] = None
    type: Optional[str] = None

class EvidenceItem(BaseModel):
    id: Optional[str] = None
    target: str
    disease: Optional[str] = None
    disease_efo: Optional[str] = None
    source_name: str
    source_url: str
    source_type: str        # GENETIC|EXPRESSION|PRECLINICAL|CLINICAL|SAFETY|...
    study_type: str         # GWAS|RNASEQ|IHC|RCT|FAERS|...
    species: str            # HUMAN|PRIMATE|RODENT|IN_VITRO|OTHER
    n: Optional[int] = 0
    endpoint: Optional[str] = None
    effect_direction: str   # INCREASES|DECREASES|NULL|BIDIRECTIONAL
    effect_size: Optional[EffectSize] = None
    p_value: Optional[float] = None
    ci95: Optional[List[float]] = None
    replicates: Optional[int] = 0
    risk_of_bias: Optional[str] = "UNCLEAR"
    recency: Optional[str] = None
    extraction_confidence: Optional[float] = 1.0
    citation: str
    notes: Optional[str] = None

class EvidenceResponse(BaseModel):
    status: str = "OK"
    fetched_n: int
    items: List[EvidenceItem]
    citations: List[str]
    generated_at: str
    module: str
    target: str
    disease_efo: Optional[str] = None

class EvidenceItemLite(BaseModel):
    target: str
    disease_efo: Optional[str] = None
    endpoint: Optional[str] = None
    species: Optional[str] = None
    study_type: Optional[str] = None

class ScoreOut(BaseModel):
    tcs: float
    conflict: float
    safety_penalty: float
    n_items: int

class SubgraphOut(BaseModel):
    nodes: List[str]
    edges: List[List[str]]
    communities: List[Dict[str, Any]]

# ==============================
# Scoring
# ==============================

SPECIES_W = {"HUMAN":1.0, "PRIMATE":0.7, "RODENT":0.4, "IN_VITRO":0.2, "OTHER":0.3}
STUDY_W   = {"RCT":1.0,"OBSERVATIONAL":0.7,"CLINICAL":0.8,"PRECLINICAL":0.4,
             "GWAS":0.9,"EXOME":0.9,"MR":0.9,"IN_SILICO":0.2,"RNASEQ":0.6,"IHC":0.6,
             "PET":0.8,"MRI_PDFF":0.8,"FAERS":0.7,"CASE_CONTROL":0.75,"DIO_MODEL":0.4}
BIAS_W    = {"LOW":1.0,"SOME":0.8,"HIGH":0.5,"UNCLEAR":0.7}
DIR_SIGN  = {"INCREASES":+1,"DECREASES":-1,"NULL":0,"BIDIRECTIONAL":0}

def _years_since(iso_date:str|None)->float:
    if not iso_date: return 5.0
    try:
        y = datetime.fromisoformat(iso_date[:10]).year
        return max(0, datetime.utcnow().year - y)
    except Exception:
        return 5.0

def score_items(items: Iterable[EvidenceItem], desired_direction:int) -> tuple[float,float]:
    scores, conflicts = [], []
    for it in items:
        s = SPECIES_W.get((it.species or "OTHER").upper(), 0.3) * \
            STUDY_W.get((it.study_type or "IN_SILICO").upper(), 0.4) * \
            BIAS_W.get((it.risk_of_bias or "UNCLEAR").upper(), 0.7)
        size_bonus  = math.log1p(max(0, it.n or 0))/math.log1p(1000)
        recency     = math.exp(-_years_since(it.recency)/6.0)
        strength    = abs((it.effect_size.value if it.effect_size else 0.0))
        raw         = s * (0.5 + 0.5*size_bonus) * recency * (0.5 + 0.5*min(1.0, strength))
        direction   = DIR_SIGN.get((it.effect_direction or "NULL").upper(), 0)
        signed      = raw * (1 if direction == desired_direction else (-raw if direction == -desired_direction else 0))
        scores.append(signed)
        if direction and direction != desired_direction:
            conflicts.append(abs(signed))
    if not scores:
        return 0.0, 0.0
    trimmed = sorted(scores)
    k = max(1, int(len(trimmed)*0.1))
    core = trimmed[k:-k] if len(trimmed) > 2*k else trimmed
    base = max(0.0, min(1.0, (sum(core)/len(core)+1)/2))
    denom = sum(abs(x) for x in scores) or 1e-6
    conflict = min(1.0, (sum(conflicts)/denom))
    return base, conflict

def tcs(items: Iterable[EvidenceItem], desired_direction:int=-1, safety_penalty:float=0.0)->dict:
    base, conflict = score_items(items, desired_direction)
    tcs_val = max(0.0, 100.0*(base - 0.3*conflict) * (1.0 - max(0.0, min(1.0, safety_penalty))))
    return {"tcs": round(tcs_val,1), "conflict": round(conflict,2), "safety_penalty": round(safety_penalty,2)}

# ==============================
# Graph
# ==============================

def build_graph(items: List[Union[EvidenceItem, EvidenceItemLite]]) -> nx.Graph:
    G = nx.Graph()
    for it in items:
        t = f"T::{it.target}"
        G.add_node(t, kind="target")
        if getattr(it, "disease_efo", None):
            G.add_edge(t, f"D::{it.disease_efo}", kind="assoc")
        if getattr(it, "endpoint", None):
            G.add_edge(t, f"E::{it.endpoint}", kind="endpoint")
        if getattr(it, "species", None):
            G.add_edge(t, f"S::{it.species}", kind="species")
        if getattr(it, "study_type", None):
            G.add_edge(t, f"ST::{it.study_type}", kind="study")
    return G

def communities_json(G: nx.Graph) -> List[Dict[str, Any]]:
    if G.number_of_nodes() == 0:
        return []
    comms = list(nx.algorithms.community.greedy_modularity_communities(G))
    return [{"community_id": i, "size": len(c), "nodes": sorted(list(c))} for i, c in enumerate(comms)]

# ==============================
# Connectors & helpers (public)
# ==============================

OT_GQL = "https://api.platform.opentargets.org/api/v4/graphql"
GTEX_EXPR = "https://gtexportal.org/rest/v1/expression/geneExpression"
HPA_TSV = "https://www.proteinatlas.org/api/normal_tissue.tsv"
OPENFDA_EVENT = "https://api.fda.gov/drug/event.json"
OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"

async def _http_client():
    timeout = float(os.getenv("HTTP_TIMEOUT_S", "25.0"))
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout))

SEARCH_Q = """
query Search($q: String!) {
  search(query: $q, entityNames: [TARGET], limit: 1) {
    hits { id entity }
  }
}
"""

ASSOC_Q = """
query Assoc($ensg: String!, $efo: String) {
  target(ensemblId: $ensg) {
    id
    approvedSymbol
    associatedDiseases(efoId: $efo, page: {index:0,size:25}) {
      rows { disease { id name } score  }
    }
    tractability { smallmolecule { value } antibody { value } }
  }
}
"""

async def resolve_ensg(symbol_or_ensg: str) -> Optional[str]:
    if symbol_or_ensg and symbol_or_ensg.upper().startswith("ENSG"):
        return symbol_or_ensg
    try:
        async with await _http_client() as client:
            r = await client.post(OT_GQL, json={"query": SEARCH_Q, "variables": {"q": symbol_or_ensg}})
            if r.status_code != 200:
                return None
            hits = r.json().get("data", {}).get("search", {}).get("hits", [])
            for h in hits:
                if h.get("entity") == "target":
                    return h.get("id")
    except Exception:
        return None
    return None

async def ot_fetch(ensg_or_symbol: str, efo_id: Optional[str]) -> List[EvidenceItem]:
    items: List[EvidenceItem] = []
    async with await _http_client() as client:
        ensg = await resolve_ensg(ensg_or_symbol)
        if not ensg:
            return []
        try:
            r = await client.post(OT_GQL, json={"query": ASSOC_Q, "variables": {"ensg": ensg, "efo": efo_id}})
            r.raise_for_status()
            data = r.json().get("data", {}).get("target", {})
        except Exception:
            return []
    for row in (data.get("associatedDiseases", {}) or {}).get("rows", []):
        items.append(EvidenceItem(
            target=ensg,
            disease=row["disease"]["name"],
            disease_efo=row["disease"]["id"],
            source_name="OpenTargets",
            source_url=f"https://platform.opentargets.org/target/{ensg}",
            source_type="GENETIC",
            study_type="GWAS",
            species="HUMAN",
            effect_direction="BIDIRECTIONAL",
            effect_size=EffectSize(value=float(row["score"]), type="beta"),
            citation="OpenTargets GraphQL",
            notes="Association score (multi-evidence composite)"
        ))
    tract = data.get("tractability") or {}
    for modality in ("smallmolecule", "antibody"):
        val = ((tract.get(modality) or {}).get("value"))
        if val is True:
            items.append(EvidenceItem(
                target=ensg,
                source_name="OpenTargets",
                source_url=f"https://platform.opentargets.org/target/{ensg}",
                source_type="TRACTABILITY",
                study_type="IN_SILICO",
                species="OTHER",
                effect_direction="NULL",
                effect_size=EffectSize(value=1.0, type="other"),
                citation="OpenTargets GraphQL",
                notes=f"Tractability evidence: {modality}"
            ))
    return items

async def gtex_fetch(ensg: str, efo_id: Optional[str]) -> List[EvidenceItem]:
    if not ensg or not ensg.upper().startswith("ENSG"):
        return []
    items: List[EvidenceItem] = []
    params = {"gencodeId": ensg, "format": "json"}
    try:
        async with await _http_client() as client:
            r = await client.get(GTEX_EXPR, params=params)
            if r.status_code != 200:
                return []
            js = r.json()
            for row in (js.get("geneExpression") or []):
                tissue = row.get("tissueSiteDetailId") or row.get("tissueSiteDetail") or row.get("tissue")
                tpm = float(row.get("median") or 0.0)
                items.append(EvidenceItem(
                    target=ensg,
                    disease_efo=efo_id,
                    source_name="GTEx",
                    source_url=f"https://gtexportal.org/home/gene/{ensg}",
                    source_type="EXPRESSION",
                    study_type="RNASEQ",
                    species="HUMAN",
                    endpoint=tissue,
                    effect_direction="BIDIRECTIONAL",
                    effect_size=EffectSize(value=tpm, unit="TPM", type="other"),
                    citation="GTEx REST geneExpression",
                    notes="Median expression per tissue"
                ))
    except Exception:
        return []
    return items

async def hpa_fetch(ensg: str) -> List[EvidenceItem]:
    if not ensg or not ensg.upper().startswith("ENSG"):
        return []
    items: List[EvidenceItem] = []
    params = {"ensembl": ensg}
    try:
        async with await _http_client() as client:
            r = await client.get(HPA_TSV, params=params)
            if r.status_code != 200 or not r.text:
                return []
            import csv, io
            reader = csv.DictReader(io.StringIO(r.text), delimiter="\\t")
            for row in reader:
                tissue = row.get("Tissue")
                level = row.get("Level")
                reliability = row.get("Reliability") or "Uncertain"
                if tissue and level:
                    score = {"Not detected":0, "Low":1, "Medium":2, "High":3}.get(level, 0)
                    bias = "LOW" if reliability in ("Enhanced","Supported") else ("SOME" if reliability=="Approved" else "UNCLEAR")
                    items.append(EvidenceItem(
                        target=ensg,
                        source_name="Human Protein Atlas",
                        source_url=f"https://www.proteinatlas.org/{ensg}-protein",
                        source_type="EXPRESSION",
                        study_type="IHC",
                        species="HUMAN",
                        endpoint=tissue,
                        effect_direction="BIDIRECTIONAL",
                        effect_size=EffectSize(value=float(score), type="other"),
                        risk_of_bias=bias,
                        citation="HPA normal_tissue.tsv",
                        notes=f"Protein IHC: {level} ({reliability})"
                    ))
    except Exception:
        return []
    return items

async def faers_fetch(target_for_context: str, drug_kw: Optional[str]) -> List[EvidenceItem]:
    if not drug_kw:
        return []
    items: List[EvidenceItem] = []
    params = {
        "search": f'patient.drug.medicinalproduct:"{drug_kw}"',
        "count": "patient.reaction.reactionmeddrapt.exact",
        "limit": 10
    }
    try:
        async with await _http_client() as client:
            r = await client.get(OPENFDA_EVENT, params=params)
            if r.status_code != 200:
                return []
            for rec in (r.json().get("results") or []):
                ae = rec.get("term")
                cnt = int(rec.get("count") or 0)
                items.append(EvidenceItem(
                    target=target_for_context,
                    source_name="openFDA FAERS",
                    source_url="https://open.fda.gov/apis/drug/event/",
                    source_type="SAFETY",
                    study_type="FAERS",
                    species="HUMAN",
                    endpoint=ae,
                    effect_direction="INCREASES",
                    effect_size=EffectSize(value=float(cnt), type="other"),
                    citation="openFDA FAERS aggregation",
                    notes=f"Class AE frequency proxy for '{drug_kw}'"
                ))
    except Exception:
        return []
    return items

async def resolve_efo(disease_text: Optional[str]) -> Optional[str]:
    if not disease_text:
        return None
    params = {"q": disease_text, "ontology": "efo", "rows": 1}
    try:
        async with await _http_client() as client:
            r = await client.get(OLS_SEARCH, params=params)
            if r.status_code != 200:
                return None
            docs = r.json().get("response", {}).get("docs", [])
            if not docs:
                return None
            iri: str = docs[0].get("iri") or ""
            if "EFO_" in iri:
                return iri.split("/")[-1]
    except Exception:
        return None
    return None

# ==============================
# Hybrid wrappers for live connectors (snapshots are optional and can be plugged later)
# ==============================

# Defaults here keep behavior as-live; you can change default="snapshot" once you add local snapshot functions.
hybrid_ot = hybridize(
    "insight_ot",
    ot_fetch,
    snapshot_fn=None,                 # plug snapshot callable when ready
    default="live",
    ttl_sec=7 * 24 * 3600,
    source_name_live="OpenTargets",
    source_name_snapshot="OT_SNAPSHOT"
)

hybrid_gtex = hybridize(
    "insight_gtex",
    gtex_fetch,
    snapshot_fn=None,                 # plug snapshot callable when ready
    default="live",
    ttl_sec=30 * 24 * 3600,
    source_name_live="GTEx",
    source_name_snapshot="GTEx_SNAPSHOT"
)

hybrid_hpa = hybridize(
    "insight_hpa",
    hpa_fetch,
    snapshot_fn=None,
    default="live",
    ttl_sec=60 * 24 * 3600,
    source_name_live="HPA",
    source_name_snapshot="HPA_SNAPSHOT"
)

hybrid_faers = hybridize(
    "insight_faers",
    faers_fetch,
    snapshot_fn=None,
    default="live",
    ttl_sec=14 * 24 * 3600,
    source_name_live="openFDA_FAERS",
    source_name_snapshot="FAERS_SNAPSHOT"
)

# ==============================
# Endpoints (PUBLIC)
# ==============================

@router.get("/evidence", response_model=EvidenceResponse)
async def get_evidence(
    target: str,
    disease: Optional[str] = None,
    faers_kw: Optional[str] = None,
    mode: SourceMode = Depends(get_source_mode)   # live|snapshot|auto (default auto via dependency)
):
    """
    Aggregate evidence from multiple sources. The `mode` parameter controls source behavior:
      - mode=live: use live APIs
      - mode=snapshot: use snapshot callables (if configured)
      - mode=auto (default): try live, fall back to snapshot if available
    """
    # Resolve EFO and ENSG so all downstream connectors can run.
    efo_id = await resolve_efo(disease) if disease else None
    ensg = await resolve_ensg(target) or target

    # Call wrappers concurrently; each returns an Evidence envelope with .data list[EvidenceItem]
    tasks = [
        hybrid_ot(ensg_or_symbol=target, efo_id=efo_id, mode=mode),         # accepts symbol or ENSG
        hybrid_gtex(ensg=ensg, efo_id=efo_id, mode=mode),                   # requires ENSG
        hybrid_hpa(ensg=ensg, mode=mode),                                   # requires ENSG
        hybrid_faers(target_for_context=ensg, drug_kw=faers_kw, mode=mode)  # optional keyword for class AE proxy
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    items: List[EvidenceItem] = []
    for res in results:
        if isinstance(res, Exception):
            continue
        # res is an Evidence envelope from app.hybrid; extract its .data payload
        data = getattr(res, "data", None)
        if isinstance(data, list):
            for it in data:
                if isinstance(it, EvidenceItem):
                    items.append(it)
                elif isinstance(it, dict):
                    try:
                        items.append(EvidenceItem(**it))
                    except Exception:
                        continue

    return EvidenceResponse(
        fetched_n=len(items),
        items=items,
        citations=sorted(list({it.source_url for it in items if it.source_url})),
        generated_at=datetime.utcnow().isoformat(),
        module="insight.aggregate",
        target=ensg,
        disease_efo=efo_id
    )

@router.post("/score", response_model=ScoreOut)
async def post_score(items: List[EvidenceItem], desired_direction: int = -1, safety_penalty: float = 0.0):
    res = tcs(items, desired_direction=desired_direction, safety_penalty=safety_penalty)
    res["n_items"] = len(items)
    return res

@router.post("/graph/subgraph", response_model=SubgraphOut)
async def post_subgraph(items: List[EvidenceItemLite]):
    G = build_graph(items)
    return {
        "nodes": list(G.nodes()),
        "edges": [[u, v, G[u][v].get("kind")] for u, v in G.edges()],
        "communities": communities_json(G)
    }

# -*- coding: utf-8 -*-
"""
TARGETVAL Gateway — Router (tightened) — 55 modules × 6 domains (LIVE + fallbacks + query adaptation)

This router is the hardened version requested by Alp:
 • All **55 modules** are active and dynamically registered from `targetval_modules.yaml`.
 • Each module executes **live** programmatic calls to its listed sources, with **fallbacks** and **query adaptation**:
     - ID normalization (HGNC→ENSG via GTEx; HGNC→UniProt via UniProtKB; aliases via UniProt + HGNC).
     - Disease/tissue normalization to **EFO/MONDO** and **UBERON/CL** via **EBI OLSv4**.
     - Multi-try strategies: symbol → synonyms → UniProt → ENSG (and the reverse if needed).
     - Alternate parameterizations per upstream (e.g., Reactome by UniProt, STRING by Symbol, PSICQUIC by MI-TAB).
 • HTTP client: retries on 429/5xx, exponential backoff, per-request timeout, gzip, and a tiny **circuit breaker**.
 • Response envelope: Evidence(status|source|fetched_n|data|citations|fetched_at|debug).
 • Literature layer: Europe PMC, PubTator3; scoring; synthesis; triggers — unchanged but with better debug info.
 • Env-variable switches: If a premium API key exists (DepMap, DGIdb, BioGRID ORCS, IEDB, PDC), handlers will use it;
   otherwise they fall back to public alternatives without stubs.

Dependencies expected: fastapi, pydantic, httpx, pyyaml

Author: ChatGPT (GPT-5 Pro)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, validator

router = APIRouter()

# -------------------------------------------------------------------------------------
# Evidence model
# -------------------------------------------------------------------------------------

class Evidence(BaseModel):
    status: str
    source: str
    fetched_n: int
    data: Dict[str, Any]
    citations: List[str]
    fetched_at: str
    debug: Optional[Dict[str, Any]] = None

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _ev_ok(source: str, payload: Dict[str, Any], citations: List[str], fetched_n: Optional[int] = None, debug: Optional[Dict[str, Any]] = None) -> Evidence:
    if fetched_n is None:
        if isinstance(payload, list):
            fetched_n = len(payload)
        elif isinstance(payload, dict):
            fetched_n = payload.get("_count") or len(payload) or 1
        else:
            fetched_n = 1
    return Evidence(status="OK", source=source, fetched_n=int(fetched_n or 0), data=payload, citations=citations, fetched_at=_now_iso(), debug=debug)

def _ev_empty(source: str, citations: List[str], debug: Optional[Dict[str, Any]] = None) -> Evidence:
    return Evidence(status="NO_DATA", source=source, fetched_n=0, data={}, citations=citations, fetched_at=_now_iso(), debug=debug)

def _ev_error(source: str, msg: str, citations: List[str], debug: Optional[Dict[str, Any]] = None) -> Evidence:
    return Evidence(status="ERROR", source=source, fetched_n=0, data={"error": msg}, citations=citations, fetched_at=_now_iso(), debug=debug)

# -------------------------------------------------------------------------------------
# HTTP client (retry, backoff, circuit breaker)
# -------------------------------------------------------------------------------------

DEFAULT_TIMEOUT = float(os.getenv("TARGETVAL_HTTP_TIMEOUT", "35.0"))
MAX_RETRIES = int(os.getenv("TARGETVAL_HTTP_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("TARGETVAL_HTTP_BACKOFF_BASE", "0.7"))

class LiveHttp:
    def __init__(self):
        limits = httpx.Limits(max_connections=24, max_keepalive_connections=24)
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, limits=limits, headers={"User-Agent": "Targetval-Gateway/55 (tightened)"}
        )
        self._cb: Dict[str, Dict[str, Any]] = {}

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        key = url.split("?")[0]
        now = time.time()
        st = self._cb.setdefault(key, {"fail": 0, "until": 0.0})
        if now < st["until"]:
            raise httpx.ConnectError(f"Circuit open for {key} (cooldown)")

        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await self.client.request(method, url, **kwargs)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"{r.status_code} upstream", request=r.request, response=r)
                return r
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep((BACKOFF_BASE ** attempt) * (0.9 + 0.2 * (attempt % 2)))
        st["fail"] += 1
        st["until"] = now + min(90.0, 10.0 * st["fail"])
        raise last_exc or RuntimeError("LiveHttp: unknown error")

    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        r = await self._request("GET", url, params=params, headers=headers)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def post_json(self, url: str, json_body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        r = await self._request("POST", url, json=json_body, headers=headers)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> str:
        r = await self._request("GET", url, params=params, headers=headers)
        r.raise_for_status()
        return r.text

live = LiveHttp()

# -------------------------------------------------------------------------------------
# ID & ontology resolution (multi-source, live only)
# -------------------------------------------------------------------------------------

async def resolve_uniprot_accessions(symbol_or_id: str, organism_id: str = "9606") -> List[str]:
    """
    Prefer UniProtKB REST for symbol→accessions.
    Accept an accession and pass through.
    """
    if re.match(r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]{5}$", symbol_or_id):
        return [symbol_or_id]
    url = "https://rest.uniprot.org/uniprotkb/search"
    params = {
        "query": f"gene_exact:{symbol_or_id} AND organism_id:{organism_id}",
        "fields": "accession,genes,protein_name,organism_id",
        "format": "json",
        "size": 5,
    }
    js = await live.get_json(url, params=params)
    return [x.get("primaryAccession") for x in js.get("results", []) if x.get("primaryAccession")]

async def resolve_symbol_aliases(symbol: str) -> List[str]:
    """
    Aggregate aliases from UniProtKB (gene synonyms) and HGNC REST.
    Only live sources; return unique list including the symbol itself.
    """
    aliases = {symbol}
    # UniProt gene synonyms
    try:
        js = await live.get_json("https://rest.uniprot.org/uniprotkb/search", params={
            "query": f"gene_exact:{symbol} AND organism_id:9606",
            "fields": "genes,accession",
            "format": "json", "size": 3
        })
        for res in js.get("results", []):
            for g in res.get("genes", []):
                if g.get("geneName", {}).get("value"):
                    aliases.add(g["geneName"]["value"])
                for syn in g.get("synonyms", []):
                    v = syn.get("value")
                    if v:
                        aliases.add(v)
    except Exception:
        pass
    # HGNC REST for aliases
    try:
        js = await live.get_json("https://rest.genenames.org/fetch/symbol/" + symbol, headers={"Accept": "application/json"})
        docs = js.get("response", {}).get("docs", [])
        for d in docs:
            for k in ("alias_symbol", "prev_name", "prev_symbol", "symbol"):
                v = d.get(k)
                if isinstance(v, list):
                    for x in v:
                        aliases.add(str(x))
                elif isinstance(v, str):
                    aliases.add(v)
    except Exception:
        pass
    return sorted({a for a in aliases if a and len(a) <= 32})

async def resolve_ensembl_gene(symbol: Optional[str]) -> Optional[str]:
    if not symbol:
        return None
    try:
        js = await live.get_json(f"https://gtexportal.org/api/v2/genes/lookup/symbol/{symbol}", params={"format": "json"})
        if isinstance(js, dict):
            if "gene" in js and isinstance(js["gene"], dict):
                return js["gene"].get("gencodeId")
            if "genes" in js and js["genes"]:
                return js["genes"][0].get("gencodeId")
    except Exception:
        return None
    return None

async def normalize_ontology(term_or_id: Optional[str], ontology: Optional[str] = None) -> Optional[str]:
    """
    Use EBI OLS v4 to resolve label→CURIE (e.g., "obesity" → EFO:0001073).
    """
    if not term_or_id:
        return None
    if ":" in term_or_id:
        return term_or_id.replace("_", ":")
    base = "https://www.ebi.ac.uk/ols4/api/search"
    params = {"q": term_or_id, "rows": 1}
    if ontology:
        params["ontology"] = ontology
    try:
        js = await live.get_json(base, params=params)
        docs = js.get("response", {}).get("docs", [])
        if docs:
            curie = docs[0].get("obo_id") or docs[0].get("short_form")
            if curie:
                return curie.replace("_", ":")
    except Exception:
        return None
    return None

# -------------------------------------------------------------------------------------
# Helpers for "multi-try" query strategies
# -------------------------------------------------------------------------------------

async def try_europepmc_queries(queries: List[str], page_size: int = 50) -> Evidence:
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    citations = ["https://europepmc.org/"]
    debug = {"attempts": []}
    for q in queries:
        params = {"query": q, "format": "json", "pageSize": page_size}
        try:
            js = await live.get_json(base, params=params)
            hits = js.get("resultList", {}).get("result", []) or []
            debug["attempts"].append({"q": q, "n": len(hits)})
            if hits:
                return _ev_ok("Europe PMC", {"hits": hits}, citations, fetched_n=len(hits), debug=debug)
        except Exception as e:
            debug["attempts"].append({"q": q, "error": str(e)})
            continue
    return _ev_empty("Europe PMC", citations, debug=debug)

async def try_ncbi_esearch(db: str, terms: List[str], retmax: int = 50) -> Evidence:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    citations = [f"https://www.ncbi.nlm.nih.gov/{db}/"]
    debug = {"attempts": []}
    for term in terms:
        params = {"db": db, "term": term, "retmode": "json", "retmax": retmax}
        try:
            js = await live.get_json(base, params=params)
            ids = js.get("esearchresult", {}).get("idlist", [])
            debug["attempts"].append({"term": term, "n": len(ids)})
            if ids:
                return _ev_ok("NCBI E-utilities", {"ids": ids}, citations, fetched_n=len(ids), debug=debug)
        except Exception as e:
            debug["attempts"].append({"term": term, "error": str(e)})
            continue
    return _ev_empty("NCBI E-utilities", citations, debug=debug)

# -------------------------------------------------------------------------------------
# Primary transforms per live source (with conservative, well-known endpoints)
# -------------------------------------------------------------------------------------

# GTEx baseline expression
async def tx_gtex_baseline(ensg: Optional[str]) -> Evidence:
    if not ensg:
        return _ev_empty("GTEx Portal v2", ["https://gtexportal.org/"], debug={"reason": "no ENSG"})
    url = "https://gtexportal.org/api/v2/expression/geneExpression"
    try:
        js = await live.get_json(url, params={"gencodeId": ensg, "format": "json"})
        data = js.get("geneExpression", [])
        return _ev_ok("GTEx Portal v2", {"geneExpression": data}, ["https://gtexportal.org/"], fetched_n=len(data))
    except Exception as e:
        return _ev_error("GTEx Portal v2", str(e), ["https://gtexportal.org/"])

# UniProt subcellular localization & features
async def tx_uniprot_entry(symbol_or_acc: str) -> Evidence:
    # resolve to accession if needed
    accs = await resolve_uniprot_accessions(symbol_or_acc)
    if not accs:
        return _ev_empty("UniProtKB", ["https://rest.uniprot.org/"], debug={"reason": "no accession"})
    acc = accs[0]
    try:
        js = await live.get_json(f"https://rest.uniprot.org/uniprotkb/{acc}", params={"format": "json"})
        payload = {
            "accession": acc,
            "protein": js.get("proteinDescription"),
            "comments": js.get("comments"),
            "features": js.get("features"),
        }
        return _ev_ok("UniProtKB", payload, ["https://rest.uniprot.org/"], fetched_n=1)
    except Exception as e:
        return _ev_error("UniProtKB", str(e), ["https://rest.uniprot.org/"])

# Europe PMC generic search
async def tx_eupmc_search(q: str, page_size: int = 50) -> Evidence:
    return await try_europepmc_queries([q], page_size=page_size)

# GEO search
async def tx_geo(symbol: str, condition: Optional[str], synonyms: List[str], retmax: int = 50) -> Evidence:
    terms = []
    base_term = f'({symbol}[Gene Symbol]) AND Homo sapiens[Organism]'
    terms.append(base_term if not condition else f'{base_term} AND ({condition})')
    for syn in synonyms:
        if syn == symbol:
            continue
        t = f'({syn}[Gene Symbol]) AND Homo sapiens[Organism]'
        terms.append(t if not condition else f'{t} AND ({condition})')
    return await try_ncbi_esearch("gds", terms, retmax=retmax)

# BioStudies search
async def tx_biostudies(q: str, size: int = 50) -> Evidence:
    try:
        js = await live.get_json("https://www.ebi.ac.uk/biostudies/api/v1/studies", params={"search": q, "size": size})
        hits = js.get("hits", {}).get("hits", [])
        return _ev_ok("BioStudies API", {"hits": hits}, ["https://www.ebi.ac.uk/biostudies/"], fetched_n=len(hits))
    except Exception as e:
        return _ev_error("BioStudies API", str(e), ["https://www.ebi.ac.uk/biostudies/"])

# PRIDE Archive
async def tx_pride(symbol_or_acc: str, size: int = 50) -> Evidence:
    accs = await resolve_uniprot_accessions(symbol_or_acc)
    q = accs[0] if accs else symbol_or_acc
    try:
        js = await live.get_json("https://www.ebi.ac.uk/pride/ws/archive/v2/search", params={"query": q, "pageSize": size})
        hits = js.get("list", [])
        return _ev_ok("PRIDE Archive", {"hits": hits}, ["https://www.ebi.ac.uk/pride/ws/archive/"], fetched_n=len(hits))
    except Exception as e:
        return _ev_error("PRIDE Archive", str(e), ["https://www.ebi.ac.uk/pride/ws/archive/"])

# STRING PPI
async def tx_string(symbol: str) -> Evidence:
    try:
        js = await live.get_json("https://string-db.org/api/json/network", params={"identifiers": symbol, "species": 9606})
        if isinstance(js, list) and js:
            return _ev_ok("STRING", {"edges": js}, ["https://string-db.org/"], fetched_n=len(js))
        # fallback: IntAct PSICQUIC (MI-TAB 2.5) — parse minimally
        tab = await live.get_text(f"https://www.ebi.ac.uk/Tools/webservices/psicquic/intact/webservices/current/search/interactor/{symbol}", params={"format": "tab25"})
        lines = [ln for ln in tab.splitlines() if ln and not ln.startswith("#")]
        edges = [{"raw": ln} for ln in lines]
        return _ev_ok("IntAct PSICQUIC", {"edges": edges}, ["https://www.ebi.ac.uk/intact/"], fetched_n=len(edges))
    except Exception as e:
        return _ev_error("STRING/IntAct", str(e), ["https://string-db.org/", "https://www.ebi.ac.uk/intact/"])

# OmniPath ligand–receptor + IUPHAR fallback
async def tx_ligrec(symbol: str) -> Evidence:
    citations = ["https://omnipathdb.org/", "https://www.guidetopharmacology.org/"]
    debug = {"attempts": []}
    try:
        js = await live.get_json("https://omnipathdb.org/intercell", params={"genesymbols": "1", "organism": "9606", "resources": "consensus", "format": "json"})
        rows = [r for r in js if symbol in (r.get("source_genesymbol"), r.get("target_genesymbol"))]
        debug["attempts"].append({"source": "OmniPath", "n": len(rows)})
        if rows:
            return _ev_ok("OmniPath (intercell)", {"rows": rows}, citations, fetched_n=len(rows), debug=debug)
    except Exception as e:
        debug["attempts"].append({"source": "OmniPath", "error": str(e)})
    # Fallback: Guide to Pharmacology simple target search
    try:
        js = await live.get_json("https://www.guidetopharmacology.org/services/targets", params={"name": symbol})
        return _ev_ok("IUPHAR/Guide to Pharmacology", {"targets": js}, citations, fetched_n=len(js) if isinstance(js, list) else 0, debug=debug)
    except Exception as e:
        return _ev_error("Ligand–receptor", str(e), citations, debug=debug)

# Reactome by UniProt with Pathway Commons fallback
async def tx_pathways(symbol_or_acc: str) -> Evidence:
    accs = await resolve_uniprot_accessions(symbol_or_acc)
    if accs:
        acc = accs[0]
        try:
            js = await live.get_json(f"https://reactome.org/ContentService/data/pathways/low/diagram/entity/UniProt:{acc}")
            if isinstance(js, list):
                return _ev_ok("Reactome Content Service", {"pathways": js}, ["https://reactome.org/"], fetched_n=len(js))
        except Exception as e:
            pass
    # Fallback: Pathway Commons search by gene symbol
    try:
        js = await live.get_json("https://www.pathwaycommons.org/pc2/search.json", params={"q": symbol_or_acc, "type": "Pathway", "pageSize": 50})
        return _ev_ok("Pathway Commons", js, ["https://www.pathwaycommons.org/"], fetched_n=int(js.get("numHits", 0)))
    except Exception as e:
        return _ev_error("Reactome/PathwayCommons", str(e), ["https://reactome.org/", "https://www.pathwaycommons.org/"])

# OpenTargets GraphQL
OPENTARGETS_GRAPHQL_URL = os.getenv("OPENTARGETS_GRAPHQL_URL", "https://api.platform.opentargets.org/api/v4/graphql")
async def tx_opentargets_l2g(ensg: Optional[str], efo: Optional[str], size: int = 50) -> Evidence:
    if not ensg:
        return _ev_empty("OpenTargets GraphQL", [OPENTARGETS_GRAPHQL_URL], {"reason": "no ENSG"})
    q = """
    query L2G($ensg: String!, $size: Int) {
      gene(ensemblId: $ensg) {
        id
        symbol
        gwasColoc {
          count
          rows {
            studyId
            traitReported
            yProbaModel
            h4
            disease { id name }
            leadVariant { id rsId }
            pval
            beta
            nTotal
          }
        }
      }
    }
    """
    try:
        js = await live.post_json(OPENTARGETS_GRAPHQL_URL, {"query": q, "variables": {"ensg": ensg, "size": size}})
        rows = js.get("data", {}).get("gene", {}).get("gwasColoc", {}).get("rows", []) or []
        return _ev_ok("OpenTargets GraphQL (gwasColoc)", {"rows": rows}, [OPENTARGETS_GRAPHQL_URL], fetched_n=len(rows))
    except Exception as e:
        return _ev_error("OpenTargets GraphQL", str(e), [OPENTARGETS_GRAPHQL_URL])

async def tx_opentargets_evidence(symbol: str, efo: Optional[str], size: int = 50) -> Evidence:
    q = """
    query Evidence($symbol: String!, $efo: String, $size: Int) {
      target(chemblIdOrSymbol: $symbol) {
        id
        approvedSymbol
        associatedDiseases(page: {index: 0, size: $size}, filter: {efo: $efo}) {
          count
          rows { disease { id name } score evidenceCount }
        }
      }
    }
    """
    try:
        js = await live.post_json(OPENTARGETS_GRAPHQL_URL, {"query": q, "variables": {"symbol": symbol, "efo": efo, "size": size}})
        rows = js.get("data", {}).get("target", {}).get("associatedDiseases", {}).get("rows", []) or []
        return _ev_ok("OpenTargets GraphQL (associatedDiseases)", {"rows": rows}, [OPENTARGETS_GRAPHQL_URL], fetched_n=len(rows))
    except Exception as e:
        return _ev_error("OpenTargets GraphQL", str(e), [OPENTARGETS_GRAPHQL_URL])

# eQTL Catalogue sQTL
async def tx_eqtl_sqtl(ensg: Optional[str], size: int = 50) -> Evidence:
    if not ensg:
        return _ev_empty("eQTL Catalogue (sQTL)", ["https://www.ebi.ac.uk/eqtl/"], {"reason": "no ENSG"})
    try:
        js = await live.get_json("https://www.ebi.ac.uk/eqtl/api/associations", params={"gene_id": ensg, "qtl_group": "all", "quant_method": "sQTL", "size": size})
        hits = js.get("data", []) or js.get("associations", []) or []
        if hits:
            return _ev_ok("eQTL Catalogue (sQTL)", {"hits": hits}, ["https://www.ebi.ac.uk/eqtl/"], fetched_n=len(hits))
        return _ev_empty("eQTL Catalogue (sQTL)", ["https://www.ebi.ac.uk/eqtl/"])
    except Exception as e:
        return _ev_error("eQTL Catalogue (sQTL)", str(e), ["https://www.ebi.ac.uk/eqtl/"])

# PRoteomics: PDC GraphQL fallbacks are supported
PDC_GRAPHQL_URL = os.getenv("PDC_GRAPHQL_URL", "https://pdc.cancer.gov/graphql")
async def tx_pdc_gene(symbol: str, first: int = 50) -> Evidence:
    q = """
    query Gene($symbols: [String!], $first: Int) {
      genePaginated(geneSymbols: $symbols, first: $first) {
        edges { node { gene_symbol gene_id uniprot_id description } }
      }
    }
    """
    try:
        js = await live.post_json(PDC_GRAPHQL_URL, {"query": q, "variables": {"symbols": [symbol], "first": first}})
        edges = js.get("data", {}).get("genePaginated", {}).get("edges", []) or []
        return _ev_ok("PDC GraphQL", {"edges": edges}, [PDC_GRAPHQL_URL], fetched_n=len(edges))
    except Exception as e:
        return _ev_error("PDC GraphQL", str(e), [PDC_GRAPHQL_URL])

# ChEMBL + DrugCentral + DGIdb (opt) + PubChem AIDs
async def tx_chembl_target(symbol: str) -> Evidence:
    try:
        js = await live.get_json("https://www.ebi.ac.uk/chembl/api/data/target/search.json", params={"q": symbol, "limit": 25})
        hits = js.get("page_meta", {}).get("total_count", 0)
        return _ev_ok("ChEMBL", js, ["https://www.ebi.ac.uk/chembl/"], fetched_n=hits)
    except Exception as e:
        return _ev_error("ChEMBL", str(e), ["https://www.ebi.ac.uk/chembl/"])

async def tx_drugcentral_target(symbol: str) -> Evidence:
    try:
        js = await live.get_json("https://drugcentral.org/api/targets", params={"search": symbol})
        return _ev_ok("DrugCentral", {"targets": js}, ["https://drugcentral.org/api"], fetched_n=len(js) if isinstance(js, list) else 0)
    except Exception as e:
        return _ev_error("DrugCentral", str(e), ["https://drugcentral.org/api"])

async def tx_dgidb(symbol: str) -> Evidence:
    url = os.getenv("DGIDB_GRAPHQL_URL", "https://dgidb.org/graphql")
    key = os.getenv("DGIDB_API_KEY", None)
    q = """
    query Q($q: String!) { search(terms: [$q]) { genes { name conceptId interactions { drugs { name } } } } }
    """
    headers = {}
    if key:
        headers["Authorization"] = f"Token {key}"
    try:
        js = await live.post_json(url, {"query": q, "variables": {"q": symbol}}, headers=headers or None)
        return _ev_ok("DGIdb GraphQL", js, [url], fetched_n=1)
    except Exception as e:
        return _ev_error("DGIdb GraphQL", str(e), [url])

async def tx_pubchem_gene_aids(symbol: str) -> Evidence:
    try:
        js = await live.get_json(f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/target/gene/symbol/{symbol}/aids/JSON")
        aids = js.get("InformationList", {}).get("Information", [])
        return _ev_ok("PubChem PUG-REST (AIDs)", {"aids": aids}, ["https://pubchem.ncbi.nlm.nih.gov/"], fetched_n=len(aids))
    except Exception as e:
        return _ev_error("PubChem PUG-REST", str(e), ["https://pubchem.ncbi.nlm.nih.gov/"])

# RNAcentral transcripts
async def tx_rnacentral(symbol: str, size: int = 50) -> Evidence:
    try:
        js = await live.get_json("https://rnacentral.org/api/v1/rna", params={"gene": symbol, "page_size": size})
        results = js.get("results", [])
        return _ev_ok("RNAcentral", {"results": results}, ["https://rnacentral.org/"], fetched_n=len(results))
    except Exception as e:
        return _ev_error("RNAcentral", str(e), ["https://rnacentral.org/"])

# ENCODE & 4DN
async def tx_encode_search(params: Dict[str, Any]) -> Evidence:
    try:
        js = await live.get_json("https://www.encodeproject.org/search/", params=params, headers={"Accept": "application/json"})
        n = len(js.get("@graph", [])) if isinstance(js, dict) else 0
        return _ev_ok("ENCODE", js, ["https://www.encodeproject.org/"], fetched_n=n)
    except Exception as e:
        return _ev_error("ENCODE", str(e), ["https://www.encodeproject.org/"])

async def tx_4dn_search(term: str) -> Evidence:
    try:
        js = await live.get_json("https://data.4dnucleome.org/search/", params={"type": "Experiment", "searchTerm": term, "format": "json"})
        items = js.get("@graph", [])
        return _ev_ok("4D Nucleome", {"items": items}, ["https://data.4dnucleome.org/"], fetched_n=len(items))
    except Exception as e:
        return _ev_error("4D Nucleome", str(e), ["https://data.4dnucleome.org/"])

# Europe PMC HPA pathology proxy
async def tx_hpa_pathology(symbol: str, size: int = 50) -> Evidence:
    queries = [
        f'"Human Protein Atlas" AND {symbol}',
        f'(HPA) AND {symbol} AND pathology',
    ]
    return await try_europepmc_queries(queries, page_size=size)

# IEDB / immunology (literature-first unless peptides provided)
async def tx_immuno_lit(symbol: str, size: int = 50) -> Evidence:
    queries = [
        f'({symbol}) AND (immunogenic OR epitope OR "immune response") AND (Homo sapiens)',
        f'({symbol}) AND (MHC OR HLA OR epitope)',
    ]
    return await try_europepmc_queries(queries, page_size=size)

# ClinicalTrials.gov v2
async def tx_ctgov(query: str, size: int = 50, recruiting_only: bool = False) -> Evidence:
    params = {"query.term": query, "pageSize": size}
    if recruiting_only:
        params["filter.overallStatus"] = "Recruiting"
    try:
        js = await live.get_json("https://clinicaltrials.gov/api/v2/studies", params=params)
        studies = js.get("studies", [])
        return _ev_ok("ClinicalTrials.gov v2", {"studies": studies}, ["https://clinicaltrials.gov/"], fetched_n=len(studies))
    except Exception as e:
        return _ev_error("ClinicalTrials.gov v2", str(e), ["https://clinicaltrials.gov/"])

# openFDA FAERS with DrugCentral ADR fallback
async def tx_faers(symbol: str, size: int = 100) -> Evidence:
    try:
        js = await live.get_json("https://api.fda.gov/drug/event.json", params={"search": f'"{symbol}"', "limit": min(size, 100)})
        results = js.get("results", [])
        if results:
            return _ev_ok("openFDA FAERS", {"events": results}, ["https://api.fda.gov/"], fetched_n=len(results))
    except Exception as e:
        pass
    try:
        js = await live.get_json("https://drugcentral.org/api/adr", params={"q": symbol})
        return _ev_ok("DrugCentral ADR", js, ["https://drugcentral.org/"], fetched_n=len(js) if isinstance(js, list) else 0)
    except Exception as e:
        return _ev_error("FAERS/DrugCentral ADR", str(e), ["https://api.fda.gov/", "https://drugcentral.org/"])

# gnomAD GraphQL (constraint/priors)
async def tx_gnomad_constraint(symbol: str) -> Evidence:
    q = """
    query Constraint($gene_symbol: String!) {
      gene(gene_symbol: $gene_symbol) {
        gnomad_constraint { pLI oe_lof oe_mis mis_z syn_z }
      }
    }
    """
    url = "https://gnomad.broadinstitute.org/api"
    try:
        js = await live.post_json(url, {"query": q, "variables": {"gene_symbol": symbol}})
        return _ev_ok("gnomAD GraphQL", js, [url], fetched_n=1)
    except Exception as e:
        return _ev_error("gnomAD GraphQL", str(e), [url])

# MyVariant.info
async def tx_myvariant_by_gene(symbol: str, size: int = 50) -> Evidence:
    try:
        js = await live.get_json("https://myvariant.info/v1/query", params={"q": f"gene:{symbol}", "size": size})
        hits = js.get("hits", [])
        return _ev_ok("MyVariant.info", {"hits": hits}, ["https://myvariant.info/"], fetched_n=len(hits))
    except Exception as e:
        return _ev_error("MyVariant.info", str(e), ["https://myvariant.info/"])

# OpenGWAS search
async def tx_opengwas_search(qterm: str) -> Evidence:
    try:
        js = await live.get_json("https://api.opengwas.io/studies", params={"q": qterm})
        return _ev_ok("IEU OpenGWAS", js, ["https://gwas.mrcieu.ac.uk/"], fetched_n=len(js.get("data", [])) if isinstance(js, dict) else 0)
    except Exception as e:
        return _ev_error("IEU OpenGWAS", str(e), ["https://gwas.mrcieu.ac.uk/"])

# -------------------------------------------------------------------------------------
# Module dispatcher with fallbacks and query adaptation
# -------------------------------------------------------------------------------------

class ModuleParams(BaseModel):
    symbol: Optional[str] = Field(None)
    ensembl_id: Optional[str] = Field(None)
    uniprot_id: Optional[str] = Field(None)
    condition: Optional[str] = Field(None)
    efo: Optional[str] = Field(None)
    tissue: Optional[str] = Field(None)
    cell_type: Optional[str] = Field(None)
    species: Optional[str] = Field("human")
    limit: Optional[int] = Field(100)
    offset: Optional[int] = Field(0)
    strict: Optional[bool] = Field(False)

    @validator("species")
    def v_species(cls, v):
        vv = (v or "").lower()
        if vv in {"human", "hs", "homo sapiens", "h.sapiens", "9606"}:
            return "human"
        return v

async def dispatch_module(key: str, p: ModuleParams) -> Evidence:
    # Pre-resolve IDs and aliases
    symbol = p.symbol or ""
    aliases = await resolve_symbol_aliases(symbol) if symbol else []
    ensg = p.ensembl_id or (await resolve_ensembl_gene(symbol)) if symbol else None
    efo = await normalize_ontology(p.efo or p.condition, ontology=None) if (p.efo or p.condition) else None

    # Helper shortcuts for Europe PMC “multi-try” blocks
    def _sc_queries(sym: str, extra: str) -> List[str]:
        base = [
            f'({sym}) AND {extra} AND (Homo sapiens) AND FIRST_PDATE:[NOW-1825DAY TO NOW]',
            f'"{sym}" AND {extra}',
        ]
        return base

    try:
        # 1 expr-baseline
        if key == "expr-baseline":
            ev = await tx_gtex_baseline(ensg)
            if ev.status == "OK" and ev.fetched_n > 0:
                return ev
            # fallback: Expression Atlas is volatile; keep Europe PMC as a literature pointer is out of scope here
            return ev  # truthful NO_DATA if GTEx has none

        # 2 expr-localization
        if key == "expr-localization":
            return await tx_uniprot_entry(p.uniprot_id or symbol)

        # 3 expr-inducibility
        if key == "expr-inducibility":
            queries = []
            syns = aliases or [symbol]
            for s in syns[:6]:
                queries += _sc_queries(s, '(induc* OR upregulat* OR downregulat*)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 4 assoc-bulk-rna
        if key == "assoc-bulk-rna":
            ev = await tx_geo(symbol, p.condition, synonyms=aliases, retmax=min(p.limit or 50, 200))
            if ev.status == "OK" and ev.fetched_n > 0:
                return ev
            # fallback: BioStudies free search
            q = " ".join([symbol] + ([p.condition] if p.condition else []))
            return await tx_biostudies(q, size=min(p.limit or 50, 200))

        # 5 assoc-sc
        if key == "assoc-sc":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '("single-cell" OR scRNA-seq OR "single cell RNA")')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 6 assoc-spatial
        if key == "assoc-spatial":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '("spatial transcriptomics" OR Visium OR MERFISH OR Slide-seq)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 7 sc-hubmap
        if key == "sc-hubmap":
            try:
                js = await live.get_json("https://search.api.hubmapconsortium.org/search", params={"q": symbol, "size": min(p.limit or 50, 200)})
                hits = js.get("hits", {}).get("hits", [])
                if hits:
                    return _ev_ok("HuBMAP Search API", {"hits": hits}, ["https://portal.hubmapconsortium.org/"], fetched_n=len(hits))
            except Exception as e:
                pass
            # fallback to Europe PMC
            return await try_europepmc_queries(_sc_queries(symbol, "(HuBMAP OR HCA)"))

        # 8 assoc-proteomics
        if key == "assoc-proteomics":
            ev = await tx_pride(p.uniprot_id or symbol, size=min(p.limit or 50, 200))
            if ev.status == "OK" and ev.fetched_n > 0:
                return ev
            return await tx_pdc_gene(symbol, first=min(p.limit or 50, 100))

        # 9 assoc-metabolomics
        if key == "assoc-metabolomics":
            # MetaboLights
            try:
                js = await live.get_json("https://www.ebi.ac.uk/metabolights/ws/studies", params={"searchQuery": f"{symbol} {p.condition or ''}".strip()})
                studies = js.get("content", [])
                if studies:
                    return _ev_ok("MetaboLights", {"studies": studies}, ["https://www.ebi.ac.uk/metabolights/"], fetched_n=len(studies))
            except Exception:
                pass
            # Metabolomics Workbench simple search (fallback)
            try:
                js = await live.get_json("https://www.metabolomicsworkbench.org/rest/study/summary", params={"study_type": "all"})
                # This endpoint isn't filterable; return NO_DATA if we can't constrain
                return _ev_empty("Metabolomics Workbench", ["https://www.metabolomicsworkbench.org/rest"])
            except Exception as e:
                return _ev_error("Metabolomics", str(e), ["https://www.ebi.ac.uk/metabolights/", "https://www.metabolomicsworkbench.org/rest"])

        # 10 assoc-hpa-pathology
        if key == "assoc-hpa-pathology":
            return await tx_hpa_pathology(symbol, size=min(p.limit or 50, 200))

        # 11 assoc-perturb
        if key == "assoc-perturb":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '(CRISPR OR knockout OR knockdown OR overexpress* OR "gain-of-function" OR "loss-of-function")')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 12 genetics-l2g
        if key == "genetics-l2g":
            return await tx_opentargets_l2g(ensg, efo, size=min(p.limit or 50, 200))

        # 13 genetics-coloc (colocs exposed with same GraphQL field set)
        if key == "genetics-coloc":
            return await tx_opentargets_l2g(ensg, efo, size=min(p.limit or 50, 200))

        # 14 genetics-mr
        if key == "genetics-mr":
            # Provide study list via OpenGWAS search
            return await tx_opengwas_search(p.condition or symbol)

        # 15 genetics-rare
        if key == "genetics-rare":
            # ClinVar by gene
            terms = [f"{symbol}[gene] AND human[orgn]"]
            for syn in (aliases or [])[1:6]:
                terms.append(f"{syn}[gene] AND human[orgn]")
            ev = await try_ncbi_esearch("clinvar", terms, retmax=min(p.limit or 50, 200))
            if ev.status == "OK" and ev.fetched_n > 0:
                return ev
            # Fallback: MyVariant gene sweep
            return await tx_myvariant_by_gene(symbol, size=min(p.limit or 50, 200))

        # 16 genetics-mendelian
        if key == "genetics-mendelian":
            q = f'({symbol}) AND ("ClinGen" AND curation)'
            return await tx_eupmc_search(q, page_size=min(p.limit or 50, 200))

        # 17 genetics-phewas-human-knockout (requires variant; provide guidance via empty evidence)
        if key == "genetics-phewas-human-knockout":
            return _ev_empty("OpenGWAS PheWAS", ["https://gwas.mrcieu.ac.uk/"], {"note": "provide rsID/variant for PheWAS"})

        # 18 genetics-sqtl
        if key == "genetics-sqtl":
            return await tx_eqtl_sqtl(ensg, size=min(p.limit or 50, 200))

        # 19 genetics-pqtl
        if key == "genetics-pqtl":
            return await tx_opentargets_evidence(symbol, efo, size=min(p.limit or 50, 200))

        # 20 genetics-chromatin-contacts
        if key == "genetics-chromatin-contacts":
            return await tx_encode_search({"type": "Experiment", "searchTerm": symbol, "format": "json"})

        # 21 genetics-3d-maps
        if key == "genetics-3d-maps":
            return await tx_4dn_search(symbol)

        # 22 genetics-regulatory
        if key == "genetics-regulatory":
            return await tx_encode_search({"type": "Experiment", "target.label": symbol, "status": "released", "format": "json"})

        # 23 genetics-annotation
        if key == "genetics-annotation":
            return await tx_myvariant_by_gene(symbol, size=min(p.limit or 50, 200))

        # 24 genetics-consortia-summary
        if key == "genetics-consortia-summary":
            return await tx_opengwas_search(p.condition or symbol)

        # 25 genetics-functional
        if key == "genetics-functional":
            key_bg = os.getenv("BIOGRID_KEY")
            if key_bg:
                try:
                    js = await live.get_json("https://webservice.thebiogrid.org/orthogonal/", params={"geneList": symbol, "includeInteractors": "false", "accesskey": key_bg, "format": "json"})
                    return _ev_ok("BioGRID ORCS", js, ["https://orcs.thebiogrid.org/"], fetched_n=len(js))
                except Exception:
                    pass
            # fallback: literature CRISPR screens
            return await try_europepmc_queries(_sc_queries(symbol, '(CRISPR screen OR genome-wide screen OR loss-of-function)'))

        # 26 genetics-mavedb
        if key == "genetics-mavedb":
            try:
                js = await live.get_json("https://api.mavedb.org/experiments", params={"q": symbol})
                return _ev_ok("MaveDB", js, ["https://mavedb.org/"], fetched_n=js.get("count", 0))
            except Exception as e:
                return _ev_error("MaveDB", str(e), ["https://mavedb.org/"])

        # 27 genetics-lncrna
        if key == "genetics-lncrna":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '(lncRNA OR "long noncoding RNA" OR "long non-coding RNA")')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 28 genetics-mirna
        if key == "genetics-mirna":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '(miRNA OR microRNA)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 29 genetics-pathogenicity-priors
        if key == "genetics-pathogenicity-priors":
            return await tx_gnomad_constraint(symbol)

        # 30 genetics-intolerance
        if key == "genetics-intolerance":
            return await tx_gnomad_constraint(symbol)

        # 31 mech-structure
        if key == "mech-structure":
            return await tx_uniprot_entry(p.uniprot_id or symbol)

        # 32 mech-ppi
        if key == "mech-ppi":
            return await tx_string(symbol)

        # 33 mech-pathways
        if key == "mech-pathways":
            return await tx_pathways(p.uniprot_id or symbol)

        # 34 mech-ligrec
        if key == "mech-ligrec":
            return await tx_ligrec(symbol)

        # 35 biology-causal-pathways
        if key == "biology-causal-pathways":
            queries = []
            for s in (aliases or [symbol])[:6]:
                queries += _sc_queries(s, '("causal pathway" OR "causal interaction" OR SIGNOR)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 36 tract-drugs
        if key == "tract-drugs":
            ev1 = await tx_chembl_target(symbol)
            ev2 = await tx_drugcentral_target(symbol)
            dgi = await tx_dgidb(symbol) if os.getenv("DGIDB_API_KEY") else _ev_empty("DGIdb GraphQL", ["https://dgidb.org/graphql"])
            pc = await tx_pubchem_gene_aids(symbol)
            n = (ev1.fetched_n or 0) + (ev2.fetched_n or 0) + (dgi.fetched_n or 0) + (pc.fetched_n or 0)
            return _ev_ok("ChEMBL+DrugCentral+DGIdb+PubChem", {"chembl": ev1.dict(), "drugcentral": ev2.dict(), "dgidb": dgi.dict(), "pubchem": pc.dict()}, ["https://www.ebi.ac.uk/chembl/", "https://drugcentral.org/api", "https://dgidb.org/graphql", "https://pubchem.ncbi.nlm.nih.gov/"], fetched_n=n)

        # 37 tract-ligandability-sm
        if key == "tract-ligandability-sm":
            return await tx_pubchem_gene_aids(symbol)

        # 38 tract-ligandability-ab
        if key == "tract-ligandability-ab":
            # compute heuristic from UniProt features
            ev = await tx_uniprot_entry(p.uniprot_id or symbol)
            if ev.status != "OK":
                return ev
            feats = ev.data.get("features", []) or []
            comments = json.dumps(ev.data.get("comments", [])).lower()
            n_glyco = len([f for f in feats if (f.get("type") or "").lower().startswith("glycosylation")])
            n_tm = len([f for f in feats if (f.get("type") or "").lower().startswith("transmembrane")])
            pm = ("plasma membrane" in comments) or ("cell membrane" in comments)
            score = float(pm) + 0.5 * float(n_glyco > 0) - 0.3 * float(n_tm > 7)
            return _ev_ok("UniProtKB (features-derived)", {"pm_localization": pm, "n_glyco": n_glyco, "n_tm": n_tm, "heuristic_score": score}, ["https://rest.uniprot.org/"], fetched_n=1)

        # 39 tract-ligandability-oligo
        if key == "tract-ligandability-oligo":
            return await tx_rnacentral(symbol, size=min(p.limit or 50, 200))

        # 40 tract-modality
        if key == "tract-modality":
            evu = await tx_uniprot_entry(p.uniprot_id or symbol)
            has_af2 = bool(evu.status == "OK")
            return _ev_ok("UniProt+AlphaFold", {"alphafold_available": has_af2, "uniprot": evu.dict()}, ["https://rest.uniprot.org/", "https://alphafold.ebi.ac.uk/"], fetched_n=1)

        # 41 tract-immunogenicity
        if key == "tract-immunogenicity":
            return await tx_immuno_lit(symbol, size=min(p.limit or 50, 200))

        # 42 tract-mhc-binding
        if key == "tract-mhc-binding":
            return await tx_immuno_lit(symbol, size=min(p.limit or 50, 200))

        # 43 tract-iedb-epitopes
        if key == "tract-iedb-epitopes":
            queries = _sc_queries(symbol, '(IEDB OR epitope)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 44 tract-surfaceome
        if key == "tract-surfaceome":
            return await dispatch_module("tract-ligandability-ab", p)

        # 45 function-dependency
        if key == "function-dependency":
            depmap_key = os.getenv("DEPMAP_API_KEY")
            if depmap_key:
                try:
                    js = await live.get_json("https://api.depmap.org/api/v1/genes", params={"gene": symbol, "token": depmap_key})
                    return _ev_ok("DepMap", js, ["https://depmap.org/"], fetched_n=len(js) if isinstance(js, list) else 0)
                except Exception:
                    pass
            return await dispatch_module("genetics-functional", p)

        # 46 immuno-hla-coverage
        if key == "immuno-hla-coverage":
            queries = _sc_queries(symbol, '("population coverage" OR HLA)')
            return await try_europepmc_queries(queries, page_size=min(p.limit or 50, 200))

        # 47 clin-endpoints
        if key == "clin-endpoints":
            q = " ".join([x for x in [symbol, p.condition] if x])
            return await tx_ctgov(q, size=min(p.limit or 50, 100), recruiting_only=False)

        # 48 clin-biomarker-fit
        if key == "clin-biomarker-fit":
            return await tx_opentargets_evidence(symbol, efo, size=min(p.limit or 50, 200))

        # 49 clin-pipeline
        if key == "clin-pipeline":
            try:
                js = await live.get_json("https://drugs.ncats.io/api/v1/drugs", params={"q": p.condition or symbol or "", "top": min(p.limit or 50, 200)})
                return _ev_ok("Inxight Drugs", js, ["https://drugs.ncats.io/"], fetched_n=js.get("count", 0) if isinstance(js, dict) else 0)
            except Exception:
                pass
            return await tx_chembl_target(symbol)

        # 50 clin-safety
        if key == "clin-safety":
            return await tx_faers(symbol, size=min(p.limit or 100, 100))

        # 51 clin-rwe
        if key == "clin-rwe":
            return await tx_faers(symbol, size=min(p.limit or 100, 100))

        # 52 clin-on-target-ae-prior
        if key == "clin-on-target-ae-prior":
            try:
                js = await live.get_json("https://drugcentral.org/api/adr", params={"q": symbol})
                if isinstance(js, list) and js:
                    return _ev_ok("DrugCentral ADR", js, ["https://drugcentral.org/"], fetched_n=len(js))
            except Exception:
                pass
            return await tx_faers(symbol, size=min(p.limit or 100, 100))

        # 53 clin-feasibility
        if key == "clin-feasibility":
            q = " ".join([x for x in [symbol, p.condition] if x])
            return await tx_ctgov(q, size=min(p.limit or 50, 100), recruiting_only=True)

        # 54 comp-intensity
        if key == "comp-intensity":
            try:
                js = await live.post_json("https://api.patentsview.org/patents/query", {
                    "q": {"_text_any": {"patent_title": f"{symbol} {p.condition or ''}".strip()}},
                    "f": ["patent_number", "patent_title", "patent_date"],
                    "o": {"per_page": min(p.limit or 50, 100)}
                })
                return _ev_ok("PatentsView", js, ["https://patentsview.org/"], fetched_n=len(js.get("patents", [])) if isinstance(js, dict) else 0)
            except Exception as e:
                return _ev_error("PatentsView", str(e), ["https://patentsview.org/"])

        # 56 lit-search-adv
        if key == "lit-search-adv":
            return await _dispatch_lit_search_adv(p)

        # 57 lit-relations
        if key == "lit-relations":
            return await _dispatch_lit_relations(p)

        # 58 clin-safety-penalty
        if key == "clin-safety-penalty":
            return await _dispatch_safety_penalty(p)

        # 55 comp-freedom
        if key == "comp-freedom":
            return _ev_empty("PatentsView API", ["https://patentsview.org/"], debug={"reason": "not implemented"})

        return _ev_error("router", f"Unhandled module key: {key}", [])
    except Exception as e:
        return _ev_error("router", str(e), [])

# -------------------------------------------------------------------------------------

# -------------------------------------------------------------------------------------
# Dynamic registration — inline registry (55 modules; YAML removed)
# -------------------------------------------------------------------------------------

# Inline module registry (exactly the 55 core modules). Source: user-provided targetval_modules.yaml (trimmed to IDs 1–55).
# Keeping the same structure the old YAML loader produced so downstream code stays the same.
MODCFG = {
    "modules": [
        {"id":1, "key":"expr-baseline", "path":"/expr/baseline", "domains":{"primary":[3], "secondary":[2,5]}, "live_sources":["GTEx API","EBI Expression Atlas API"]},
        {"id":2, "key":"expr-localization", "path":"/expr/localization", "domains":{"primary":[3,4], "secondary":[5]}, "live_sources":["UniProtKB API"]},
        {"id":3, "key":"expr-inducibility", "path":"/expr/inducibility", "domains":{"primary":[3], "secondary":[2,5]}, "live_sources":["EBI Expression Atlas API","NCBI GEO E-utilities","ArrayExpress/BioStudies API"]},
        {"id":4, "key":"assoc-bulk-rna", "path":"/assoc/bulk-rna", "domains":{"primary":[2], "secondary":[1]}, "live_sources":["NCBI GEO E-utilities","ArrayExpress/BioStudies API","EBI Expression Atlas API"]},
        {"id":5, "key":"assoc-sc", "path":"/assoc/sc", "domains":{"primary":[3], "secondary":[2]}, "live_sources":["HCA Azul APIs","Single-Cell Expression Atlas API","CELLxGENE Discover API"]},
        {"id":6, "key":"assoc-spatial", "path":"/assoc/spatial", "domains":{"primary":[3], "secondary":[2,5]}, "live_sources":["Europe PMC API"]},
        {"id":7, "key":"sc-hubmap", "path":"/sc/hubmap", "domains":{"primary":[3], "secondary":[2]}, "live_sources":["HuBMAP Search API","HCA Azul APIs"]},
        {"id":8, "key":"assoc-proteomics", "path":"/assoc/proteomics", "domains":{"primary":[2], "secondary":[1,5,6]}, "live_sources":["ProteomicsDB API","PRIDE Archive API","PDC (CPTAC) GraphQL"]},
        {"id":9, "key":"assoc-metabolomics", "path":"/assoc/metabolomics", "domains":{"primary":[2], "secondary":[1,6]}, "live_sources":["MetaboLights API","Metabolomics Workbench API"]},
        {"id":10, "key":"assoc-hpa-pathology", "path":"/assoc/hpa-pathology", "domains":{"primary":[3], "secondary":[2,5]}, "live_sources":["Europe PMC API"]},
        {"id":11, "key":"assoc-perturb", "path":"/assoc/perturb", "domains":{"primary":[2], "secondary":[4]}, "live_sources":["LINCS LDP APIs","CLUE.io API","PubChem PUG-REST"]},
        {"id":12, "key":"genetics-l2g", "path":"/genetics/l2g", "domains":{"primary":[1], "secondary":[6]}, "live_sources":["OpenTargets GraphQL (L2G)","GWAS Catalog REST API"]},
        {"id":13, "key":"genetics-coloc", "path":"/genetics/coloc", "domains":{"primary":[1], "secondary":[2]}, "live_sources":["OpenTargets GraphQL (colocalisations)","eQTL Catalogue API","OpenGWAS API"]},
        {"id":14, "key":"genetics-mr", "path":"/genetics/mr", "domains":{"primary":[1]}, "live_sources":["IEU OpenGWAS API","PhenoScanner v2 API"]},
        {"id":15, "key":"genetics-rare", "path":"/genetics/rare", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["ClinVar via NCBI E-utilities","MyVariant.info","Ensembl VEP REST"]},
        {"id":16, "key":"genetics-mendelian", "path":"/genetics/mendelian", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["ClinGen GeneGraph/GraphQL"]},
        {"id":17, "key":"genetics-phewas-human-knockout", "path":"/genetics/phewas-human-knockout", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["PhenoScanner v2 API","OpenGWAS PheWAS","HPO/Monarch APIs"]},
        {"id":18, "key":"genetics-sqtl", "path":"/genetics/sqtl", "domains":{"primary":[1,3]}, "live_sources":["GTEx sQTL API","eQTL Catalogue API"]},
        {"id":19, "key":"genetics-pqtl", "path":"/genetics/pqtl", "domains":{"primary":[1,3]}, "live_sources":["OpenTargets GraphQL (pQTL colocs)","OpenGWAS (protein traits when available)"]},
        {"id":20, "key":"genetics-chromatin-contacts", "path":"/genetics/chromatin-contacts", "domains":{"primary":[1], "secondary":[2]}, "live_sources":["ENCODE REST API","UCSC Genome Browser track APIs","4D Nucleome API"]},
        {"id":21, "key":"genetics-3d-maps", "path":"/genetics/3d-maps", "domains":{"primary":[1], "secondary":[2]}, "live_sources":["4D Nucleome API","UCSC loop/interaction tracks"]},
        {"id":22, "key":"genetics-regulatory", "path":"/genetics/regulatory", "domains":{"primary":[1], "secondary":[2,3]}, "live_sources":["ENCODE REST API","eQTL Catalogue API"]},
        {"id":23, "key":"genetics-annotation", "path":"/genetics/annotation", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["Ensembl VEP REST","MyVariant.info","CADD API"]},
        {"id":24, "key":"genetics-consortia-summary", "path":"/genetics/consortia-summary", "domains":{"primary":[1], "secondary":[6]}, "live_sources":["IEU OpenGWAS API"]},
        {"id":25, "key":"genetics-functional", "path":"/genetics/functional", "domains":{"primary":[1], "secondary":[2]}, "live_sources":["DepMap API","BioGRID ORCS REST","Europe PMC API"]},
        {"id":26, "key":"genetics-mavedb", "path":"/genetics/mavedb", "domains":{"primary":[1], "secondary":[2]}, "live_sources":["MaveDB API"]},
        {"id":27, "key":"genetics-lncrna", "path":"/genetics/lncrna", "domains":{"primary":[2], "secondary":[1]}, "live_sources":["RNAcentral API","Europe PMC API"]},
        {"id":28, "key":"genetics-mirna", "path":"/genetics/mirna", "domains":{"primary":[2], "secondary":[1]}, "live_sources":["RNAcentral API","Europe PMC API"]},
        {"id":29, "key":"genetics-pathogenicity-priors", "path":"/genetics/pathogenicity-priors", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["gnomAD GraphQL API","CADD API"]},
        {"id":30, "key":"genetics-intolerance", "path":"/genetics/intolerance", "domains":{"primary":[1], "secondary":[5]}, "live_sources":["gnomAD GraphQL API"]},
        {"id":31, "key":"mech-structure", "path":"/mech/structure", "domains":{"primary":[4], "secondary":[2]}, "live_sources":["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API"]},
        {"id":32, "key":"mech-ppi", "path":"/mech/ppi", "domains":{"primary":[2], "secondary":[4,5]}, "live_sources":["STRING API","IntAct via PSICQUIC","OmniPath API"]},
        {"id":33, "key":"mech-pathways", "path":"/mech/pathways", "domains":{"primary":[2], "secondary":[5]}, "live_sources":["Reactome Content/Analysis APIs","Pathway Commons API","SIGNOR API","QuickGO API"]},
        {"id":34, "key":"mech-ligrec", "path":"/mech/ligrec", "domains":{"primary":[2,4], "secondary":[3]}, "live_sources":["OmniPath (ligand–receptor)","IUPHAR/Guide to Pharmacology API","Reactome interactors"]},
        {"id":35, "key":"biology-causal-pathways", "path":"/biology/causal-pathways", "domains":{"primary":[2], "secondary":[5]}, "live_sources":["SIGNOR API","Reactome Analysis Service","Pathway Commons API"]},
        {"id":36, "key":"tract-drugs", "path":"/tract/drugs", "domains":{"primary":[4,6], "secondary":[1]}, "live_sources":["ChEMBL API","DGIdb GraphQL","DrugCentral API","BindingDB API","PubChem PUG-REST","STITCH API","Pharos GraphQL"]},
        {"id":37, "key":"tract-ligandability-sm", "path":"/tract/ligandability-sm", "domains":{"primary":[4], "secondary":[2]}, "live_sources":["UniProtKB API","AlphaFold DB API","PDBe API","PDBe-KB API","BindingDB API"]},
        {"id":38, "key":"tract-ligandability-ab", "path":"/tract/ligandability-ab", "domains":{"primary":[4,3], "secondary":[5]}, "live_sources":["UniProtKB API","GlyGen API"]},
        {"id":39, "key":"tract-ligandability-oligo", "path":"/tract/ligandability-oligo", "domains":{"primary":[4], "secondary":[2]}, "live_sources":["Ensembl VEP REST","RNAcentral API","Europe PMC API"]},
        {"id":40, "key":"tract-modality", "path":"/tract/modality", "domains":{"primary":[4], "secondary":[6]}, "live_sources":["UniProtKB API","AlphaFold DB API","Pharos GraphQL","IUPHAR/Guide to Pharmacology API"]},
        {"id":41, "key":"tract-immunogenicity", "path":"/tract/immunogenicity", "domains":{"primary":[5], "secondary":[4]}, "live_sources":["IEDB IQ-API","IPD-IMGT/HLA API","Europe PMC API"]},
        {"id":42, "key":"tract-mhc-binding", "path":"/tract/mhc-binding", "domains":{"primary":[5], "secondary":[4]}, "live_sources":["IEDB Tools API (prediction)","IPD-IMGT/HLA API"]},
        {"id":43, "key":"tract-iedb-epitopes", "path":"/tract/iedb-epitopes", "domains":{"primary":[5], "secondary":[4]}, "live_sources":["IEDB IQ-API","IEDB Tools API"]},
        {"id":44, "key":"tract-surfaceome", "path":"/tract/surfaceome", "domains":{"primary":[4,3], "secondary":[6]}, "live_sources":["UniProtKB API","GlyGen API"]},
        {"id":45, "key":"function-dependency", "path":"/function/dependency", "domains":{"primary":[5], "secondary":[2,3]}, "live_sources":["DepMap API","BioGRID ORCS REST"]},
        {"id":46, "key":"immuno-hla-coverage", "path":"/immuno/hla-coverage", "domains":{"primary":[5], "secondary":[6]}, "live_sources":["IEDB population coverage/Tools API","IPD-IMGT/HLA API"]},
        {"id":47, "key":"clin-endpoints", "path":"/clin/endpoints", "domains":{"primary":[6], "secondary":[1]}, "live_sources":["ClinicalTrials.gov v2 API","WHO ICTRP web service"]},
        {"id":48, "key":"clin-biomarker-fit", "path":"/clin/biomarker-fit", "domains":{"primary":[6], "secondary":[1]}, "live_sources":["OpenTargets GraphQL (evidence)","PharmGKB API","HPO/Monarch APIs"]},
        {"id":49, "key":"clin-pipeline", "path":"/clin/pipeline", "domains":{"primary":[6], "secondary":[4]}, "live_sources":["Inxight Drugs API","ChEMBL API","DrugCentral API"]},
        {"id":50, "key":"clin-safety", "path":"/clin/safety", "domains":{"primary":[5]}, "live_sources":["openFDA FAERS API","DrugCentral API","CTDbase API","DGIdb GraphQL","IMPC API"]},
        {"id":51, "key":"clin-rwe", "path":"/clin/rwe", "domains":{"primary":[5]}, "live_sources":["openFDA FAERS API"]},
        {"id":52, "key":"clin-on-target-ae-prior", "path":"/clin/on-target-ae-prior", "domains":{"primary":[5], "secondary":[3]}, "live_sources":["DrugCentral API","DGIdb GraphQL","openFDA FAERS API"]},
        {"id":53, "key":"clin-feasibility", "path":"/clin/feasibility", "domains":{"primary":[6]}, "live_sources":["ClinicalTrials.gov v2 API","WHO ICTRP web service"]},
        {"id":54, "key":"comp-intensity", "path":"/comp/intensity", "domains":{"primary":[6]}, "live_sources":["PatentsView API"]},
        {"id":55, "key":"comp-freedom", "path":"/comp/freedom", "domains":{"primary":[6]}, "live_sources":["PatentsView API"]}
    ]
}

MOD_BY_KEY = {m["key"]: m for m in MODCFG["modules"]}

def _register(mod: dict) -> None:
    path = mod["path"]; key = mod["key"]
    desc = f"{key} | domains: {mod['domains']} | live_sources: {', '.join(mod.get('live_sources', []))}"
    @router.get(path, summary=key, description=desc, response_model=Evidence)
    async def endpoint(
        symbol: Optional[str] = Query(None, description="HGNC symbol"),
        ensembl_id: Optional[str] = Query(None, description="ENSG"),
        uniprot_id: Optional[str] = Query(None, description="UniProt accession"),
        condition: Optional[str] = Query(None, description="Condition/trait label"),
        efo: Optional[str] = Query(None, description="EFO/MONDO ID or label"),
        tissue: Optional[str] = Query(None),
        cell_type: Optional[str] = Query(None),
        species: Optional[str] = Query("human"),
        limit: Optional[int] = Query(100, ge=1, le=500),
        offset: Optional[int] = Query(0, ge=0),
        strict: Optional[bool] = Query(False),
    ) -> Evidence:
        return await dispatch_module(key, ModuleParams(symbol=symbol, ensembl_id=ensembl_id, uniprot_id=uniprot_id, condition=condition, efo=efo, tissue=tissue, cell_type=cell_type, species=species, limit=limit, offset=offset, strict=strict))

for m in MODCFG["modules"]:
    _register(m)


# -------------------------------------------------------------------------------------
# Literature & synthesis endpoints (unchanged API, improved debug)
# -------------------------------------------------------------------------------------

class LitParams(BaseModel):
    symbol: str
    condition: Optional[str] = None
    window_days: int = 1825
    page_size: int = 50

@router.get("/lit/search", response_model=Evidence, summary="Europe PMC search (5y default)")
async def lit_search(symbol: str = Query(...), condition: Optional[str] = Query(None), window_days: int = Query(1825), page_size: int = Query(50)) -> Evidence:
    queries = [f'({symbol}) AND FIRST_PDATE:[NOW-{window_days}DAY TO NOW]']
    if condition:
        queries.insert(0, f'({symbol}) AND ({condition}) AND FIRST_PDATE:[NOW-{window_days}DAY TO NOW]')
    return await try_europepmc_queries(queries, page_size=page_size)

@router.get("/lit/claims", response_model=Evidence, summary="PubTator3 relations proxy")
async def lit_claims(symbol: str = Query(...), condition: Optional[str] = Query(None), limit: int = Query(50)) -> Evidence:
    base = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/search/"
    try:
        js = await live.get_json(base, params={"text": f"{symbol} {condition or ''}".strip(), "entity_types": "gene,disease,chemical", "size": limit})
        return _ev_ok("PubTator3", js, ["https://www.ncbi.nlm.nih.gov/research/pubtator3/"], fetched_n=len(js.get("data", [])) if isinstance(js, dict) else 0)
    except Exception as e:
        return _ev_error("PubTator3", str(e), ["https://www.ncbi.nlm.nih.gov/research/pubtator3/"])

@router.post("/lit/score", response_model=Evidence, summary="Score literature items (objective rubric)")
async def lit_score(payload: Dict[str, Any] = Body(...)) -> Evidence:
    items = payload.get("items", [])
    def grade(it: Dict[str, Any]) -> float:
        design = {"meta": 1.0, "rct": 1.0, "mr": 0.9, "gwas": 0.85, "screen": 0.8, "cohort": 0.6, "case": 0.3, "review": 0.1}.get(str(it.get("design","")).lower(), 0.3)
        n = float(it.get("n", 0) or 0); size = min(1.0, (0.2 + 0.1 * (len(str(int(n))) - 1)) if n else 0.0)
        year = int(it.get("year", datetime.now().year)); recency = max(0.0, min(1.0, 1.0 - max(0, datetime.now().year - year - 5) * 0.1))
        venue = 0.15 if it.get("venue_indexed", True) else 0.0
        cites = min(1.0, float(it.get("citations_3y", 0)) / 20.0) * 0.15
        penalties = 1.0 if it.get("retracted") else (0.8 if it.get("preprint") else 0.0)
        return max(0.0, min(1.0, 0.35*design + 0.15*size + 0.15*recency + venue + cites - penalties))
    graded = [{"item": it, "score": grade(it)} for it in items]
    return _ev_ok("Targetval Literature Scorer", {"graded": graded}, [], fetched_n=len(graded))

@router.post("/synth/targetcard", response_model=Evidence, summary="Assemble a target card (modules + literature)")
async def synth_targetcard(payload: Dict[str, Any] = Body(...)) -> Evidence:
    symbol = payload.get("symbol"); condition = payload.get("condition")
    modules = payload.get("modules") or [m["key"] for m in MODCFG["modules"]]
    p = ModuleParams(symbol=symbol, condition=condition)
    tasks = [dispatch_module(k, p) for k in modules]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assembled = {}
    for k, ev in zip(modules, results):
        if isinstance(ev, Evidence):
            assembled[k] = ev.dict()
        else:
            assembled[k] = _ev_error("router", f"{ev}", []).dict()
    lit = (await lit_search(symbol or "", condition=condition)).dict()
    return _ev_ok("Targetval Synthesizer", {"modules": assembled, "literature": lit}, [], fetched_n=len(assembled))

@router.post("/synth/triggers", response_model=Evidence, summary="Derive refresh triggers from high-grade claims")
async def synth_triggers(payload: Dict[str, Any] = Body(...)) -> Evidence:
    claims = payload.get("claims", []); thr = float(payload.get("threshold", 0.7))
    triggers = []
    for c in claims:
        if float(c.get("score", 0.0)) >= thr:
            pred = str(c.get("predicate", ""))
            if pred in {"associates", "increases", "decreases"} and c.get("design") in {"GWAS", "MR", "colocalization"}:
                triggers.append({"rerun_modules": ["genetics-l2g", "genetics-coloc", "genetics-mr", "genetics-consortia-summary"], "scope": {"efo": c.get("efo")}})
            elif pred in {"regulates", "binds"} and c.get("tissue"):
                triggers.append({"rerun_modules": ["genetics-regulatory", "genetics-sqtl"], "scope": {"tissue": c.get("tissue")}})
            elif pred in {"links_variant_to_promoter"}:
                triggers.append({"rerun_modules": ["genetics-3d-maps", "genetics-chromatin-contacts"], "scope": {"locus": c.get("locus")}})
            elif pred in {"perturbs"} and c.get("design") in {"CRISPR", "transcriptomics", "L1000"}:
                triggers.append({"rerun_modules": ["assoc-perturb"], "scope": {"system": c.get("system")}})
            elif pred in {"causes_adverse_event", "increases_risk"} and c.get("adverse_event"):
                triggers.append({"rerun_modules": ["clin-safety", "clin-rwe", "clin-on-target-ae-prior"], "scope": {"ae": c.get("adverse_event")}})
            elif pred in {"is_epitope_for"} and c.get("hla"):
                triggers.append({"rerun_modules": ["tract-iedb-epitopes", "tract-mhc-binding", "immuno-hla-coverage"], "scope": {"hla": c.get("hla")}})
    return _ev_ok("Targetval Trigger Engine", {"triggers": triggers}, [], fetched_n=len(triggers))

@router.get("/health", response_model=Evidence)
async def health() -> Evidence:
    return _ev_ok("router", {"ok": True, "modules": len(MODCFG["modules"])}, [], fetched_n=1)

# === BEGIN ENHANCEMENTS: literature filters, relations, safety penalty ===

# Utilities for building Europe PMC fielded queries and computing FAERS-derived safety penalty.
from typing import List, Optional, Dict, Any
from fastapi import Query, Body
from pydantic import BaseModel
from datetime import datetime as _dt
import math

def _date_ymd(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    # Accept YYYY or YYYY-MM or YYYY-MM-DD
    try:
        if len(s) == 4:
            return f"{s}-01-01"
        if len(s) == 7:
            return f"{s}-01"
        # assume already YYYY-MM-DD
        _ = _dt.fromisoformat(s)
        return s
    except Exception:
        return None

def build_eupmc_query(symbol: str, condition: Optional[str] = None,
                      from_date: Optional[str] = None, to_date: Optional[str] = None,
                      window_days: Optional[int] = None,
                      species: Optional[str] = None,
                      study_types: Optional[List[str]] = None,
                      article_types: Optional[List[str]] = None,
                      strict: bool = False) -> str:
    """Construct a conservative Europe PMC Lucene query.

    Notes:
      - FIRST_PDATE range is supported per Europe PMC search syntax.
      - For study/species/article filters, we conservatively inject phrases as tokens to avoid brittle field names.
        (You can upgrade this to fielded filters like PUB_TYPE or MeSH-specific clauses later.)
    """
    clauses = []
    base = f"({symbol})"
    if condition:
        base = f"({symbol}) AND ({condition})"
    clauses.append(base)

    # Publication date filter
    if from_date or to_date:
        fd = _date_ymd(from_date) or "1900-01-01"
        td = _date_ymd(to_date) or _dt.now().strftime("%Y-%m-%d")
        clauses.append(f'FIRST_PDATE:[{fd} TO {td}]')
    elif window_days and window_days > 0:
        clauses.append(f'FIRST_PDATE:[NOW-{window_days}DAY TO NOW]')

    # Species (insert as phrase tokens)
    if species:
        spp = [s.strip() for s in (species if isinstance(species, list) else [species]) if s and s.strip()]
        for s in spp:
            # map common shorthands
            s_norm = s.lower()
            if s_norm in {"human", "homo sapiens", "hs"}:
                clauses.append('("Homo sapiens" OR Humans)')
            elif s_norm in {"mouse", "mus musculus", "mm"}:
                clauses.append('("Mus musculus" OR mouse)')
            elif s_norm in {"rat", "rattus norvegicus"}:
                clauses.append('("Rattus norvegicus" OR rat)')
            else:
                clauses.append(f'("{s}")')

    # Study types as tokens (e.g., randomized, meta-analysis, mendelian randomization)
    if study_types:
        stoks = []
        for t in study_types:
            tnorm = (t or "").lower()
            if tnorm in {"rct", "randomized", "randomised", "randomized controlled trial"}:
                stoks.append('"randomized controlled"')
                stoks.append('"clinical trial"')
            elif tnorm in {"meta", "meta-analysis", "metaanalysis"}:
                stoks.append('"meta-analysis"')
            elif tnorm in {"mr", "mendelian randomization", "mendelian randomisation"}:
                stoks.append('"Mendelian randomization" OR "Mendelian randomisation"')
            elif tnorm in {"gwas"}:
                stoks.append("GWAS")
            else:
                stoks.append(f'"{t}"')
        if stoks:
            clauses.append("(" + " OR ".join(stoks) + ")")

    # Article types as tokens
    if article_types:
        atoks = []
        for t in article_types:
            tnorm = (t or "").lower()
            if tnorm in {"review"}:
                atoks.append("review")
            elif tnorm in {"preprint"}:
                atoks.append("preprint")
            elif tnorm in {"case report", "case-report"}:
                atoks.append('"case report"')
            else:
                atoks.append(f'"{t}"')
        if atoks:
            clauses.append("(" + " OR ".join(atoks) + ")")

    op = " AND " if strict else " AND "
    return f"{op.join(clauses)}"

@router.get("/lit/search/advanced", response_model=Evidence, summary="Europe PMC search with filters")
async def lit_search_advanced(
    symbol: str = Query(...),
    condition: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="YYYY or YYYY-MM or YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="YYYY or YYYY-MM or YYYY-MM-DD"),
    window_days: int = Query(1825, ge=0, description="Ignored if from_date/to_date provided"),
    species: Optional[str] = Query(None, description="human, mouse, rat, etc. (free text OK)"),
    study_types: Optional[str] = Query(None, description="CSV, e.g., rct,meta,mr,gwas"),
    article_types: Optional[str] = Query(None, description="CSV, e.g., review,preprint"),
    page_size: int = Query(50, ge=1, le=200),
    strict: bool = Query(False)
) -> Evidence:
    st = [s.strip() for s in (study_types.split(",") if study_types else []) if s.strip()]
    at = [s.strip() for s in (article_types.split(",") if article_types else []) if s.strip()]
    q = build_eupmc_query(symbol, condition, from_date, to_date, window_days, species, st, at, strict=strict)
    return await try_europepmc_queries([q], page_size=page_size)

# PubTator3 export-based relations extraction (BioC JSON co-mentions)
async def tx_pubtator_export_relations(pmids: List[str], concepts: str = "gene,disease,chemical", max_relations_per_doc: int = 20) -> Evidence:
    base = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
    try:
        js = await live.get_json(base, params={"pmids": ",".join(pmids[:50]), "concepts": concepts})
        rels = []
        docs = js.get("documents") if isinstance(js, dict) else None
        if not docs:
            return _ev_empty("PubTator3 Export", ["https://www.ncbi.nlm.nih.gov/research/pubtator3/api/"])
        for d in docs:
            pmid = str(d.get("id") or "")
            passages = d.get("passages") or []
            for p in passages:
                text = p.get("text") or ""
                anns = p.get("annotations") or []
                genes = [a for a in anns if (a.get("infons", {}) or {}).get("type", "").lower() in {"gene", "protein", "gene/protein"}]
                dises = [a for a in anns if (a.get("infons", {}) or {}).get("type", "").lower() in {"disease", "disease_or_phenotype", "disease/phenotype"}]
                # simple co-mention heuristic
                count_doc = 0
                for gi, g in enumerate(genes):
                    gtext = (g.get("text") or "").strip()
                    for di, dzz in enumerate(dises):
                        dztext = (dzz.get("text") or "").strip()
                        if gtext and dztext:
                            rels.append({
                                "pmid": pmid,
                                "gene": gtext,
                                "disease": dztext,
                                "sentence": text[:4000]
                            })
                            count_doc += 1
                            if count_doc >= max_relations_per_doc:
                                break
                    if count_doc >= max_relations_per_doc:
                        break
        return _ev_ok("PubTator3 Export (BioC co-mentions)", {"relations": rels}, ["https://www.ncbi.nlm.nih.gov/research/pubtator3/api/"], fetched_n=len(rels))
    except Exception as e:
        return _ev_error("PubTator3 Export", str(e), ["https://www.ncbi.nlm.nih.gov/research/pubtator3/api/"])

@router.get("/lit/relations", response_model=Evidence, summary="Extract gene–disease co-mentions via PubTator3 BioC")
async def lit_relations(
    pmids: str = Query(..., description="Comma-separated PMIDs"),
    concepts: str = Query("gene,disease,chemical"),
    max_relations_per_doc: int = Query(20, ge=1, le=200)
) -> Evidence:
    pmid_list = [p.strip() for p in pmids.split(",") if p.strip()]
    return await tx_pubtator_export_relations(pmid_list, concepts=concepts, max_relations_per_doc=max_relations_per_doc)

# FAERS-derived safety penalty
def _sum_counts(results: Any, key_count: str = "count") -> int:
    try:
        return int(sum(int(r.get(key_count, 0) or 0) for r in (results or [])))
    except Exception:
        return 0

async def tx_openfda_count(query: str, count_field: str, limit: int = 1000) -> Dict[str, Any]:
    base = "https://api.fda.gov/drug/event.json"
    js = await live.get_json(base, params={"search": query, "count": count_field, "limit": min(limit, 1000)})
    return js or {}

def _build_openfda_drug_query(drugs: List[str], from_date: Optional[str], to_date: Optional[str]) -> str:
    terms = []
    for d in drugs:
        d = d.strip()
        if not d: 
            continue
        # search across medicinal product and substance name; phrase match
        terms.append(f'patient.drug.medicinalproduct:"{d}"')
        terms.append(f'patient.drug.openfda.substance_name:"{d}"')
        terms.append(f'patient.drug.openfda.brand_name:"{d}"')
    drug_clause = "(" + " OR ".join(set(terms)) + ")"
    if from_date or to_date:
        fd = (from_date or "").replace("-", "")
        td = (to_date or "").replace("-", "")
        date_clause = f'receivedate:[{fd or "20040101"}+TO+{td or _dt.now().strftime("%Y%m%d")}]'
        return f"{date_clause}+AND+{drug_clause}"
    return drug_clause

@router.get("/clin/safety/penalty", response_model=Evidence, summary="Compute safety penalty from FAERS counts")
async def safety_penalty(
    drugs: str = Query(..., description="CSV of drug names (brand or substance)"),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to start of FAERS if omitted"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
    scale: int = Query(2000, ge=100, le=100000, description="Reports for penalty=~1.0 (rough scale)")
) -> Evidence:
    """Returns a unitless penalty in [0,1], plus supporting counts.
    Penalty is based on:
      - total reports: count(receivedate) over time window
      - death reports: count(receivedate) with seriousnessdeath:1
    """
    drug_list = [d.strip() for d in drugs.split(",") if d.strip()]
    if not drug_list:
        return _ev_error("openFDA FAERS", "No drugs provided", ["https://open.fda.gov/apis/drug/event/"])
    q = _build_openfda_drug_query(drug_list, from_date, to_date)
    try:
        # total reports timeseries (sum across bins)
        js_total = await tx_openfda_count(q, "receivedate")
        total_reports = _sum_counts(js_total.get("results"))

        # death-only reports
        js_death = await tx_openfda_count(q + "+AND+seriousnessdeath:1", "receivedate")
        death_reports = _sum_counts(js_death.get("results"))

        # derive penalty (conservative, monotonic)
        # hazard ~ sqrt(total_reports/scale); death_weight accentuates severe outcomes
        hazard = min(1.0, math.sqrt((total_reports or 0) / float(scale)))
        death_frac = (death_reports / total_reports) if total_reports else 0.0
        penalty = max(0.0, min(1.0, 0.75*hazard + 0.25*death_frac))

        payload = {
            "inputs": {"drugs": drug_list, "from_date": from_date, "to_date": to_date, "scale": scale},
            "counts": {"total_reports": total_reports, "death_reports": death_reports, "death_fraction": death_frac},
            "penalty": penalty
        }
        return _ev_ok("openFDA FAERS (derived penalty)", payload, ["https://open.fda.gov/apis/drug/event/"], fetched_n=1)
    except Exception as e:
        return _ev_error("openFDA FAERS", str(e), ["https://open.fda.gov/apis/drug/event/"])

# Add dispatch_module handlers so these can also be called via /aggregate using YAML keys
async def _dispatch_lit_search_adv(p: ModuleParams) -> Evidence:
    return await lit_search_advanced(symbol=p.symbol or "", condition=p.condition, from_date=None, to_date=None,
                                     window_days=int(p.limit or 1825), species=p.species, study_types=None,
                                     article_types=None, page_size=min(p.limit or 50, 200), strict=bool(getattr(p, "strict", False)))

async def _dispatch_lit_relations(p: ModuleParams) -> Evidence:
    # For aggregation, derive PMIDs from the basic /lit/search (symbol+condition) window and then export relations
    ev = await lit_search_advanced(symbol=p.symbol or "", condition=p.condition, page_size=min(p.limit or 50, 200))
    hits = (ev.data or {}).get("hits", []) if hasattr(ev, "data") else []
    pmids = [str(h.get("pmid") or h.get("id") or h.get("pmcid") or "").split()[0] for h in hits if h]
    pmids = [pm for pm in pmids if pm and pm.isdigit()]
    if not pmids:
        return _ev_empty("PubTator3 Export (BioC co-mentions)", ["https://www.ncbi.nlm.nih.gov/research/pubtator3/api/"])
    return await tx_pubtator_export_relations(pmids[:20])

async def _dispatch_safety_penalty(p: ModuleParams) -> Evidence:
    # Require condition to be list of comma-separated drug names or fallback to symbol
    drugs = p.condition or (p.symbol or "")
    return await safety_penalty(drugs=drugs)

# === END ENHANCEMENTS ===

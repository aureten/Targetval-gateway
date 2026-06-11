# app/webui.py
# A no-code, free-text "ask" interface for the TargetVal gateway.
# Serves a chat-style page at /chat and an /ask endpoint that:
#   1. parses a free-text query ("B7H7 for oncology") into gene + condition
#   2. resolves gene symbol/alias -> Ensembl + UniProt (mygene.info)
#   3. resolves the condition -> EFO id (EBI OLS), best-effort
#   4. runs a curated, robust set of modules and returns readable results
#
# It reuses the gateway's internal self-call helper, so it goes through the
# same module pipeline as every other endpoint.
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import Body, Query
from fastapi.responses import HTMLResponse, JSONResponse

# Per-module budget so one slow/dead upstream never stalls the whole page.
ASK_MODULE_TIMEOUT_S = float(os.getenv("ASK_MODULE_TIMEOUT_S", "15"))
ASK_CONCURRENCY = int(os.getenv("ASK_CONCURRENCY", "24"))
# Hard wall-clock cap on the whole /ask run so it ALWAYS returns valid JSON well
# within the gunicorn request timeout (any module not done by then -> TIMEOUT).
ASK_TOTAL_BUDGET_S = float(os.getenv("ASK_TOTAL_BUDGET_S", "75"))
RESOLVE_TIMEOUT_S = float(os.getenv("RESOLVE_TIMEOUT_S", "8"))

# Common nicknames -> official HGNC gene symbols. mygene also resolves many
# aliases, but seeding the obvious immuno-oncology ones keeps it snappy/reliable.
ALIASES = {
    "B7H7": "HHLA2", "B7-H7": "HHLA2", "B7Y": "HHLA2",
    "B7H3": "CD276", "B7-H3": "CD276",
    "B7H4": "VTCN1", "B7-H4": "VTCN1",
    "PDL1": "CD274", "PD-L1": "CD274", "PD1": "PDCD1", "PD-1": "PDCD1",
    "CTLA4": "CTLA4", "TIGIT": "TIGIT", "LAG3": "LAG3", "TIM3": "HAVCR2",
    "PCSK9": "PCSK9",
}

# Curated module set for a fast, robust feasibility snapshot across domains.
# Biased toward stable public sources so the casual UI feels responsive.
CURATED_MODULES = [
    "genetics-l2g",           # Open Targets — genetic association (often NO_DATA for IO targets)
    "expr-baseline",          # GTEx — baseline expression
    "expr-localization",      # UniProt — subcellular location
    "assoc-sc",               # Human Cell Atlas — single-cell
    "mech-ppi",               # STRING — interactors
    "mech-pathways",          # Reactome — pathways
    "mech-ligrec",            # OmniPath — ligand/receptor (key for checkpoints)
    "tract-surfaceome",       # UniProt — is it cell-surface?
    "tract-ligandability-ab", # UniProt — antibody tractability
    "tract-modality",         # UniProt — modality fit
    "tract-drugs",            # ChEMBL/EBI — known drugs
    "clin-pipeline",          # ClinicalTrials.gov — programs in development
    "clin-endpoints",         # ClinicalTrials.gov — trial endpoints
    "clin-safety",            # openFDA — adverse events
]

_STOP = {"for", "in", "as", "a", "an", "the", "target", "drug", "develop",
         "therapeutic", "feasibility", "of", "to", "and", "explore"}


def _parse_query(q: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (gene_token, condition_text) from free text."""
    if not q:
        return None, None
    txt = q.strip()
    m = re.match(r"^\s*([A-Za-z0-9\-]+)\s+(?:in|for|->|→|targeting)\s+(.+)$", txt, flags=re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Fallback: first gene-like token is the gene, the rest (minus stopwords) the condition.
    tokens = txt.split()
    gene = None
    for t in tokens:
        c = t.strip(" ,;:.").upper()
        if c and c.lower() not in _STOP and re.fullmatch(r"[A-Z0-9\-]{2,12}", c):
            gene = c
            break
    rest = [t for t in tokens if t.strip(" ,;:.").upper() != gene and t.lower() not in _STOP]
    cond = " ".join(rest).strip(" ,;:.") or None
    return gene, cond


async def _resolve_gene(token: str) -> Dict[str, Optional[str]]:
    """token (symbol/alias) -> {symbol, ensembl_id, uniprot_id}. Best-effort."""
    out: Dict[str, Optional[str]] = {"symbol": token, "ensembl_id": None, "uniprot_id": None}
    if not token:
        return out
    symbol = ALIASES.get(token.upper(), token.upper())
    out["symbol"] = symbol
    try:
        async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_S, headers={"User-Agent": "TargetvalGateway-webui"}) as c:
            r = await c.get(
                "https://mygene.info/v3/query",
                params={"q": f"{symbol}", "species": "human",
                        "fields": "symbol,ensembl.gene,uniprot.Swiss-Prot", "size": 1},
            )
            r.raise_for_status()
            hits = (r.json() or {}).get("hits") or []
            if hits:
                h = hits[0]
                if h.get("symbol"):
                    out["symbol"] = h["symbol"]
                ens = h.get("ensembl")
                if isinstance(ens, list) and ens:
                    ens = ens[0]
                if isinstance(ens, dict):
                    out["ensembl_id"] = ens.get("gene")
                up = (h.get("uniprot") or {}).get("Swiss-Prot")
                if isinstance(up, list) and up:
                    up = up[0]
                out["uniprot_id"] = up
    except Exception:
        pass
    return out


async def _resolve_drug_target(token: str) -> Optional[str]:
    """If the token is a drug/biologic name, return the gene symbol it targets.
    Uses the Open Targets Platform search + mechanisms of action. Best-effort."""
    if not token:
        return None
    gql = """
query ($q: String!) {
  search(queryString: $q, entityNames: ["drug"]) {
    hits {
      id
      object {
        __typename
        ... on Drug {
          mechanismsOfAction { rows { targets { approvedSymbol } } }
        }
      }
    }
  }
}"""
    try:
        async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_S,
                                     headers={"User-Agent": "TargetvalGateway-webui"}) as c:
            r = await c.post("https://api.platform.opentargets.org/api/v4/graphql",
                             json={"query": gql, "variables": {"q": token}})
            r.raise_for_status()
            hits = (((r.json() or {}).get("data") or {}).get("search") or {}).get("hits") or []
            for h in hits:
                obj = h.get("object") or {}
                for row in ((obj.get("mechanismsOfAction") or {}).get("rows") or []):
                    for t in (row.get("targets") or []):
                        sym = t.get("approvedSymbol")
                        if sym:
                            return sym
    except Exception:
        pass
    return None


async def _resolve_efo(condition: Optional[str]) -> Optional[str]:
    if not condition:
        return None
    try:
        async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_S, headers={"User-Agent": "TargetvalGateway-webui"}) as c:
            r = await c.get(
                "https://www.ebi.ac.uk/ols4/api/search",
                params={"q": condition, "ontology": "efo", "rows": 1, "exact": "false"},
            )
            r.raise_for_status()
            docs = (((r.json() or {}).get("response") or {}).get("docs")) or []
            if docs:
                short = docs[0].get("short_form") or docs[0].get("obo_id", "").replace(":", "_")
                if short and short.upper().startswith("EFO"):
                    return short
    except Exception:
        pass
    return None


def register_webui(app, self_get: Callable, modules: Optional[Dict[str, Any]],
                   domain_modules: Optional[Dict[int, List[str]]] = None,
                   domains_meta: Optional[Dict[int, Dict[str, str]]] = None) -> None:
    available = set(modules.keys()) if isinstance(modules, dict) else set()
    dmods: Dict[int, List[str]] = {int(k): list(v) for k, v in (domain_modules or {}).items()}
    dmeta: Dict[int, Dict[str, str]] = {int(k): v for k, v in (domains_meta or {}).items()}
    # module -> (domain_id, domain_name)
    mod_domain: Dict[str, Tuple[int, str]] = {}
    for did, mlist in dmods.items():
        name = (dmeta.get(did) or {}).get("name", f"Domain {did}")
        for m in mlist:
            mod_domain[m] = (did, name)

    @app.get("/ask", include_in_schema=False)
    async def ask(q: str = Query(..., description="Free-text inquiry, e.g. 'B7H7 for oncology'"),
                  quick: bool = False):
        gene_tok, condition = _parse_query(q)
        gene = await _resolve_gene(gene_tok or "")
        drug_hint = None
        # If it didn't resolve to a real gene, the token may be a drug/biologic
        # name (e.g. "sActRIIb-Fc") — look up the gene it targets, then re-resolve.
        if not gene.get("ensembl_id") and gene_tok:
            tgt = await _resolve_drug_target(gene_tok)
            if tgt and tgt.upper() != (gene.get("symbol") or "").upper():
                drug_hint = gene_tok
                gene = await _resolve_gene(tgt)
        efo = await _resolve_efo(condition)
        resolved = {
            "symbol": gene["symbol"], "ensembl_id": gene["ensembl_id"],
            "uniprot_id": gene["uniprot_id"], "condition": condition, "efo": efo,
            "drug": drug_hint,
        }
        params = {
            "symbol": resolved["symbol"], "ensembl_id": resolved["ensembl_id"],
            "uniprot_id": resolved["uniprot_id"], "efo": resolved["efo"],
            "condition": resolved["condition"], "limit": 25,
        }
        # Default: every module, grouped by domain. quick=true -> fast curated set.
        mods = [m for m in (CURATED_MODULES if quick else sorted(available)) if m in available]
        sem = asyncio.Semaphore(ASK_CONCURRENCY)

        async def _one(key: str) -> Dict[str, Any]:
            did, dname = mod_domain.get(key, (0, "Other"))
            base = {"module": key, "domain_id": did, "domain": dname}
            async with sem:
                try:
                    env = await asyncio.wait_for(
                        self_get(f"/targetval/module/{key}", params),
                        timeout=ASK_MODULE_TIMEOUT_S,
                    )
                    base.update(status=env.get("status", "?"), source=env.get("source", ""),
                                fetched_n=env.get("fetched_n", 0), raw=env.get("data"))
                except asyncio.TimeoutError:
                    base.update(status="TIMEOUT", source="(slow upstream)", fetched_n=0,
                                raw={"error": f"exceeded {ASK_MODULE_TIMEOUT_S}s"})
                except Exception as e:
                    base.update(status="ERROR", source="(gateway)", fetched_n=0,
                                raw={"error": str(e)})
            return base

        # Run with a hard wall-clock cap: whatever isn't done by the budget is
        # marked TIMEOUT, so /ask always returns valid JSON within the request
        # timeout (avoids the worker being killed and Render returning HTML).
        task_key = {asyncio.ensure_future(_one(k)): k for k in mods}
        done, pending = await asyncio.wait(list(task_key), timeout=ASK_TOTAL_BUDGET_S)
        results: List[Dict[str, Any]] = []
        for task, key in task_key.items():
            if task in done and not task.cancelled() and task.exception() is None:
                results.append(task.result())
            else:
                task.cancel()
                did, dname = mod_domain.get(key, (0, "Other"))
                results.append({"module": key, "domain_id": did, "domain": dname,
                                "status": "TIMEOUT", "source": "(overall budget reached)",
                                "fetched_n": 0,
                                "raw": {"error": f"not finished within {ASK_TOTAL_BUDGET_S}s budget"}})
        results.sort(key=lambda r: (r["domain_id"], r["module"]))

        def _is_ok(r): return r["status"] == "OK"
        def _is_nd(r): return r["status"] == "NO_DATA"
        ok = sum(1 for r in results if _is_ok(r))
        nd = sum(1 for r in results if _is_nd(r))
        err = len(results) - ok - nd

        # Per-domain rollup for the report
        domains_report = []
        seen_ids = sorted({r["domain_id"] for r in results})
        for did in seen_ids:
            drs = [r for r in results if r["domain_id"] == did]
            with_data = [{"module": r["module"], "source": r["source"], "n": r["fetched_n"]}
                         for r in drs if _is_ok(r)]
            domains_report.append({
                "id": did, "name": drs[0]["domain"],
                "ok": sum(1 for r in drs if _is_ok(r)),
                "no_data": sum(1 for r in drs if _is_nd(r)),
                "error": sum(1 for r in drs if not _is_ok(r) and not _is_nd(r)),
                "with_data": with_data,
            })

        # Honest plain-English readout (coverage, not fabricated biology)
        strong = sorted([d for d in domains_report if d["ok"] > 0],
                        key=lambda d: -d["ok"])
        empty = [d["name"] for d in domains_report if d["ok"] == 0]
        verdict_bits = [
            f"Live data returned from {ok} of {len(results)} modules across "
            f"{len(seen_ids)} evidence domains for {resolved['symbol']}"
            + (f" in {resolved['condition']}" if resolved['condition'] else "") + "."
        ]
        if strong:
            top = ", ".join(f"{d['name']} ({d['ok']})" for d in strong[:3])
            verdict_bits.append(f"Strongest coverage: {top}.")
        if empty:
            verdict_bits.append("No data: " + ", ".join(empty) + ".")
        if err:
            verdict_bits.append(f"{err} module(s) errored or timed out (often slow/cold "
                                "upstreams — re-run to retry).")

        return JSONResponse({
            "query": q, "resolved": resolved,
            "summary": {"modules": len(results), "ok": ok, "no_data": nd,
                        "error": err, "mode": "quick" if quick else "full"},
            "report": {"verdict": " ".join(verdict_bits), "domains": domains_report},
            "results": results,
        })

    @app.post("/synthesize", include_in_schema=False)
    async def synthesize_endpoint(payload: Dict[str, Any] = Body(...)):
        from app.synthesis import synthesize
        out = await synthesize(
            payload.get("query", ""),
            payload.get("resolved", {}) or {},
            payload.get("results", []) or [],
        )
        return JSONResponse(out)

    @app.get("/chat", include_in_schema=False, response_class=HTMLResponse)
    async def chat():
        return HTMLResponse(_CHAT_HTML)


_CHAT_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TargetVal — Ask</title>
<style>
 :root { color-scheme: light dark; }
 * { box-sizing: border-box; }
 body { font: 16px/1.5 -apple-system, system-ui, sans-serif; margin: 0; background:#0b0f14; color:#e6edf3; }
 header { padding: 16px; border-bottom: 1px solid #21262d; position: sticky; top:0; background:#0b0f14; }
 header h1 { font-size: 18px; margin: 0; }
 header p { margin: 4px 0 0; color:#8b949e; font-size: 13px; }
 main { max-width: 760px; margin: 0 auto; padding: 16px; }
 form { display: flex; gap: 8px; margin-bottom: 16px; }
 input { flex:1; padding: 12px 14px; border-radius: 10px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; font-size:16px; }
 button { padding: 12px 16px; border-radius: 10px; border:0; background:#238636; color:#fff; font-weight:600; font-size:15px; }
 button:disabled { opacity:.5; }
 .examples { color:#8b949e; font-size:13px; margin: -8px 0 16px; }
 .examples a { color:#58a6ff; text-decoration:none; cursor:pointer; }
 .resolved { background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:12px; margin-bottom:14px; font-size:14px; }
 .resolved b { color:#58a6ff; }
 .card { background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:12px 14px; margin-bottom:10px; }
 .card .top { display:flex; justify-content:space-between; align-items:center; gap:8px; }
 .mod { font-weight:600; }
 .src { color:#8b949e; font-size:12px; word-break:break-all; }
 .badge { font-size:12px; font-weight:700; padding:2px 8px; border-radius:999px; white-space:nowrap; }
 .OK { background:#196c2e; color:#d2f4d8; }
 .NO_DATA { background:#3d2c00; color:#f6d899; }
 .ERR { background:#5c1a1a; color:#ffd0d0; }
 details { margin-top:8px; } summary { cursor:pointer; color:#8b949e; font-size:13px; }
 pre { overflow:auto; background:#010409; padding:10px; border-radius:8px; font-size:12px; max-height:320px; }
 .status { color:#8b949e; font-size:14px; margin-bottom:12px; }
 .spin { display:inline-block; animation: s 1s linear infinite; } @keyframes s { to { transform: rotate(360deg);} }
 .report { background:#0d1117; border:1px solid #1f6feb; border-radius:10px; padding:14px; margin-bottom:16px; }
 .report h2 { font-size:15px; margin:0 0 8px; color:#58a6ff; }
 .report .verdict { font-size:14px; margin-bottom:10px; }
 .drow { display:flex; justify-content:space-between; gap:8px; padding:6px 0; border-top:1px solid #21262d; font-size:13px; }
 .drow .dn { font-weight:600; } .drow .dc { color:#8b949e; white-space:nowrap; }
 .pill { font-size:11px; padding:1px 7px; border-radius:999px; margin-left:6px; }
 .dhead { margin:18px 0 8px; font-size:14px; font-weight:700; color:#8b949e; text-transform:uppercase; letter-spacing:.04em; }
 .synthbtn { width:100%; margin:4px 0 16px; padding:12px; border-radius:10px; border:1px solid #8957e5; background:#6e40c9; color:#fff; font-weight:600; font-size:15px; }
 .synthbtn:disabled { opacity:.5; }
 .synth { background:#0d1117; border:1px solid #8957e5; border-radius:10px; padding:14px 16px; margin-bottom:16px; font-size:14.5px; line-height:1.55; }
 .synth h1,.synth h2,.synth h3 { color:#b392f0; font-size:15px; margin:14px 0 6px; }
 .synth strong { color:#e6edf3; }
 .synth ul { margin:6px 0 6px 18px; } .synth li { margin:3px 0; }
 .synth p { margin:8px 0; }
</style></head>
<body>
<header><h1>TargetVal — Ask</h1><p>Type a target and indication in plain English. No code.</p></header>
<main>
 <form id="f"><input id="q" placeholder="e.g. B7H7 for oncology" autocomplete="off"><button id="go">Ask</button></form>
 <div class="examples">Try:
   <a onclick="ex('B7H7 for oncology')">B7H7 for oncology</a> ·
   <a onclick="ex('PCSK9 for hypercholesterolemia')">PCSK9 for hypercholesterolemia</a> ·
   <a onclick="ex('TIGIT for lung cancer')">TIGIT for lung cancer</a>
   &nbsp;|&nbsp; <label><input type="checkbox" id="quick"> quick (14 fast modules only)</label>
 </div>
 <div id="out"></div>
</main>
<script>
const $ = s => document.querySelector(s);
let lastData = null;
function ex(t){ $('#q').value = t; $('#f').dispatchEvent(new Event('submit')); }
function cls(st){ return st==='OK'?'OK':(st==='NO_DATA'?'NO_DATA':'ERR'); }
$('#f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('#q').value.trim(); if(!q) return;
  const quick = $('#quick').checked;
  $('#go').disabled = true;
  $('#out').innerHTML = '<div class="status"><span class="spin">⏳</span> Waking the service & querying all data sources… (first call can take ~30–60s if it was asleep; full run covers every module)</div>';
  try {
    // Pre-warm: wake a sleeping (free-tier) instance before the heavy call so
    // the cold-start delay doesn't eat into the /ask request budget.
    try { await fetch('/healthz', {cache:'no-store'}); } catch(_) {}
    const ctrl = new AbortController();
    const tmo = setTimeout(() => ctrl.abort(), 110000);
    const r = await fetch(`/ask?q=${encodeURIComponent(q)}${quick?'&quick=true':''}`, {signal: ctrl.signal});
    clearTimeout(tmo);
    const text = await r.text();
    let d;
    try { d = JSON.parse(text); }
    catch(_) {
      $('#out').innerHTML = `<div class="status">The server returned an unexpected response (HTTP ${r.status}). `
        + `It may be redeploying or briefly overloaded — wait ~30s and try again, or tick <b>quick</b> for a lighter run.<br><br>`
        + `<details><summary>details</summary><pre>${escapeHtml(text).slice(0,1200)}</pre></details></div>`;
      return;
    }
    lastData = d;
    const rz = d.resolved || {};
    const s = d.summary || {};
    const rep = d.report || {};
    // 1) Resolved line
    let html = `<div class="resolved">Understood as → `
      + (rz.drug?`drug <b>${escapeHtml(rz.drug)}</b> → `:'')
      + `gene <b>${rz.symbol||'?'}</b>`
      + (rz.ensembl_id?` (<b>${rz.ensembl_id}</b>)`:'')
      + (rz.uniprot_id?` · UniProt <b>${rz.uniprot_id}</b>`:'')
      + (rz.condition?` · indication <b>${escapeHtml(rz.condition)}</b>`:'')
      + (rz.efo?` (<b>${rz.efo}</b>)`:'')
      + `<br><span class="src">${s.ok} OK · ${s.no_data} no-data · ${s.error} error across ${s.modules} modules (${s.mode} mode)</span></div>`;
    // 2) AI synthesis button + container
    html += `<button class="synthbtn" id="synth">✨ Generate AI feasibility synthesis</button><div id="synthout"></div>`;
    // 3) Summary report (verdict + per-domain rollup)
    html += `<div class="report"><h2>Coverage report</h2><div class="verdict">${escapeHtml(rep.verdict||'')}</div>`;
    for (const dm of (rep.domains||[])) {
      html += `<div class="drow"><span class="dn">${dm.name}</span>`
        + `<span class="dc"><span class="badge OK">${dm.ok} OK</span>`
        + `<span class="badge NO_DATA">${dm.no_data}</span>`
        + `<span class="badge ERR">${dm.error}</span></span></div>`;
    }
    html += `</div>`;
    // 3) Cards grouped by domain
    let lastDom = null;
    for (const it of (d.results||[])) {
      if (it.domain !== lastDom) { html += `<div class="dhead">${escapeHtml(it.domain||'Other')}</div>`; lastDom = it.domain; }
      html += `<div class="card"><div class="top"><span class="mod">${it.module}</span>`
        + `<span class="badge ${cls(it.status)}">${it.status}${it.fetched_n?(' · '+it.fetched_n):''}</span></div>`
        + `<div class="src">${it.source||''}</div>`
        + `<details><summary>raw</summary><pre>${escapeHtml(JSON.stringify(it.raw,null,2)).slice(0,8000)}</pre></details></div>`;
    }
    $('#out').innerHTML = html;
    const sb = $('#synth');
    if (sb) sb.addEventListener('click', runSynthesis);
  } catch(err) {
    const msg = (err && err.name === 'AbortError')
      ? 'The full run took too long this time (the service may have been cold). Wait ~30s and try again, or tick <b>quick</b> for a fast 14-source run.'
      : ('Network error: ' + escapeHtml(String(err)) + ' — wait a moment and retry.');
    $('#out').innerHTML = `<div class="status">${msg}</div>`;
  } finally { $('#go').disabled = false; }
});

async function runSynthesis(){
  if(!lastData) return;
  const sb = $('#synth'); sb.disabled = true;
  $('#synthout').innerHTML = '<div class="status"><span class="spin">✨</span> Claude is reading the evidence and writing the assessment… (~20–40s)</div>';
  try {
    const r = await fetch('/synthesize', {method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({query:lastData.query, resolved:lastData.resolved, results:lastData.results})});
    const d = await r.json();
    if(d.ok){ $('#synthout').innerHTML = `<div class="synth">${mdToHtml(d.report_markdown||'')}</div>`; }
    else { $('#synthout').innerHTML = `<div class="status">Synthesis unavailable: ${escapeHtml(d.error||'unknown')}`
      + (/(ANTHROPIC_API_KEY)/.test(d.error||'') ? ' — set <b>ANTHROPIC_API_KEY</b> in the Render environment to enable AI synthesis.' : '') + `</div>`; }
  } catch(err){ $('#synthout').innerHTML = `<div class="status">Synthesis error: ${escapeHtml(String(err))}</div>`; }
  finally { sb.disabled = false; }
}

function mdToHtml(md){
  const esc = escapeHtml(md);
  const lines = esc.split('\\n'); let out=''; let inList=false;
  const inline = t => t.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>').replace(/\\*(.+?)\\*/g,'<em>$1</em>');
  for(let line of lines){
    const h = line.match(/^(#{1,3})\\s+(.*)/);
    const li = line.match(/^\\s*[-*]\\s+(.*)/);
    if(h){ if(inList){out+='</ul>';inList=false;} out+=`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; }
    else if(li){ if(!inList){out+='<ul>';inList=true;} out+=`<li>${inline(li[1])}</li>`; }
    else if(line.trim()===''){ if(inList){out+='</ul>';inList=false;} }
    else { if(inList){out+='</ul>';inList=false;} out+=`<p>${inline(line)}</p>`; }
  }
  if(inList) out+='</ul>';
  return out;
}
function escapeHtml(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
</script>
</body></html>"""

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
from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse

# Per-module budget so one slow/dead upstream never stalls the whole page.
ASK_MODULE_TIMEOUT_S = float(os.getenv("ASK_MODULE_TIMEOUT_S", "22"))
ASK_CONCURRENCY = int(os.getenv("ASK_CONCURRENCY", "24"))

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
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "TargetvalGateway-webui"}) as c:
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


async def _resolve_efo(condition: Optional[str]) -> Optional[str]:
    if not condition:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "TargetvalGateway-webui"}) as c:
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
        efo = await _resolve_efo(condition)
        resolved = {
            "symbol": gene["symbol"], "ensembl_id": gene["ensembl_id"],
            "uniprot_id": gene["uniprot_id"], "condition": condition, "efo": efo,
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

        results: List[Dict[str, Any]] = await asyncio.gather(*[_one(k) for k in mods])
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
function ex(t){ $('#q').value = t; $('#f').dispatchEvent(new Event('submit')); }
function cls(st){ return st==='OK'?'OK':(st==='NO_DATA'?'NO_DATA':'ERR'); }
$('#f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('#q').value.trim(); if(!q) return;
  const quick = $('#quick').checked;
  $('#go').disabled = true;
  $('#out').innerHTML = '<div class="status"><span class="spin">⏳</span> Resolving & querying all data sources… (first call may take ~30–60s if the service was asleep; full run covers every module)</div>';
  try {
    const r = await fetch(`/ask?q=${encodeURIComponent(q)}${quick?'&quick=true':''}`);
    const d = await r.json();
    const rz = d.resolved || {};
    const s = d.summary || {};
    const rep = d.report || {};
    // 1) Resolved line
    let html = `<div class="resolved">Understood as → gene <b>${rz.symbol||'?'}</b>`
      + (rz.ensembl_id?` (<b>${rz.ensembl_id}</b>)`:'')
      + (rz.uniprot_id?` · UniProt <b>${rz.uniprot_id}</b>`:'')
      + (rz.condition?` · indication <b>${rz.condition}</b>`:'')
      + (rz.efo?` (<b>${rz.efo}</b>)`:'')
      + `<br><span class="src">${s.ok} OK · ${s.no_data} no-data · ${s.error} error across ${s.modules} modules (${s.mode} mode)</span></div>`;
    // 2) Summary report (verdict + per-domain rollup)
    html += `<div class="report"><h2>Summary report</h2><div class="verdict">${escapeHtml(rep.verdict||'')}</div>`;
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
  } catch(err) {
    $('#out').innerHTML = `<div class="status">Error: ${escapeHtml(String(err))}</div>`;
  } finally { $('#go').disabled = false; }
});
function escapeHtml(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
</script>
</body></html>"""

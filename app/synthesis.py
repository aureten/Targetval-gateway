# app/synthesis.py
# Evidence-grounded synthesis of a target-feasibility run.
#
# Two engines:
#   * rule-based (default) — deterministic reasoning over what the modules
#     actually returned (which sources fired, counts, modality signals). No key,
#     no network, instant. Grounded only in fetched evidence.
#   * llm (optional) — if SYNTH_ENGINE=llm AND ANTHROPIC_API_KEY is set, hands the
#     evidence digest to Claude for a fluent narrative. Falls back to rule-based
#     on any error or missing key.
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

SYNTH_ENGINE = os.getenv("SYNTH_ENGINE", "rule").lower()  # "rule" | "llm"
SYNTH_MODEL = os.getenv("SYNTH_MODEL", "claude-opus-4-8")
SYNTH_MAX_TOKENS = int(os.getenv("SYNTH_MAX_TOKENS", "6000"))
_PER_MODULE_RAW_CHARS = int(os.getenv("SYNTH_PER_MODULE_CHARS", "1200"))

# Domains in report order
_DOMAIN_ORDER = [
    "Genetics & Causal Variants",
    "Expression & Localization",
    "Association & Omics",
    "Biology, Mechanism & Perturbation",
    "Clinical, Safety & Tractability",
    "Competitive & IP",
]


# ---------------------------------------------------------------------------
# Rule-based engine
# ---------------------------------------------------------------------------
def _status_map(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r.get("module"): r for r in results}


def _ok(sm: Dict[str, Dict[str, Any]], key: str) -> bool:
    return (sm.get(key) or {}).get("status") == "OK"


def _domain_counts(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for r in results:
        d = out.setdefault(r.get("domain", "Other"), {"OK": 0, "NO_DATA": 0, "ERR": 0})
        st = r.get("status")
        if st == "OK":
            d["OK"] += 1
        elif st == "NO_DATA":
            d["NO_DATA"] += 1
        else:
            d["ERR"] += 1
    return out


def _modality_read(sm: Dict[str, Dict[str, Any]]) -> str:
    surface = _ok(sm, "tract-surfaceome")
    ab = _ok(sm, "tract-ligandability-ab")
    sm_lig = _ok(sm, "tract-ligandability-sm")
    oligo = _ok(sm, "tract-ligandability-oligo")
    if surface or ab:
        base = ("Cell-surface / antibody-accessible target — biologics (mAb, bispecific, "
                "ADC, CAR) are the natural modality")
        if sm_lig:
            base += "; a small-molecule pocket may also exist"
        return base + "."
    if sm_lig:
        return "Small-molecule ligandability signal present — SM is a plausible modality."
    if oligo:
        return "Oligonucleotide tractability signal — ASO/siRNA may apply where delivery is solved."
    return "Modality is not yet defined by the tractability sources that returned data."


def _genetics_read(sm: Dict[str, Dict[str, Any]], dc: Dict[str, int]) -> str:
    strong = _ok(sm, "genetics-l2g") or _ok(sm, "genetics-coloc") or _ok(sm, "genetics-pqtl")
    if dc["OK"] == 0:
        return ("Genetics returned no live data. For an expression- or immune-driven target "
                "this is expected and not disqualifying — weight expression, mechanism, and "
                "clinical evidence instead.")
    if strong:
        return (f"Genetic association evidence returned from {dc['OK']} source(s), including "
                "Open Targets target–disease links — supportive of a causal role for the pathway.")
    return (f"{dc['OK']} genetics source(s) returned data, but the core association sources "
            "(L2G/coloc/pQTL) did not — treat the genetic case as partial.")


def _clinical_read(sm: Dict[str, Dict[str, Any]], dc: Dict[str, int]) -> str:
    pipeline = _ok(sm, "clin-pipeline") or _ok(sm, "clin-endpoints") or _ok(sm, "clin-feasibility")
    drugs = _ok(sm, "tract-drugs")
    bits = []
    if pipeline:
        bits.append("clinical-trial activity is registered for this target/indication "
                    "(a validation and competitive signal)")
    if drugs:
        bits.append("known drugs/chemical matter exist against the target")
    if not bits:
        bits.append("no clinical or known-drug evidence returned from the sources that responded")
    return (f"{dc['OK']} source(s) returned data; " + "; ".join(bits) + ".")


def _generic_read(name: str, dc: Dict[str, int]) -> str:
    if dc["OK"] == 0 and dc["NO_DATA"] == 0:
        return f"No live data returned ({dc['ERR']} source(s) errored/timed out — re-run to retry)."
    if dc["OK"] == 0:
        return "Sources responded but found no records for this target."
    return f"{dc['OK']} source(s) returned data."


def synthesize_rule_based(query: str, resolved: Dict[str, Any],
                          results: List[Dict[str, Any]]) -> Dict[str, Any]:
    sm = _status_map(results)
    counts = _domain_counts(results)
    sym = resolved.get("symbol") or "the target"
    cond = resolved.get("condition") or "the indication"
    drug = resolved.get("drug")
    total = len(results)
    ok = sum(1 for r in results if r.get("status") == "OK")
    err = sum(1 for r in results if r.get("status") not in ("OK", "NO_DATA"))

    gen = counts.get("Genetics & Causal Variants", {"OK": 0, "NO_DATA": 0, "ERR": 0})
    clin = counts.get("Clinical, Safety & Tractability", {"OK": 0, "NO_DATA": 0, "ERR": 0})
    comp = counts.get("Competitive & IP", {"OK": 0, "NO_DATA": 0, "ERR": 0})

    modality = _modality_read(sm)
    # Verdict
    strengths = sorted(
        [(d, c["OK"]) for d, c in counts.items() if c["OK"] > 0],
        key=lambda x: -x[1])
    top = ", ".join(f"{d.split(' &')[0].split(',')[0]} ({n})" for d, n in strengths[:3]) or "none"
    lead = f"**{sym}** for **{cond}**" + (f" (resolved from drug *{drug}*)" if drug else "")
    verdict = (
        f"Live evidence returned from **{ok} of {total}** modules across "
        f"{len([c for c in counts.values() if c['OK']>0])} domains for {lead}. "
        f"Strongest coverage: {top}. {modality} "
    )
    if _ok(sm, "clin-pipeline") or _ok(sm, "clin-endpoints"):
        verdict += "Existing clinical activity suggests the target is already being pursued — differentiation will matter. "
    if gen["OK"] == 0:
        verdict += "The biggest open question is mechanistic, not genetic (genetics is quiet here)."
    elif not (_ok(sm, "tract-surfaceome") or _ok(sm, "tract-ligandability-ab") or _ok(sm, "tract-ligandability-sm")):
        verdict += "The biggest open question is **modality** — tractability sources did not confirm a clear drugging route."
    else:
        verdict += "Biology and tractability are supported; the open questions are differentiation and on-target safety."

    # Per-domain
    domain_lines = []
    for d in _DOMAIN_ORDER:
        c = counts.get(d, {"OK": 0, "NO_DATA": 0, "ERR": 0})
        if d.startswith("Genetics"):
            txt = _genetics_read(sm, c)
        elif d.startswith("Clinical"):
            txt = _clinical_read(sm, c) + " " + modality
        else:
            txt = _generic_read(d, c)
        domain_lines.append(f"- **{d}** — {txt} _({c['OK']} OK / {c['NO_DATA']} no-data / {c['ERR']} error)_")

    # Risks (derived from evidence)
    risks = []
    if gen["OK"] == 0:
        risks.append("**Causality** — no live human-genetic support returned; confirm the target drives disease, not just correlates.")
    if _ok(sm, "tract-surfaceome") or _ok(sm, "tract-ligandability-ab"):
        risks.append("**On-target safety / normal-tissue expression** — for a surface biologic target, define the therapeutic window vs healthy tissue.")
    if not (_ok(sm, "tract-ligandability-sm") or _ok(sm, "tract-surfaceome") or _ok(sm, "tract-ligandability-ab")):
        risks.append("**Druggability** — no clear small-molecule or surface tractability signal; the modality is unproven.")
    if _ok(sm, "clin-pipeline") or _ok(sm, "comp-intensity"):
        risks.append("**Competition** — clinical/IP activity indicates others are pursuing this space.")
    if err >= total // 3:
        risks.append(f"**Evidence gaps** — {err} sources errored/timed out this run; re-run (and add BioGRID/OpenGWAS keys) before relying on absence as a negative.")
    if not risks:
        risks.append("No dominant risk flagged by the returned evidence; deepen the analysis on the empty domains.")

    # Next experiments (derived from gaps)
    nexts = []
    if gen["OK"] == 0:
        nexts.append("Run a human-genetics deep dive (Open Targets, gnomAD, OpenGWAS with a token) to test causal support.")
    if _ok(sm, "expr-baseline") or _ok(sm, "expr-localization"):
        nexts.append("Quantify tumor-vs-normal expression / single-cell localization to size the therapeutic window.")
    else:
        nexts.append("Establish baseline and disease-tissue expression (GTEx, HPA, single-cell).")
    if _ok(sm, "tract-surfaceome") or _ok(sm, "tract-ligandability-ab"):
        nexts.append("Confirm surface accessibility and epitope/shedding behavior to lock the biologic modality.")
    if _ok(sm, "clin-pipeline"):
        nexts.append("Map the competitive pipeline and define a differentiation thesis (selectivity, indication, tolerability).")
    nexts = nexts[:4] or ["Broaden source coverage and re-run before committing."]

    md = []
    md.append("## Verdict")
    md.append(verdict)
    md.append("## By evidence domain")
    md.extend(domain_lines)
    md.append("## Key risks")
    md.extend(f"- {r}" for r in risks)
    md.append("## Recommended next experiments")
    md.extend(f"- {n}" for n in nexts)
    md.append("")
    md.append("_Rule-based synthesis grounded in which sources returned data for this run. "
              "For a fluent, literature-aware narrative, enable the LLM engine "
              "(SYNTH_ENGINE=llm + ANTHROPIC_API_KEY)._")
    return {"ok": True, "report_markdown": "\n".join(md), "engine": "rule-based"}


# ---------------------------------------------------------------------------
# LLM engine (optional)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a drug-target validation analyst. You are given the \
evidence a federation of ~74 public bioinformatics sources returned for a \
specific target gene and indication. Write a concise, decision-oriented \
feasibility assessment grounded only in the supplied evidence. When a domain \
returned no data, say so plainly rather than inventing findings. Structure as \
markdown: Verdict, By evidence domain, Key risks, Recommended next experiments."""


def _digest(results: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for r in results:
        status = r.get("status", "?")
        lines.append(f"### {r.get('module')} [{r.get('domain','?')}] status={status} source={r.get('source','')}")
        if status == "OK":
            try:
                blob = json.dumps(r.get("raw"), separators=(",", ":"), default=str)
            except Exception:
                blob = str(r.get("raw"))
            if blob and blob not in ("null", "{}", "[]"):
                lines.append(blob[:_PER_MODULE_RAW_CHARS])
    return "\n".join(lines)


async def _synthesize_llm(query: str, resolved: Dict[str, Any],
                          results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import AsyncAnthropic
    except Exception:
        return None
    ok = sum(1 for r in results if r.get("status") == "OK")
    user_content = (
        f"TARGET: {resolved.get('symbol')} (Ensembl {resolved.get('ensembl_id')}, "
        f"UniProt {resolved.get('uniprot_id')})\nINDICATION: {resolved.get('condition')} "
        f"(EFO {resolved.get('efo')})\nQUERY: {query}\nCOVERAGE: {ok} of {len(results)} modules "
        f"returned live data.\n\nEVIDENCE DIGEST:\n{_digest(results)}"
    )
    client = AsyncAnthropic()
    base = dict(model=SYNTH_MODEL, max_tokens=SYNTH_MAX_TOKENS, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}])
    try:
        try:
            msg = await client.messages.create(thinking={"type": "adaptive"},
                                                output_config={"effort": "high"}, **base)
        except TypeError:
            msg = await client.messages.create(**base)
    except Exception:
        return None
    if getattr(msg, "stop_reason", None) == "refusal":
        return None
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    if not text.strip():
        return None
    return {"ok": True, "report_markdown": text, "engine": f"llm:{SYNTH_MODEL}"}


async def synthesize(query: str, resolved: Dict[str, Any],
                     results: List[Dict[str, Any]]) -> Dict[str, Any]:
    # LLM only when explicitly enabled and a key is present; otherwise (and on any
    # failure) fall back to the deterministic engine so the button always works.
    if SYNTH_ENGINE == "llm":
        out = await _synthesize_llm(query, resolved, results)
        if out:
            return out
    return synthesize_rule_based(query, resolved, results)

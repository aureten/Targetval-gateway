# app/synthesis.py
# LLM-powered synthesis layer: turns the raw evidence the gateway collected
# (module envelopes + which sources returned data) into a grounded, readable
# target-feasibility assessment using the Claude API.
#
# Activated only when ANTHROPIC_API_KEY is set; otherwise the caller falls back
# to the deterministic coverage report.
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

SYNTH_MODEL = os.getenv("SYNTH_MODEL", "claude-opus-4-8")
SYNTH_MAX_TOKENS = int(os.getenv("SYNTH_MAX_TOKENS", "6000"))
# Cap how much raw payload per module we hand the model, to bound input tokens.
_PER_MODULE_RAW_CHARS = int(os.getenv("SYNTH_PER_MODULE_CHARS", "1200"))

SYSTEM_PROMPT = """You are a drug-target validation analyst. You are given the \
evidence a federation of ~74 public bioinformatics sources returned for a \
specific target gene and indication. Write a concise, decision-oriented \
feasibility assessment.

Ground every claim in the supplied evidence. When a domain returned no data, \
say so plainly rather than inventing findings — absent genetics evidence for an \
immuno-oncology target, for example, is expected, not disqualifying. Do not \
fabricate citations, numbers, or mechanisms that are not supported by the \
evidence digest.

Structure the report as markdown:
- **Verdict** — one short paragraph: is this a credible target for the \
indication, and the single biggest open question.
- **By evidence domain** — Genetics, Expression & Localization, Association & \
Omics, Biology/Mechanism, Clinical/Safety/Tractability, Competitive & IP. For \
each, 1-2 sentences on what the returned data supports or leaves open.
- **Key risks** — the 2-4 things most likely to kill the program.
- **Recommended next experiments** — 2-4 concrete, prioritized.

Be honest about evidence gaps and source failures. Keep it tight — a busy \
scientist should grasp the picture in under two minutes."""


def _digest(results: List[Dict[str, Any]]) -> str:
    """Compact, token-bounded evidence digest grouped by module."""
    lines: List[str] = []
    for r in results:
        status = r.get("status", "?")
        head = f"### {r.get('module')} [{r.get('domain','?')}] status={status} source={r.get('source','')}"
        lines.append(head)
        if status == "OK":
            raw = r.get("raw")
            try:
                blob = json.dumps(raw, separators=(",", ":"), default=str)
            except Exception:
                blob = str(raw)
            if blob and blob not in ("null", "{}", "[]"):
                lines.append(blob[:_PER_MODULE_RAW_CHARS])
    return "\n".join(lines)


async def synthesize(query: str, resolved: Dict[str, Any],
                     results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not configured on the server.",
                "report_markdown": None}
    try:
        from anthropic import AsyncAnthropic
    except Exception as e:  # package missing
        return {"ok": False, "error": f"anthropic SDK unavailable: {e}", "report_markdown": None}

    ok = sum(1 for r in results if r.get("status") == "OK")
    user_content = (
        f"TARGET: {resolved.get('symbol')} "
        f"(Ensembl {resolved.get('ensembl_id')}, UniProt {resolved.get('uniprot_id')})\n"
        f"INDICATION: {resolved.get('condition')} (EFO {resolved.get('efo')})\n"
        f"ORIGINAL QUERY: {query}\n"
        f"COVERAGE: {ok} of {len(results)} modules returned live data.\n\n"
        f"EVIDENCE DIGEST (only modules that returned data are expanded):\n{_digest(results)}"
    )

    client = AsyncAnthropic()
    base_kwargs = dict(
        model=SYNTH_MODEL,
        max_tokens=SYNTH_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    try:
        # Prefer adaptive thinking + high effort (Opus 4.8); fall back if the
        # installed SDK version doesn't accept those kwargs.
        try:
            msg = await client.messages.create(
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                **base_kwargs,
            )
        except TypeError:
            msg = await client.messages.create(**base_kwargs)
    except Exception as e:
        return {"ok": False, "error": f"Claude API error: {e}", "report_markdown": None}

    if getattr(msg, "stop_reason", None) == "refusal":
        return {"ok": False, "error": "The model declined to answer this request.",
                "report_markdown": None}
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return {"ok": True, "report_markdown": text, "model": SYNTH_MODEL}

#!/usr/bin/env python3
"""Read-only live audit of TargetVal03 from a mechanistic-biologist perspective.

Submits ten adversarial target-disease pairs to the public validation endpoint,
preserves every complete response, and writes compact mechanistic summaries.
The script never writes to TargetVal.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("TARGETVAL_BASE_URL", "https://targetval-api.onrender.com").rstrip("/")
OUT = Path(os.environ.get("AUDIT_OUT", "audit_results/mechanistic"))
FULL = OUT / "full"

CASES: list[dict[str, str]] = [
    {
        "id": "01_gdf15_obesity",
        "gene": "GDF15",
        "disease": "obesity",
        "stress": "same target must resolve to agonize/harness GFRAL aversive-metabolic signaling, not infer sign from elevated disease levels",
    },
    {
        "id": "02_gdf15_cancer_cachexia",
        "gene": "GDF15",
        "disease": "cancer cachexia",
        "stress": "same target, opposite therapeutic sign: block GDF15-GFRAL drive in cachexia; resolver and disease-context polarity",
    },
    {
        "id": "03_fgf21_mash",
        "gene": "FGF21",
        "disease": "metabolic dysfunction-associated steatohepatitis",
        "stress": "endogenous elevation can be compensatory/insufficient; agonism despite positive disease correlation; co-receptor and tissue-state biology",
    },
    {
        "id": "04_il10_ibd",
        "gene": "IL10",
        "disease": "inflammatory bowel disease",
        "stress": "protective cytokine biology but delivery-, compartment- and cell-state-dependent translation; systemic exposure is not equivalent to local mechanism",
    },
    {
        "id": "05_hhla2_nsclc",
        "gene": "HHLA2",
        "disease": "non-small cell lung cancer",
        "stress": "one ligand engages inhibitory KIR3DL3 and costimulatory TMIGD2; network polarity and epitope/arm-selection must remain explicit",
    },
    {
        "id": "06_flt1_preeclampsia",
        "gene": "FLT1",
        "disease": "preeclampsia",
        "stress": "placental soluble sFLT1 isoforms versus full-length receptor, maternal-fetal compartment and gestational timing",
    },
    {
        "id": "07_col1a1_oi",
        "gene": "COL1A1",
        "disease": "osteogenesis imperfecta",
        "stress": "haploinsufficiency versus dominant-negative collagen incorporation; causal gene does not imply whole-gene inhibition",
    },
    {
        "id": "08_trem2_ad",
        "gene": "TREM2",
        "disease": "Alzheimer disease",
        "stress": "microglial state and disease-stage dependence; strong genetics versus target-engaged clinical outcome failure",
    },
    {
        "id": "09_brd9_synovial_sarcoma",
        "gene": "BRD9",
        "disease": "synovial sarcoma",
        "stress": "SS18-SSX/GBAF complex dependency, degradation versus residual/adaptive complex activity, target-versus-operation failure",
    },
    {
        "id": "10_kras_pancreatic",
        "gene": "KRAS",
        "disease": "pancreatic cancer",
        "stress": "allele/state-specific signaling, cytosolic plasma-membrane topology, feedback/adaptation and current human translation",
    },
]

RETRYABLE = {429, 500, 502, 503, 504}


def request_case(case: dict[str, str]) -> dict[str, Any]:
    payload = json.dumps({
        "gene_symbol": case["gene"],
        "disease_name": case["disease"],
        "mode": "SETTLE",
    }).encode("utf-8")
    req_url = f"{BASE_URL}/api/v1/validate"
    attempts: list[dict[str, Any]] = []
    start_all = time.monotonic()

    for attempt in range(1, 5):
        start = time.monotonic()
        req = urllib.request.Request(
            req_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TargetVal-mechanistic-biologist-audit/2026-07-17",
            },
        )
        status: int | None = None
        body = ""
        error = ""
        try:
            with urllib.request.urlopen(req, timeout=1200) as resp:
                status = int(resp.status)
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read().decode("utf-8", errors="replace")
            error = f"HTTPError: {exc}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        elapsed = round(time.monotonic() - start, 3)
        attempts.append({
            "attempt": attempt,
            "status": status,
            "elapsed_s": elapsed,
            "error": error,
            "body_prefix": body[:800],
        })

        if status == 200:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                error = f"JSONDecodeError: {exc}"
            else:
                return {
                    "ok": True,
                    "http_status": status,
                    "elapsed_total_s": round(time.monotonic() - start_all, 3),
                    "attempts": attempts,
                    "response": parsed,
                }

        if (status in RETRYABLE or status is None) and attempt < 4:
            delay = 25 * attempt
            print(f"[{case['id']}] attempt {attempt} failed ({status or error}); retry in {delay}s", flush=True)
            time.sleep(delay)
            continue
        break

    return {
        "ok": False,
        "http_status": attempts[-1].get("status") if attempts else None,
        "elapsed_total_s": round(time.monotonic() - start_all, 3),
        "attempts": attempts,
        "error": attempts[-1].get("error") if attempts else "no attempt",
        "response_body_prefix": attempts[-1].get("body_prefix") if attempts else "",
    }


def cell_map(response: dict[str, Any]) -> dict[str, Any]:
    cells = ((response.get("domain_report") or {}).get("drug_hunter") or [])
    out: dict[str, Any] = {}
    for c in cells:
        key = str(c.get("key") or c.get("title") or "unknown")
        out[key] = {
            "title": c.get("title"),
            "status": c.get("status"),
            "verdict": c.get("verdict"),
            "verdict_label": c.get("verdict_label"),
            "confidence": c.get("confidence"),
            "basis": c.get("basis"),
            "narrative": c.get("narrative"),
            "needs": c.get("needs"),
            "meta": c.get("meta"),
        }
    return out


def summarize(case: dict[str, str], result: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        **case,
        "ok": result.get("ok", False),
        "http_status": result.get("http_status"),
        "elapsed_total_s": result.get("elapsed_total_s"),
        "attempts": result.get("attempts"),
    }
    if not result.get("ok"):
        row["error"] = result.get("error") or result.get("response_body_prefix")
        return row

    r = result["response"]
    dr = r.get("domain_report") or {}
    card = r.get("card") or {}
    cells = cell_map(r)
    fetch = r.get("fetch_report") or {}
    status_counts: dict[str, int] = {}
    for value in fetch.values():
        if isinstance(value, dict):
            status = str(value.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

    row.update({
        "stamp": r.get("stamp"),
        "cached": r.get("cached"),
        "resolved": r.get("resolved"),
        "decision_header": dr.get("decision_header") or {},
        "card": {
            "card_class": card.get("card_class"),
            "conclusion": card.get("conclusion"),
            "path_quality": card.get("path_quality"),
            "has_causal_path": card.get("has_causal_path"),
            "mechanism_coverage": card.get("mechanism_coverage"),
            "direction_confidence": card.get("direction_confidence"),
            "effective_independent_count": card.get("effective_independent_count"),
            "tier_distribution": card.get("tier_distribution"),
        },
        "mechanism": dr.get("mechanism"),
        "phenotype_outcome_synthesis": dr.get("phenotype_outcome_synthesis"),
        "relational_synthesis": dr.get("relational_synthesis"),
        "functional_analog": dr.get("functional_analog"),
        "family_analog": dr.get("family_analog"),
        "modality_profile": dr.get("modality_profile"),
        "domain_resolution": dr.get("domain_resolution"),
        "epitope_role": dr.get("epitope_role"),
        "drug_hunter_cells": cells,
        "convergence_provenance": r.get("convergence_provenance"),
        "n_claims": r.get("n_claims"),
        "fetch_status_counts": status_counts,
        "report_markdown": r.get("report_markdown"),
        "literature": r.get("literature"),
    })
    return row


def cell_label(cells: dict[str, Any], key: str) -> str:
    c = cells.get(key) or {}
    return str(c.get("verdict_label") or c.get("verdict") or c.get("status") or "—")


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# TargetVal03 live mechanistic-biologist audit",
        "",
        f"Endpoint: `{BASE_URL}/api/v1/validate`",
        "",
        "| Case | HTTP | s | Verdict | Direction | Lesion / allelic mechanism | Causal mechanism | Network / partners | Perturbation | Molecular disease | Engagement |",
        "|---|---:|---:|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.get("ok"):
            lines.append(f"| {row['gene']} — {row['disease']} | {row.get('http_status') or '—'} | {row.get('elapsed_total_s') or '—'} | **NO RESULT** | — | — | — | — | — | — | — |")
            continue
        cells = row.get("drug_hunter_cells") or {}
        hdr = row.get("decision_header") or {}
        lesion = cell_label(cells, "lesion_type")
        if lesion == "—":
            lesion = cell_label(cells, "allelic_series")
        causal = cell_label(cells, "causal_mechanism")
        if causal == "—":
            causal = cell_label(cells, "mechanism")
        network = cell_label(cells, "network")
        perturb = cell_label(cells, "perturbation")
        molecular = cell_label(cells, "molecular_disease")
        engage = cell_label(cells, "engagement")
        lines.append(
            f"| {row['gene']} — {row['disease']} | {row.get('http_status')} | {row.get('elapsed_total_s')} | "
            f"**{hdr.get('verdict') or '—'}** | {cell_label(cells, 'direction')} | {lesion} | {causal} | "
            f"{network} | {perturb} | {molecular} | {engage} |"
        )

    lines += ["", "## Per-case structured excerpts", ""]
    keys = [
        "genetic_link", "allelic_series", "lesion_type", "direction", "qtl_cascade",
        "causal_mechanism", "network", "perturbation", "expression", "surface_epitope",
        "modality", "engagement", "molecular_disease", "clinical_validation", "attrition", "decision",
    ]
    for row in rows:
        lines.append(f"### {row['gene']} — {row['disease']}")
        lines.append(f"- Stress: {row['stress']}")
        lines.append(f"- Operational: ok={row.get('ok')}, status={row.get('http_status')}, total={row.get('elapsed_total_s')}s")
        if row.get("ok"):
            lines.append(f"- Stamp: `{json.dumps(row.get('stamp'), ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Decision header: `{json.dumps(row.get('decision_header'), ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Mechanistic spine: `{json.dumps(row.get('mechanism'), ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Phenotype→outcome: `{json.dumps(row.get('phenotype_outcome_synthesis'), ensure_ascii=False, sort_keys=True)}`")
            cells = row.get("drug_hunter_cells") or {}
            for key in keys:
                if key in cells:
                    lines.append(f"- `{key}`: `{json.dumps(cells[key], ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Fetch states: `{json.dumps(row.get('fetch_status_counts'), sort_keys=True)}`")
        else:
            lines.append(f"- Error: `{row.get('error')}`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    FULL.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []

    for idx, case in enumerate(CASES, 1):
        print(f"=== {idx}/{len(CASES)} {case['gene']} — {case['disease']} ===", flush=True)
        result = request_case(case)
        with (FULL / f"{case['id']}.json").open("w", encoding="utf-8") as fh:
            json.dump({"case": case, "result": result}, fh, ensure_ascii=False, indent=2)
        summary = summarize(case, result)
        summaries.append(summary)
        print(f"result ok={summary.get('ok')} status={summary.get('http_status')} time={summary.get('elapsed_total_s')}s", flush=True)

    with (OUT / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summaries, fh, ensure_ascii=False, indent=2)
    (OUT / "summary.md").write_text(markdown(summaries), encoding="utf-8")

    ok_n = sum(1 for r in summaries if r.get("ok"))
    print(f"Completed: {ok_n}/{len(summaries)} usable results; output={OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

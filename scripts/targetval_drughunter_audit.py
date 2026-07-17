#!/usr/bin/env python3
"""Read-only live audit of TargetVal03 from a drug-hunter perspective.

The script submits ten deliberately adversarial target–disease pairs to the
public /api/v1/validate endpoint, preserves each complete JSON response, and
writes a compact cross-case summary. It never writes to TargetVal.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("TARGETVAL_BASE_URL", "https://targetval-api.onrender.com").rstrip("/")
OUT = Path(os.environ.get("AUDIT_OUT", "audit_results"))
FULL = OUT / "full"

CASES: list[dict[str, str]] = [
    {
        "id": "01_pcsk9_fh",
        "gene": "PCSK9",
        "disease": "familial hypercholesterolemia",
        "stress": "positive control; inhibitory direction; human LoF to clinical validation; modality breadth",
    },
    {
        "id": "02_glp1r_obesity",
        "gene": "GLP1R",
        "disease": "obesity",
        "stress": "must resolve agonism rather than default inhibition; validated biology but molecule design still matters",
    },
    {
        "id": "03_tyk2_psoriasis",
        "gene": "TYK2",
        "disease": "psoriasis",
        "stress": "inhibit, but allosteric/selective TYK2 is not equivalent to broad JAK-family inhibition",
    },
    {
        "id": "04_gba_pd",
        "gene": "GBA",
        "disease": "Parkinson disease",
        "stress": "loss-of-function risk implies restore/chaperone rather than inhibit; CNS and lysosomal delivery",
    },
    {
        "id": "05_trem2_ad",
        "gene": "TREM2",
        "disease": "Alzheimer disease",
        "stress": "strong causal genetics but stage-, cell-state- and modality-dependent therapeutic operation",
    },
    {
        "id": "06_cetp_cad",
        "gene": "CETP",
        "disease": "coronary artery disease",
        "stress": "mixed clinical history; surrogate versus outcome; target versus molecule-specific failure",
    },
    {
        "id": "07_il1b_cad",
        "gene": "IL1B",
        "disease": "coronary artery disease",
        "stress": "clinical efficacy signal with infection liability, patient-selection and indication-window constraints",
    },
    {
        "id": "08_brd9_synovial_sarcoma",
        "gene": "BRD9",
        "disease": "synovial sarcoma",
        "stress": "strong dependency biology but negative clinical degrader readout; target-versus-asset adjudication",
    },
    {
        "id": "09_flt1_preeclampsia",
        "gene": "FLT1",
        "disease": "preeclampsia",
        "stress": "soluble isoform, placental compartment and gestational timing; whole-gene direction is unsafe",
    },
    {
        "id": "10_kras_pancreatic",
        "gene": "KRAS",
        "disease": "pancreatic cancer",
        "stress": "allele and tumor-context specificity; approvals in other diseases must not over-credit pancreatic cancer",
    },
]

RETRYABLE = {429, 500, 502, 503, 504}


def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", text).strip("_")


def request_case(case: dict[str, str]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "gene_symbol": case["gene"],
            "disease_name": case["disease"],
            "mode": "SETTLE",
        }
    ).encode("utf-8")
    url = f"{BASE_URL}/api/v1/validate"
    attempts: list[dict[str, Any]] = []
    overall_start = time.monotonic()

    for attempt in range(1, 5):
        started = time.monotonic()
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TargetVal-drughunter-audit/2026-07-17",
            },
        )
        status: int | None = None
        body = ""
        error = ""
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                status = int(resp.status)
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read().decode("utf-8", errors="replace")
            error = f"HTTPError: {exc}"
        except Exception as exc:  # network timeout, reset, DNS, etc.
            error = f"{type(exc).__name__}: {exc}"

        elapsed = round(time.monotonic() - started, 3)
        attempts.append(
            {
                "attempt": attempt,
                "status": status,
                "elapsed_s": elapsed,
                "error": error,
                "body_prefix": body[:500],
            }
        )

        if status == 200:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                error = f"JSONDecodeError: {exc}"
            else:
                return {
                    "ok": True,
                    "http_status": status,
                    "elapsed_total_s": round(time.monotonic() - overall_start, 3),
                    "attempts": attempts,
                    "response": parsed,
                }

        retryable = status in RETRYABLE or status is None
        if retryable and attempt < 4:
            # A failed cold computation may nevertheless continue server-side and populate
            # the cache, so a measured retry is biologically neutral and operationally useful.
            delay = 25 * attempt
            print(
                f"[{case['id']}] attempt {attempt} failed ({status or error}); retrying after {delay}s",
                flush=True,
            )
            time.sleep(delay)
            continue
        break

    return {
        "ok": False,
        "http_status": attempts[-1].get("status") if attempts else None,
        "elapsed_total_s": round(time.monotonic() - overall_start, 3),
        "attempts": attempts,
        "error": attempts[-1].get("error") if attempts else "no attempt recorded",
        "response_body_prefix": attempts[-1].get("body_prefix") if attempts else "",
    }


def _cell_map(response: dict[str, Any]) -> dict[str, Any]:
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
    base: dict[str, Any] = {
        "id": case["id"],
        "gene": case["gene"],
        "disease": case["disease"],
        "stress": case["stress"],
        "ok": result.get("ok", False),
        "http_status": result.get("http_status"),
        "elapsed_total_s": result.get("elapsed_total_s"),
        "attempts": result.get("attempts"),
    }
    if not result.get("ok"):
        base["error"] = result.get("error") or result.get("response_body_prefix")
        return base

    r = result["response"]
    dr = r.get("domain_report") or {}
    header = dr.get("decision_header") or {}
    card = r.get("card") or {}
    resolved = r.get("resolved") or {}
    cells = _cell_map(r)
    fetch = r.get("fetch_report") or {}
    status_counts: dict[str, int] = {}
    for value in fetch.values():
        if isinstance(value, dict):
            s = str(value.get("status") or "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1

    base.update(
        {
            "stamp": r.get("stamp"),
            "cached": r.get("cached"),
            "resolved": resolved,
            "decision_header": header,
            "card": {
                "card_class": card.get("card_class"),
                "conclusion": card.get("conclusion"),
                "path_quality": card.get("path_quality"),
                "has_causal_path": card.get("has_causal_path"),
                "mechanism_coverage": card.get("mechanism_coverage"),
                "direction_confidence": card.get("direction_confidence"),
                "tier_distribution": card.get("tier_distribution"),
                "effective_independent_count": card.get("effective_independent_count"),
                "coloc_group_count": card.get("coloc_group_count"),
            },
            "n_claims": r.get("n_claims"),
            "convergence_provenance": r.get("convergence_provenance"),
            "phenotype_outcome_synthesis": dr.get("phenotype_outcome_synthesis"),
            "relational_synthesis": dr.get("relational_synthesis"),
            "modality_profile": dr.get("modality_profile"),
            "epitope_role": dr.get("epitope_role"),
            "mechanism": dr.get("mechanism"),
            "drug_hunter_cells": cells,
            "fetch_status_counts": status_counts,
            "render_invariant": r.get("render_invariant"),
            "report_markdown": r.get("report_markdown"),
        }
    )
    return base


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# TargetVal03 live drug-hunter audit",
        "",
        f"Endpoint: `{BASE_URL}/api/v1/validate`",
        "",
        "| Case | HTTP | Total s | Cached | Verdict | Grade / route | Direction | Genetic link | Clinical validation |",
        "|---|---:|---:|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.get("ok"):
            lines.append(
                f"| {row['gene']} — {row['disease']} | {row.get('http_status') or '—'} | "
                f"{row.get('elapsed_total_s') or '—'} | — | **NO RESULT** | — | — | — | — |"
            )
            continue
        hdr = row.get("decision_header") or {}
        cells = row.get("drug_hunter_cells") or {}
        direction = cells.get("direction") or {}
        genetic = cells.get("genetic_link") or {}
        clinical = cells.get("clinical_validation") or {}
        grade = hdr.get("go_grade") or hdr.get("tier_route") or hdr.get("reason") or ""
        lines.append(
            f"| {row['gene']} — {row['disease']} | {row.get('http_status')} | "
            f"{row.get('elapsed_total_s')} | {row.get('cached')} | **{hdr.get('verdict') or '—'}** | "
            f"{str(grade).replace('|', '/')} | {direction.get('verdict_label') or direction.get('verdict') or '—'} | "
            f"{genetic.get('verdict_label') or genetic.get('verdict') or '—'} | "
            f"{clinical.get('verdict_label') or clinical.get('verdict') or '—'} |"
        )
    lines += ["", "## Raw case notes", ""]
    for row in rows:
        lines.append(f"### {row['gene']} — {row['disease']}")
        lines.append(f"- Stress: {row['stress']}")
        lines.append(f"- Operational: ok={row.get('ok')}, status={row.get('http_status')}, total={row.get('elapsed_total_s')}s")
        if row.get("ok"):
            hdr = row.get("decision_header") or {}
            lines.append(f"- Decision header: `{json.dumps(hdr, ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Convergence provenance: `{json.dumps(row.get('convergence_provenance'), ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Phenotype→outcome synthesis: `{json.dumps(row.get('phenotype_outcome_synthesis'), ensure_ascii=False, sort_keys=True)}`")
            lines.append(f"- Fetch states: `{json.dumps(row.get('fetch_status_counts'), sort_keys=True)}`")
        else:
            lines.append(f"- Error: `{row.get('error')}`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    FULL.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []

    for i, case in enumerate(CASES, 1):
        print(f"=== {i}/{len(CASES)} {case['gene']} — {case['disease']} ===", flush=True)
        try:
            result = request_case(case)
        except Exception as exc:  # the audit must preserve remaining cases
            result = {
                "ok": False,
                "http_status": None,
                "elapsed_total_s": None,
                "attempts": [],
                "error": f"harness exception {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            }
        (FULL / f"{_safe_slug(case['id'])}.json").write_text(
            json.dumps({"case": case, "result": result}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        summary = summarize(case, result)
        summaries.append(summary)
        print(
            f"[{case['id']}] ok={summary.get('ok')} status={summary.get('http_status')} "
            f"elapsed={summary.get('elapsed_total_s')} verdict="
            f"{(summary.get('decision_header') or {}).get('verdict')}",
            flush=True,
        )

    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "base_url": BASE_URL,
                "generated_epoch": time.time(),
                "cases": summaries,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (OUT / "summary.md").write_text(markdown(summaries), encoding="utf-8")

    n_ok = sum(bool(r.get("ok")) for r in summaries)
    print(f"Completed: {n_ok}/{len(summaries)} returned parseable HTTP 200 JSON", flush=True)
    # Do not fail the workflow merely because the platform failed a case: those failures
    # are audit findings and the artifact must still upload.
    return 0


if __name__ == "__main__":
    sys.exit(main())

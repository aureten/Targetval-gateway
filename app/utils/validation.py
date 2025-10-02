--- validation.revised.py(prev)
+++ validation.py(new)
@@ -1,112 +1,181 @@
+
 """
-Validation helpers for TARGETVAL inputs.
-
-- Keep checks permissive enough to not block real-world identifiers.
-- Provide a lightweight "symbolish" heuristic used by the gateway when deciding
-  whether to reuse `gene` as `symbol`.
-- Add optional EFO coercion utility so everything converges to EFO_#######.
-
-NOTE: Normalization (e.g., mapping aliases to canonical symbols) should happen
-in a dedicated normalizer (e.g., utils.normalize or within router functions).
+TargetVal Gateway — Validation & Normalization Utilities
+--------------------------------------------------------
+Lightweight, dependency-free helpers used by router endpoints and synthesis code.
+No network calls here; keep this module deterministic and fast.
 """
 
-from typing import Optional
+from __future__ import annotations
+
+import asyncio
+import inspect
+import re
+from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
+
 from fastapi import HTTPException
-import re
-
-__all__ = [
-    "validate_symbol",
-    "validate_condition",
-    "normalize_gene_symbol",
-    "is_symbolish",
-    "coerce_efo_id",
-]
-
-# Simple patterns tuned for common cases
-_SYMBOLISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\\-]{0,31}$")
-# Accept both EFO_0000000 and EFO:0000000, case-insensitive; we normalize to "EFO_#######"
-_EFO_RE = re.compile(r"^EFO[_:]\\d{7}$", re.IGNORECASE)
 
 
-def validate_symbol(symbol: Optional[str], field_name: str = "symbol") -> None:
-    """
-    Validate that a gene symbol or identifier is present and non-empty.
+# -----------------------------
+# Gene / ID validation helpers
+# -----------------------------
 
-    Accepts HGNC-like symbols, Ensembl gene IDs (ENSG...), and keeps permissive
-    character set (alnum and hyphen). Does not uppercase or modify the value.
-    """
-    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
-        # Use 422 to align with FastAPI's validation semantics
-        raise HTTPException(
-            status_code=422,
-            detail=f"Invalid {field_name}: value must be a non-empty string",
-        )
-    # Allow ENSG* to pass without further checks
-    s = symbol.strip()
-    if s.upper().startswith("ENSG"):
-        return
-    # Permissive heuristic; do not hard-fail on uncommon but legitimate tokens
-    if not _SYMBOLISH_RE.match(s):
-        # Intentionally do not raise: we avoid false negatives and let downstream
-        # normalizers / resolvers handle edge-case identifiers.
-        return
+_ENSG_RE = re.compile(r"^ENSG\d{9,}\.?\d*$", re.IGNORECASE)
+_SYM_RE  = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{0,19}$")
+_EFO_RE  = re.compile(r"^(EFO|MONDO|HP|Orphanet)[:_]\w+", re.IGNORECASE)
 
 
-def validate_condition(condition: Optional[str], field_name: str = "condition") -> None:
-    """
-    Validate that a condition or disease name is present and non-empty.
-
-    Keep permissive because many APIs take free text and we will resolve/normalize later.
-    """
-    if condition is None or not isinstance(condition, str) or not condition.strip():
-        raise HTTPException(
-            status_code=422,
-            detail=f"Invalid {field_name}: value must be a non-empty string",
-        )
+def is_symbolish(text: Optional[str]) -> bool:
+    """Heuristic: looks like an HGNC-like symbol (uppercase, digits, hyphen/dot)."""
+    if not text:
+        return False
+    t = text.strip()
+    return bool(_SYM_RE.match(t))
 
 
-def normalize_gene_symbol(
-    symbol: Optional[str] = None,
-    gene: Optional[str] = None,
-    field_name: str = "symbol",
-) -> str:
-    """
-    Normalize gene input, accepting either `symbol` or `gene` as fallback.
-    This does NOT perform biological alias normalization (done elsewhere).
-    """
-    value = symbol or gene
-    if value is None or not isinstance(value, str) or not value.strip():
-        raise HTTPException(
-            status_code=422,
-            detail=f"Invalid {field_name}: value must be a non-empty string (gene or symbol)",
-        )
-    return value.strip()
+def is_ensembl_id(text: Optional[str]) -> bool:
+    if not text:
+        return False
+    return bool(_ENSG_RE.match(text.strip()))
 
 
-def is_symbolish(value: Optional[str]) -> bool:
-    """
-    Heuristic used by the gateway to decide if a token looks like an HGNC-like symbol.
-    Returns False for Ensembl (ENSG...), CURIEs (with ":"), and tokens with underscores.
-    """
-    if not isinstance(value, str) or not value.strip():
-        return False
-    up = value.strip().upper()
-    if up.startswith("ENSG") or (":" in up) or ("_" in up):
-        return False
-    return bool(_SYMBOLISH_RE.match(up))
+def validate_symbol(text: Optional[str], field_name: str = "symbol") -> str:
+    """Accepts common gene symbols; raises HTTP 422 on clearly invalid input."""
+    if not text:
+        raise HTTPException(status_code=422, detail=f"{field_name} is required")
+    t = text.strip()
+    if is_ensembl_id(t):
+        # Allow Ensembl IDs to pass here as "symbolish"; router may resolve later.
+        return t
+    if not is_symbolish(t):
+        raise HTTPException(status_code=422, detail=f"{field_name} '{text}' is not a valid symbol-like string")
+    return t.upper()
 
 
-def coerce_efo_id(efo: Optional[str]) -> Optional[str]:
+def normalize_efo(efo_or_label: Optional[str]) -> Optional[str]:
+    """If already an ontology ID (EFO:/MONDO:/HP:), return as uppercase; else pass through."""
+    if not efo_or_label:
+        return None
+    t = efo_or_label.strip()
+    if _EFO_RE.match(t):
+        t = t.replace(":", "_")
+        return t.upper()
+    return t  # a free-text label; upstream endpoints may perform mapping
+
+
+def canonicalize_gene_inputs(
+    gene: Optional[str] = None,
+    symbol: Optional[str] = None,
+    ensembl_id: Optional[str] = None,
+) -> Tuple[Optional[str], Optional[str]]:
     """
-    Best-effort coercion to canonical EFO CURIE style (EFO_0000000).
-    Returns normalized value or raises if clearly malformed.
+    Decide on (symbol, ensembl_id) without hitting the network.
+    - If ensembl_id is provided and looks valid → keep; prefer given symbol if symbolish.
+    - Else if gene/symbol provided → treat as symbol (uppercased) and leave ensembl None.
     """
-    if efo is None:
+    sym = symbol or gene
+    sym = validate_symbol(sym, field_name="gene") if sym else None
+    ens = ensembl_id if is_ensembl_id(ensembl_id) else None
+    return sym, ens
+
+
+def validate_limit(limit: Optional[int], default: int = 50, max_limit: int = 500) -> int:
+    if limit is None:
+        return default
+    try:
+        L = int(limit)
+    except Exception:
+        raise HTTPException(status_code=422, detail="limit must be an integer")
+    if L <= 0 or L > max_limit:
+        raise HTTPException(status_code=422, detail=f"limit must be in 1..{max_limit}")
+    return L
+
+
+def normalize_species(species: Optional[str]) -> str:
+    if not species:
+        return "human"
+    s = species.strip().lower()
+    if s in {"human", "homo_sapiens", "hsapiens", "h.sapiens"}:
+        return "human"
+    if s in {"mouse", "mus_musculus", "mmusculus"}:
+        return "mouse"
+    return s
+
+
+# -----------------------------
+# Concurrency / binding helpers
+# -----------------------------
+
+def _bind_kwargs(func: Callable[..., Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
+    """Trim kwargs to function signature to avoid FastAPI/inspect explosions."""
+    sig = inspect.signature(func)
+    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
+    return accepted
+
+
+async def run_handlers_parallel(
+    calls: Iterable[Tuple[str, Callable[..., Any], Dict[str, Any]]],
+    concurrency: int = 8,
+) -> List[Tuple[str, Any]]:
+    """
+    Execute a set of (name, handler, kwargs) concurrently.
+    Returns list of (name, handler_result) preserving submission order.
+    """
+    sem = asyncio.Semaphore(concurrency)
+    results: List[Tuple[str, Any]] = []
+
+    async def _one(name: str, func: Callable[..., Any], kw: Dict[str, Any]):
+        async with sem:
+            bounded = _bind_kwargs(func, kw)
+            try:
+                out = func(**bounded)
+                if asyncio.iscoroutine(out):
+                    out = await out
+                # Try to produce plain dicts for pydantic models
+                if hasattr(out, "model_dump") and callable(out.model_dump):
+                    out = out.model_dump()
+                elif hasattr(out, "dict") and callable(out.dict):
+                    out = out.dict()
+                results.append((name, out))
+            except Exception as e:
+                results.append((name, {"status": "ERROR", "error": str(e)}))
+
+    tasks = [asyncio.create_task(_one(n, f, kw)) for (n, f, kw) in calls]
+    if tasks:
+        await asyncio.gather(*tasks)
+    return results
+
+
+# -----------------------------
+# Request body schemas (pydantic)
+# -----------------------------
+
+try:
+    from pydantic import BaseModel, Field
+except Exception:  # pragma: no cover
+    class BaseModel:  # type: ignore
+        pass
+    def Field(*args, **kwargs):  # type: ignore
         return None
-    s = efo.strip().upper().replace(":", "_")
-    if not _EFO_RE.match(s):
-        raise HTTPException(
-            status_code=422,
-            detail="Invalid efo: expected EFO_####### (e.g., EFO_0004611)",
-        )
-    return s
+
+class ScorecardRequest(BaseModel):
+    gene: str
+    condition: Optional[str] = None
+    tissue: Optional[str] = None
+    modules: Optional[List[str]] = Field(default=None, description="Optional subset of buckets/modules")
+    limit: Optional[int] = 50
+    species: Optional[str] = "human"
+
+
+def validate_scorecard_request(body: ScorecardRequest) -> Dict[str, Any]:
+    sym = validate_symbol(body.gene, field_name="gene")
+    efo = normalize_efo(body.condition)
+    L = validate_limit(body.limit, default=50, max_limit=500)
+    return {
+        "gene": sym,
+        "condition": efo or body.condition,
+        "tissue": body.tissue,
+        "limit": L,
+        "species": normalize_species(body.species),
+        "modules": body.modules or [],
+    }

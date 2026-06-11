# Targetval Gateway

Biologic target validation API gateway — wired to **live public data feeds** (no stubs, no placeholders).

- **Live service:** https://targetval-gateway.onrender.com  
- **Interactive OpenAPI UI:** add `/docs` to the base URL (FastAPI default).  

This gateway fronts 74 programmatic modules for **target–disease evidence aggregation** across genetics, expression, omics, mechanism, tractability, clinical/safety, and IP, grouped into six evidence domains.  

---

## ✨ Features

- ✅ **74 live modules** across 6 evidence domains (`GET /domains` to enumerate).  
- ✅ **EFO fallback:** supports both `?efo=EFO_…` and free-text `?condition=asthma`.  
- ✅ **Bounded retries, backoff & circuit breaking:** resilient HTTP client for upstream APIs.  
- ✅ **Structured `Evidence` payloads:** with `status`, `fetched_n`, `citations`, and timestamp.  
- ✅ **NO_DATA vs ERROR:** upstream empties return `status="NO_DATA"`, true failures → `ERROR` (HTTP 200 either way).  

> **Note:** This gateway currently has **no built-in authentication** — every
> endpoint is public. If you need API-key enforcement, put it behind a
> reverse proxy / API gateway, or add a FastAPI dependency. CORS defaults to
> permissive (`*`); set `CORS_ALLOW_ORIGINS` in production.

### Key environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | Public base URL advertised in OpenAPI `servers` (for ChatGPT Actions). | — |
| `CORS_ALLOW_ORIGINS` | Comma-separated allowed origins. | `*` |
| `HTTP_USER_AGENT` | User-Agent sent to upstream APIs. | `TargetvalGateway/…` |
| `WRAPPERS_ENABLED` | Expose the `/modules`, `/module`, `/aggregate`, `/domain` compat endpoints. | `1` |
| `LOG_LEVEL` | Log verbosity. | `INFO` |

---


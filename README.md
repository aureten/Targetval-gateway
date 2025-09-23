# Targetval Gateway

> Biologic target validation API gateway — with explicit API key enforcement and flexible **EFO/condition** handling.

**Live service**: `https://targetval-gateway.onrender.com` (if deployed)  
**OpenAPI UI**: add `/docs` to the base URL (FastAPI defaults).

This repository fronts a set of programmatic endpoints for target–disease evidence aggregation. This update adds:
- ✅ **Real API key enforcement** with a small, auditable middleware.
- ✅ Clear environment-variable–based config, with a checked-in `.env.example`.
- ✅ **EFO fallback** — endpoints that previously required `?efo=EFO_...` can now resolve from `?condition=` (e.g., `asthma`) and continue, reducing “Missing efo” friction.

> Why: Feedback flagged sparse docs and a no-op API key path. This README + the new middleware and resolver fix both.

---

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env  # then edit values
# If your app entry is app/main.py with FastAPI "app", uvicorn works as:
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> The repo contains a `Procfile` for Render/Heroku-style deploys; use that in production.

---

## Authentication (now enforced)

All non-public paths require an API key unless you disable enforcement.

- **Header (recommended)**: `X-API-Key: <your_key>`  
- **Bearer**: `Authorization: Bearer <your_key>`  
- **Query (fallback)**: `?api_key=<your_key>`  

**Configure** via environment variables:

- `ENFORCE_API_KEY` (default: `true`) — set to `false` to allow unauthenticated access.
- `ALLOWED_API_KEYS` — comma-separated list of valid keys. If empty and enforcement is on, *any non-empty key* is accepted.
- `ALLOW_ANONYMOUS_PATHS` — space-separated list of paths that never require a key (default: `/ /docs /redoc /openapi.json /healthz`).

Implementation lives in `app/middleware/api_key.py` (drop-in FastAPI/Starlette middleware).

---

## EFO fallback (`condition` → `EFO_*`)

Earlier deployments hard-required `?efo=EFO_...` for some genetics endpoints. The gateway now accepts **`?condition=`** (free text disease/phenotype) and resolves it to an EFO ID using an external resolver:

- Strategy **`ols`** (default): Use EMBL-EBI **OLS4** REST API to search EFO and return the top match.
- Strategy `opentargets`: Use the Open Targets Platform’s API to resolve disease terms.
- Strategy `none`: Preserve old strict behavior (error if `efo` missing).

Environment variables:

- `EFO_RESOLVE_STRATEGY` — `ols` | `opentargets` | `none` (default: `ols`)
- `EFO_RESOLVE_REQUIRED` — `true` to raise if resolution fails, `false` to proceed with `efo=None` (default: `true`)

### How to wire it in an endpoint

Replace strict signatures like:
```python
# old
@router.get("/genetics/some-endpoint")
async def some_endpoint(efo: str = Query(...)):
    ...
```

With a tolerant version that uses the resolver:
```python
# new
from fastapi import Depends, Query
from app.utils.efo_resolver import require_efo_id

@router.get("/genetics/some-endpoint")
async def some_endpoint(efo_id: str = Depends(require_efo_id)):
    # use efo_id downstream, guaranteed non-empty if EFO_RESOLVE_REQUIRED=true
    ...
```

The resolver internally accepts either `?efo=` or `?condition=` and handles normalization.

### If you still see “Missing efo”

- Make sure you’ve **pulled the latest commit** and added the new file `app/utils/efo_resolver.py`.
- Confirm `EFO_RESOLVE_STRATEGY=ols` (or `opentargets`) in your environment.
- If you want hard errors when neither `efo` nor resolvable `condition` is supplied, set `EFO_RESOLVE_REQUIRED=true` (default).

---

## Environment variables

See `.env.example` for a ready-to-copy template. Key options:

```ini
ENFORCE_API_KEY=true
ALLOWED_API_KEYS=dev123,another_key_value
ALLOW_ANONYMOUS_PATHS=/ /docs /redoc /openapi.json /healthz

# EFO resolution behavior
EFO_RESOLVE_STRATEGY=ols          # ols | opentargets | none
EFO_RESOLVE_REQUIRED=true         # true | false
REQUEST_TIMEOUT_SECONDS=10        # outbound request timeout for resolvers
```

---


---

## Optional: degraded mode for strict upstream providers

Some sources (commercial or rate-limited) demand credentials. Use the helper in `app/utils/http_client.py` and pass
`required_env_keys=[...]` to declare which env vars must exist for a given call. Behavior:

- `ALLOW_UPSTREAM_DEGRADED=true` (default) → the call **returns a placeholder** with `_degraded: true` and a warning,
  so your endpoint can still return a 200 with partial data or a clear message.
- `ALLOW_UPSTREAM_DEGRADED=false` → the gateway raises **HTTP 424** (Failed Dependency), making the failure explicit.

Example:
```python
from app.utils.http_client import get_json

data = get_json(
    "https://strict.example/v1/things",
    headers={"Authorization": f"Bearer {os.environ.get('STRICT_API_TOKEN')}"},
    required_env_keys=["STRICT_API_TOKEN"],
)
```


## Deploy on Render

See `docs/DEPLOY_RENDER.md` for step-by-step. In short:

- **Build command**: `pip install -r requirements.txt`
- **Start command**: use the `Procfile` or `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Configure the environment variables above in the Render **Environment** tab.
- Healthcheck: `/healthz` (add a simple FastAPI `GET` if not present).

---

## Notes on upstreams

- **Open Targets Platform** GraphQL/REST is public and great for target–disease work. See docs for details.  
- **OLS4 (EFO)** provides a public REST API for ontology lookups; we use it to translate free-text conditions into EFO IDs.

See references in the bottom of this file.

---

## Contributing

- Keep endpoints small and stateless.
- Guard IO to strict timeouts and short caches.
- Fail **early** with actionable `detail` + `hint` fields in errors.
- PRs welcome for additional resolvers (MONDO, DOID) and better ranking.

---

## License

Apache-2.0 (same as most scientific infra; adjust if your repo specifies otherwise).

---

## References

- Open Targets Platform: GraphQL / API docs.  
- OLS4 (EFO): public instance & REST API.

_(Last updated: 2025-09-23)_

# Targetval Gateway

Biologic target validation API gateway — robust, key-enforced, and wired to **live public data feeds** (no stubs, no placeholders).

- **Live service:** https://targetval-gateway.onrender.com  
- **Interactive OpenAPI UI:** add `/docs` to the base URL (FastAPI default).  

This gateway fronts 30+ programmatic endpoints for **target–disease evidence aggregation** across genetics, expression, pathways, tractability, safety, and IP.  

---

## ✨ Features

- ✅ **Real API key enforcement** via middleware (`app/middleware/api_key.py`).  
- ✅ **Environment-variable config** with `.env.example` checked in.  
- ✅ **EFO fallback:** supports both `?efo=EFO_…` and free-text `?condition=asthma`.  
- ✅ **Bounded retries & backoff:** resilient HTTP client for upstream APIs.  
- ✅ **Structured `Evidence` payloads:** with `status`, `fetched_n`, `citations`, and timestamp.  
- ✅ **NO_DATA vs ERROR:** upstream empties return `status="NO_DATA"`, true failures → `ERROR`.  

---


# RouteShield

**Evidence-driven recall response and operational recovery for school-bus fleet operators.**

Built for the **Vultr Agent Rush Hackathon 2026 — Track 2: Powering the Future of Work.**

> ### Core product principle
> **Absence of evidence is not evidence of safety.**
> A model-year or model match is never presented as proof that an individual vehicle has an
> open recall. The system abstains (`VERIFICATION_REQUIRED`) whenever critical evidence is
> missing, and it never makes autonomous safety decisions — a human fleet manager explicitly
> selects the operational policy and explicitly approves every operational change.

---

## What RouteShield does

When a vehicle safety recall is announced, RouteShield:

1. **Retrieves the real recall** from the official NHTSA campaign API
   (`api.nhtsa.gov/recalls/campaignNumber`) and downloads the official **Part 573 report PDF**
   (`static.nhtsa.gov/odi/rcl/...`).
2. **Extracts applicability criteria** from the report with a deterministic PyMuPDF parser —
   make/model, per-model-year production windows, the brake configuration **feature code**,
   explicit exclusions — with **page numbers, verbatim snippets, extraction method, confidence
   and review status** preserved for every field. An optional **Vultr Serverless Inference**
   (OpenAI-compatible) LLM path produces *supplementary* extraction marked `NEEDS_REVIEW`.
3. **Matches criteria against the fleet register** using deterministic Python only
   (the LLM never classifies a vehicle).
4. **Classifies every bus** as `CAMPAIGN_CRITERIA_MATCH`, `VERIFICATION_REQUIRED`, or
   `OUTSIDE_REPORT_VERSION` — always with the exact human-readable reasons and per-criterion
   evidence. VIN-level status is tracked separately and honestly
   (`VIN_STATUS_UNAVAILABLE` for synthetic records — never faked).
5. **Computes route impact** under the fleet manager's explicitly selected policy
   (`HOLD_PENDING_VERIFICATION` or `MANAGER_REVIEW_ONLY`).
6. **Proposes replacements** from the spare pool with capacity, depot and equipment
   compatibility checks (deterministic greedy best-fit; infeasible routes report exactly why).
7. **Requires explicit manager approval** — the only code path that may change operational
   state, inside a single DB transaction, re-validating plan hash and spare availability.
8. **Records everything** in an append-only audit log that no endpoint can modify or delete.

### Security architecture (non-negotiable)

```
LLM (Vultr Serverless Inference)          Deterministic backend
────────────────────────────────          ─────────────────────────────────────────
reads report text                         validates schemas
extracts structured JSON                  checks campaign criteria (assessment.py)
output marked NEEDS_REVIEW                calculates route impact (recovery.py)
NO database access                        enforces authorization (auth.py)
NO tool access                            applies approved changes (approval.py)
NO vehicle classification                 writes append-only audit (audit.py)
NO operational writes                     ONLY POST /api/incidents/{id}/approve-plan
                                          may modify operational state
```

### Demo data

All fleet records are synthetic and labelled
**“SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES”**.
Synthetic identifiers (e.g. `SYNTHETIC-BUS-001`) are **never** submitted to NHTSA or vPIC —
enforced in code and covered by tests. The demonstration recall campaign (**26V111**,
International Motors IC Bus CESB brake lines) is **real** and retrieved from official sources.

---

## Repository layout

```
backend/                 FastAPI application
  app/
    main.py              app factory, startup seeding, optional background live refresh
    config.py            env-driven settings (pydantic-settings; no hardcoded secrets)
    db.py                SQLAlchemy engine/session (SQLite default, PostgreSQL-compatible)
    models.py            full data model (campaigns, report versions, criteria, fleet,
                         incidents, assessments, policies, proposals, approvals, audit,
                         benchmark runs, VIN decode cache)
    schemas.py           Pydantic API schemas + strict LLM extraction schema
    auth.py              manager account, PBKDF2 hashing, HMAC-signed tokens
    seed.py              idempotent synthetic fleet/route/incident seeding
    services/
      nhtsa.py           official campaign API + report download (cached by SHA-256)
      pdf_extraction.py  deterministic Part 573 parser (PyMuPDF), validation, provenance
      llm_extraction.py  Vultr Serverless Inference client (strict JSON, fallback-safe)
      evidence.py        live pipeline, hash-pinned review inheritance, fixture fallback,
                         changed-document → PENDING_REVIEW versioning
      assessment.py      deterministic classification engine (pure core + DB wrapper)
      recovery.py        route impact + spare allocation (deterministic greedy best-fit)
      approval.py        THE ONLY operational-write path (transactional, re-validating)
      audit.py           append-only audit log (no update/delete anywhere)
      vpic_service.py    single human-triggered VIN decode/recall-check, cached, throttled
    benchmark/           ground-truth cases + metric runner (nothing hardcoded)
    data/campaigns/26V111.reviewed.json   manually reviewed fallback fixture (hash-pinned)
  tests/                 93 automated tests (pytest), incl. the real report PDF fixture
frontend/                React + TypeScript + Vite + Tailwind CSS
  src/views/             Incident Command · Fleet · Route Recovery · Evidence & Audit
scripts/run_benchmark.py reproducible benchmark CLI
docker-compose.yml       single-VM deployment (nginx proxy + healthchecks + volume)
backend/Dockerfile       non-root, healthchecked
frontend/Dockerfile      multi-stage build → nginx with /api reverse proxy
.env.example             every supported environment variable
DEPLOYMENT.md            exact Vultr deployment commands
```

---

## Run locally (fresh checkout)

Requirements: Python 3.11+, Node 20+.

```bash
# 1. Backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env            # then set ROUTESHIELD_MANAGER_PASSWORD (and optionally
                                # ROUTESHIELD_AUTH_SECRET, VULTR_INFERENCE_*)
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 &
cd ..

# 2. Frontend (proxies /api to the backend)
cd frontend && npm install && npm run dev
# open http://localhost:5173
```

Manager login: username/password from `ROUTESHIELD_MANAGER_USERNAME` /
`ROUTESHIELD_MANAGER_PASSWORD`. If the password env var is unset, a random one is generated
and printed **once** in the backend log (never hardcoded).

First startup seeds the database idempotently: 15 synthetic vehicles, 6 routes, the 26V111
incident backed by the reviewed fixture, and an initial deterministic assessment. A background
thread then attempts the **live** NHTSA refresh; on success the UI banner switches from
“Using manually reviewed campaign evidence.” to “Live evidence”. Re-running the seed never
duplicates incidents, vehicles or routes.

### Tests & benchmark

```bash
cd backend && python -m pytest tests/ -q     # 93 tests, fully offline (fixtures + mocks)
python scripts/run_benchmark.py --verify-determinism
```

---

## Environment variables

See `.env.example` for the authoritative list. Highlights:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLite default; any SQLAlchemy URL (PostgreSQL works unchanged) |
| `ROUTESHIELD_MANAGER_USERNAME/PASSWORD` | Manager account (random password if unset) |
| `ROUTESHIELD_AUTH_SECRET` | HMAC token signing — **required in production** |
| `VULTR_INFERENCE_API_KEY / _BASE_URL / _MODEL` | Optional LLM-assisted extraction (never required) |
| `AUTO_REFRESH_RECALL` | Background live NHTSA refresh at startup (default `true`) |
| `ALLOW_OFFLINE_FALLBACK` | Permit the reviewed fixture when live retrieval fails (default `true`) |
| `VPIC_MIN_REQUEST_INTERVAL_SECONDS` | Outbound vPIC throttle (default 2s) |

Secrets are read from the environment only — nothing is baked into images or source.

---

## API overview (full docs at `/docs` — OpenAPI)

```
GET  /api/health
POST /api/auth/login
GET  /api/incidents                                  GET /api/incidents/{id}
POST /api/incidents/{id}/refresh-recall              GET /api/incidents/{id}/evidence
POST /api/incidents/{id}/assess-fleet                GET /api/incidents/{id}/fleet-assessment
POST /api/incidents/{id}/set-operational-policy      (manager)
POST /api/incidents/{id}/generate-recovery-plan      (manager)
GET  /api/incidents/{id}/recovery-plan
POST /api/incidents/{id}/approve-plan                (manager — ONLY operational write path)
GET  /api/incidents/{id}/audit
GET  /api/fleet/vehicles                             GET /api/fleet/routes
GET  /api/benchmarks/latest                          POST /api/benchmarks/run
POST /api/vpic/decode-single-vin                     (manager, one VIN, audited)
POST /api/vpic/recall-check-single-vin               (manager, one VIN, audited)
```

---

## Deployment

Single Vultr Cloud Compute VM via Docker Compose — see **[DEPLOYMENT.md](DEPLOYMENT.md)** for
exact commands. Short version:

```bash
git clone <repo> && cd routeshield
cp .env.example .env   # set ROUTESHIELD_MANAGER_PASSWORD + ROUTESHIELD_AUTH_SECRET
docker compose up -d --build
# UI at http://<vm-ip>:8080 (put nginx/caddy + TLS in front for public use)
```

## Honest limitations

* NHTSA’s VIN-level recall lookup is protected against third-party automated access; the
  single-VIN check reports “unavailable” honestly instead of ever fabricating a VIN-confirmed
  status. vPIC decode (attributes only) is fully functional.
* The LLM path requires `VULTR_INFERENCE_API_KEY`; without it everything still works via the
  deterministic parser and the reviewed fixture.
* Benchmark numbers describe **synthetic test cases**, not production fleet accuracy.

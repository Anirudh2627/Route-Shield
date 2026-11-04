# RouteShield — Engineering Report

**Vultr Agent Rush Hackathon 2026 · Track 2: Powering the Future of Work**
Report date: 2026-11-03 · Backend engine v1.1.0 · App v0.1.0

---

## 1. What was implemented

A complete, working, production-style MVP of **RouteShield**, an evidence-driven recall
response and operational recovery platform for school-bus fleet operators:

* **Real NHTSA integration** — campaign API retrieval + official Part 573 report PDF download
  for campaign **26V111** (International Motors, LLC — IC Bus CESB school buses, brake lines).
* **Deterministic PDF extraction** (PyMuPDF) of every applicability criterion with
  page numbers, verbatim snippets, extraction method, confidence and review status.
  Independently verified against the real PDF: 5 model-year production windows, overall
  suspect window 12/15/2022–02/02/2026, and the brake configuration criterion
  **Hydraulic Brake, feature code 0004100**, with the explicit exclusion
  “All other similar vehicles, which are not subject to this recall, are excluded.”
* **Report versioning by SHA-256** — a changed document creates a NEW `PENDING_REVIEW`
  version; reviewed criteria are never silently replaced. When the live document hash equals
  the hash-pinned reviewed fixture, the human review legitimately carries over.
* **LLM-assisted extraction** via Vultr Serverless Inference (OpenAI-compatible client),
  strict Pydantic schema validation, output stored as supplementary `NEEDS_REVIEW` evidence
  only. The LLM has **zero** write access, zero tool access, and never classifies vehicles.
* **Manually reviewed fallback fixture** (`backend/data/campaigns/26V111.reviewed.json`)
  keeps the whole product functional with no network; when active, the UI states
  “**Using manually reviewed campaign evidence.**” and never pretends it was a live extraction.
* **Deterministic vehicle assessment** with the four required dimensions:
  `CAMPAIGN_CRITERIA_MATCH` / `VERIFICATION_REQUIRED` / `OUTSIDE_REPORT_VERSION` plus a
  separate honest VIN-evidence status (`VIN_STATUS_UNAVAILABLE` for all synthetic records).
  Every classification ships exact reasons + per-criterion evidence (required vs observed vs
  source page). “Outside” never implies safe; “match” never implies VIN-confirmed.
* **Synthetic fleet** — 12 route buses + 3 spares + 6 routes, every record labelled
  “SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES”, engineered to cover every category
  (matches, missing build date, missing/ambiguous feature code, conflicting records,
  out-of-window, excluded configuration, wrong make/model).
* **Operational policy with human control** — `HOLD_PENDING_VERIFICATION` vs
  `MANAGER_REVIEW_ONLY`, explicitly selected, acknowledged, recorded, audited, and embedded
  in the proposal; never auto-chosen. Changing policy supersedes pending plans.
* **Deterministic recovery engine** — route impact, spare allocation with capacity/depot/
  equipment checks, duplicate-assignment prevention, greedy best-fit ranking, explicit
  unresolved-issue reporting for infeasible routes.
* **Transactional approval** — `POST /api/incidents/{id}/approve-plan` is the **only**
  operational write path: authenticates the manager, validates proposal status + plan hash +
  policy consistency, **re-checks spare availability and original route assignments**, applies
  all changes in one DB transaction, writes append-only audit events, returns persisted state.
* **Append-only audit log** — no update/delete code path or endpoint exists (enforced by test).
* **Measured benchmark** — 40 synthetic ground-truth cases, runtime-computed metrics
  (nothing hardcoded), confusion matrix, determinism verification, CLI + API + UI display.
* **Single-VIN vPIC decoding** — manager-authenticated, one VIN per request, cached,
  throttled, synthetic-guarded, unavailable fields stay UNKNOWN (never inferred), decode never
  becomes recall confirmation. A single human-triggered NHTSA VIN recall check exists and
  reports honestly when NHTSA declines third-party access.
* **Polished enterprise UI** — Incident Command (primary workflow), Fleet, Route Recovery,
  Evidence & Audit, with approval success transition and newly-created audit entries
  highlighted immediately after approval.
* **Deployment assets** — non-root healthchecked backend Dockerfile, multi-stage frontend
  Dockerfile (nginx + `/api` proxy + `/healthz`), docker-compose with persistent volume and
  required-secret guards, `.env.example`, DEPLOYMENT.md with exact Vultr commands.

## 2. Project directory structure

```
routeshield/
├── backend/
│   ├── app/
│   │   ├── main.py, config.py, db.py, models.py, schemas.py, auth.py, seed.py
│   │   ├── routers/    system.py · incidents.py · fleet.py · benchmarks.py · vpic.py
│   │   ├── services/   nhtsa.py · pdf_extraction.py · llm_extraction.py · evidence.py
│   │   │               assessment.py · recovery.py · approval.py · audit.py · vpic_service.py
│   │   ├── benchmark/  cases.py (ground truth) · runner.py (metrics)
│   │   └── data/campaigns/26V111.reviewed.json   (hash-pinned reviewed fixture)
│   ├── tests/          conftest + 7 test modules + fixtures (real PDF, real API JSON)
│   ├── requirements.txt · Dockerfile · .dockerignore
├── frontend/
│   ├── src/  main.tsx · App.tsx · api.ts · types.ts
│   │        components/ui.tsx
│   │        views/ IncidentCommand · FleetView · RouteRecoveryView · EvidenceAuditView
│   ├── index.html · vite.config.ts · tailwind.config.js · tsconfig.json
│   ├── Dockerfile · nginx.conf · .dockerignore
├── scripts/run_benchmark.py
├── docker-compose.yml · .env.example · .gitignore
├── README.md · DEPLOYMENT.md · ENGINEERING_REPORT.md
```

## 3. How to run locally

```bash
# backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env        # set ROUTESHIELD_MANAGER_PASSWORD
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000

# frontend (separate shell)
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Tests: `cd backend && python -m pytest tests/ -q`
Benchmark: `python scripts/run_benchmark.py --verify-determinism`

## 4. Required environment variables

| Variable | Required | Notes |
|---|---|---|
| `ROUTESHIELD_MANAGER_PASSWORD` | production | random one-time password logged if unset |
| `ROUTESHIELD_AUTH_SECRET` | production | tokens invalidate on restart if unset |
| `DATABASE_URL` | optional | SQLite default; PostgreSQL-compatible |
| `VULTR_INFERENCE_API_KEY` / `_BASE_URL` / `_MODEL` | optional | LLM-assisted extraction; app fully works without |
| `AUTO_REFRESH_RECALL`, `ALLOW_OFFLINE_FALLBACK` | optional | default `true` |
| `NHTSA_API_BASE`, `NHTSA_STATIC_BASE`, `VPIC_API_BASE`, `HTTP_TIMEOUT_SECONDS`, `VPIC_MIN_REQUEST_INTERVAL_SECONDS`, `CORS_ORIGINS`, `SERVE_FRONTEND_DIR`, `ENVIRONMENT` | optional | see `.env.example` |

## 5. What actually works end to end (verified by running it)

Verified against the live dev servers (uvicorn :8000 + vite :5173), not just unit tests:

1. Startup seeds incident 26V111 + synthetic fleet + initial assessment — idempotently.
2. Background/auto refresh retrieved the **real** campaign from `api.nhtsa.gov`, downloaded
   the **real** 469,300-byte Part 573 PDF from `static.nhtsa.gov`, extracted it
   (`complete: True`, 5 vehicle groups, feature code 0004100, exclusion), hash matched the
   reviewed fixture ⇒ `LIVE_EXTRACTION` + `REVIEWED`.
3. Fleet assessment classified all 15 synthetic vehicles: 3 match / 4 verification-required /
   8 outside — every row with reasons and per-criterion evidence.
4. Manager login → policy selection (acknowledgement enforced) → deterministic plan
   (5 disrupted routes under HOLD, 2 recoverable, 3 with explicit unresolved issues) →
   **APPROVE** → transaction applied: routes reassigned to spares, matched buses
   `GROUNDED_RECALL`, held buses `HELD_PENDING_VERIFICATION`, spares consumed; re-fetched
   fleet/route endpoints reflected persisted state; audit events returned in the approval
   response and visible in the UI feed.
5. Wrong plan hash, stale plan, policy mismatch, unavailable spare, unassigned-route drift,
   double approval — all rejected 401/409 with **zero** state change (verified).
6. Benchmark via API: 40 samples, false-confirmation 0.0, deterministic across runs.
7. vPIC live decode of a real sample VIN: 200 OK, 48/154 fields returned,
   `BrakeSystemType` empty ⇒ listed as unavailable, **not inferred**; second call served
   from cache.

## 6. NHTSA integration status

* **Campaign API** (`/recalls/campaignNumber?campaignNumber=26V111`): **working live**
  (200 OK, 5 model-year rows, cached raw payload in DB).
* **Part 573 report PDF** (`RCLRPT-26V111-4068.pdf`): **working live**; downloaded, parsed,
  SHA-256 `a29c04faf2273ba30ecfac16c0e2dfd0550f2aa2d7529e85cdce35844c8125f6`, cached by hash.
* **vPIC decode** (`DecodeVinValuesExtended`): **working live** (see §8).
* **VIN-level recall lookup** (`/recalls/vin`): NHTSA returns 401/403 (“Missing
  Authentication Token” / Akamai “Access Denied”) for third-party automated access from this
  environment. RouteShield reports this honestly as *verification unavailable* and never
  fabricates VIN-confirmed status. No bulk VIN recall requests exist anywhere (enforced by
  test `test_no_bulk_endpoint_exists`).
* Offline behavior: any NHTSA failure ⇒ reviewed fixture with explicit banner (tested).

## 7. PDF extraction accuracy limitations & fallback status

* The rule-based parser is tuned to the fixed Part 573 layout (label/value pairs, “Vehicle N”
  blocks, suspect-population sentence). On the real 26V111 document it extracts **100% of the
  required criteria with correct page references** (validated in tests against the real PDF).
* Limitations: manufacturers that phrase the suspect-population paragraph differently, or
* amended reports with unusual layouts, may produce incomplete extractions. The parser then
  reports `is_complete=False` with explicit warnings and the system **falls back to the
  reviewed fixture** rather than guessing. Multi-criterion feature codes beyond the first
  reviewed one are not used for matching. LLM-assisted extraction can supplement messy text
  but is always `NEEDS_REVIEW` and never backs matching by itself.
* Fallback fixture: **implemented, hash-pinned, tested**; UI shows the exact required string
  “Using manually reviewed campaign evidence.” when active.
* Changed-document handling: **implemented and tested** — new hash ⇒ new `PENDING_REVIEW`
  version, old reviewed criteria untouched, assessments continue against the last reviewed
  version with a UI banner explaining the situation.

## 8. Was vPIC decoding tested?

**Yes — live.** `DecodeVinValuesExtended` was called once for a public sample VIN
(`1HGCM82633A004352`): HTTP 200, Make=HONDA, Model=Accord, ModelYear=2003, 48/154 fields
populated, `BrakeSystemType` **not returned** ⇒ surfaced as unavailable and never inferred.
Caching, throttling, auth, synthetic guards and failure handling are covered by 12 automated
tests (mocked HTTP; no synthetic identifier ever leaves the process). The single-VIN NHTSA
recall check is implemented but its upstream endpoint refuses third-party access from this
environment (§6) — the honest-unavailability path is what executes and is tested.

## 9. Test results

```
cd backend && python -m pytest tests/ -q
93 passed, 0 failed, 0 errors  (~30 s, fully offline: fixtures + monkeypatched HTTP)
```

Coverage by required area:
* A. Recall extraction — 16 tests (real-PDF extraction incl. page refs & snippets, invalid
  PDF, unrelated PDF, date parsing, LLM valid/invalid-JSON/schema-violation/missing-fields/
  inference-failure/not-configured, fixture fallback, hash-inherited review, changed document).
* B. Vehicle assessment — 18 tests (exact match, missing build date/config/everything,
  outside range both directions, explicit exclusion, conflicts, parametrized
  no-false-confirmation, explanation completeness, API consistency).
* C. Route recovery — 10 tests (capacity, depot, characteristics, duplicates, unavailable
  spare, unverified spare never proposed, policy-dependent handling both ways, determinism,
  supersede).
* D. Authorization/approval — 13 tests (unauthenticated 401s, bad credentials, tampered
  tokens, invalid proposal/hash/policy, availability re-check, stale-route re-check, no
  state change before approval, persisted approval, double approval, policy-change supersede).
* E. VIN handling — 12 tests (synthetic never sent — two guards + API level, one-at-a-time
  schema, no bulk endpoint, cache hit = no second outbound call, unknown fields stay unknown,
  audit wording, upstream failure → 502, no auto-decodes during workflow).
* F. Audit — append-only routes test, module-scan test (no update/delete), previous/new state
  correctness, actor correctness, rejection auditing, immediate visibility via API.
* G. Benchmark — determinism (double run), false-confirmation & abstention formulas
  recomputed independently from case results, confusion-matrix totals, label coverage,
  API endpoints.

One benchmark case (BM-015) initially disagreed with the engine; investigation showed the
**test case label was wrong** (its build date was outside the documented window, so
OUTSIDE_REPORT_VERSION was the correct verdict). The case was corrected; the engine was not
weakened. This is recorded here for transparency.

## 10. Benchmark results (measured, not invented)

`python scripts/run_benchmark.py --verify-determinism` (engine v1.1.0, 2026-11-03):

```
SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY
Sample count                : 40
Correct campaign-match rate : 100.0%  (11/11)
Correct abstention rate     : 100.0%  (12/12)
FALSE-CONFIRMATION RATE     : 0.0%    (0/12)
Outside-report accuracy     : 100.0%  (17/17)
Overall accuracy            : 100.0%  (40/40)
DETERMINISM CHECK           : PASSED (two consecutive runs identical)
Confusion matrix (expected↓/predicted→): MATCH 11/0/0 · VERIF 0/12/0 · OUTSIDE 0/0/17
```

Computed at runtime from hand-labelled ground truth covering boundary dates (±1 day),
missing/conflicting fields, exclusions and wrong make-model. The UI (Incident Command strip
and Evidence & Audit dashboard) displays these metrics, the sample count and the last
execution timestamp from the stored `BenchmarkRun` row.

## 11. Was Docker deployment tested?

**Partially — be aware:** the build sandbox has **no Docker daemon**, so
`docker build` / `docker compose up` were **not executed**. What was done instead:

* Both Dockerfiles, `nginx.conf` and `docker-compose.yml` were written conservatively
  (official base images, non-root backend, healthchecks, named volume, `:?`-guarded secrets).
* The **single-container path was verified without Docker**: the backend serving the built
  frontend via `SERVE_FRONTEND_DIR=frontend/dist` returned the SPA (200, root div), served
  hashed assets (200, 208 KB JS) and the API (200) from one process — the same shape nginx
  proxies in compose.
* DEPLOYMENT.md includes a first-launch verification checklist to close the gap on the VM.

## 12. Was a public Vultr deployment actually completed?

**No.** No Vultr account/VM was accessible from this environment, and per the engineering
rules I will not claim otherwise. DEPLOYMENT.md contains the exact, copy-pasteable commands
(provision spec, Docker install, secrets generation, compose launch, nginx+TLS, verification
checklist) to complete it in ~10 minutes on a Vultr Cloud Compute VM.

## 13. Remaining blockers / known gaps

1. **Vultr VM deployment not executed** (no environment access) — checklist ready (§12).
2. **LLM extraction untested against a live Vultr Serverless Inference endpoint** (no API key
   here). The integration is complete and unit-tested with mocks; it is optional by design —
   with `VULTR_INFERENCE_API_KEY` unset the deterministic parser + reviewed fixture carry the
   product. Setting the key enables supplementary `NEEDS_REVIEW` extraction.
3. **NHTSA VIN-level recall lookup blocked** for third-party automation (401/403). Handled
   honestly; manual verification at nhtsa.gov remains the human path.
4. Report parser targets the standard Part 573 layout; unusual layouts fall back to the
   reviewed fixture (by design) — extending parsers to other campaigns is future work.
5. MVP auth is a single manager account with in-process HMAC tokens (sufficient per spec);
   production hardening would add token revocation, rate limiting on login, and TLS-terminated
   session cookies.
6. Stretch goals (repair-invoice upload/closure, additional campaigns) intentionally **not
   built** — core flow reliability took priority, per the brief.

## 14. Exact command to start the application

```bash
# From a fresh checkout, at the repo root:
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt \
  && cp .env.example .env \
  && (cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &) \
  && cd frontend && npm install && npm run dev
# → open http://localhost:5173   (login: fleet.manager / ROUTESHIELD_MANAGER_PASSWORD from .env)
```

Docker (on the Vultr VM): `docker compose up -d --build` → `http://<vm-ip>:8080`.

---

### Day-1 acceptance gate — status

| # | Requirement | Status |
|---|---|---|
| 1 | Real NHTSA recall fetched (or clearly identified cache) | ✅ live-verified (API + PDF) |
| 2 | Applicability criteria with references to the original report | ✅ 17 criteria, page + snippet + method + confidence + review status |
| 3 | Synthetic fleet assessed deterministically | ✅ 15/15 vehicles, reasons per classification, benchmark 40/40 |
| 4 | Affected routes & feasible replacements calculated | ✅ 5 disrupted, 2 recoverable, 3 with explicit unresolved issues |
| 5 | Manager approval persists state + audit | ✅ transactional, re-validated, append-only audit, UI transition |

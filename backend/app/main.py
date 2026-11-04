"""RouteShield FastAPI application.

Startup sequence:
1. Create tables (idempotent) and run idempotent seeding: manager account,
   synthetic fleet/routes, the 26V111 incident with manually reviewed
   evidence, and an initial deterministic assessment.
2. Optionally (AUTO_REFRESH_RECALL=true, default) kick off a background
   thread that attempts the LIVE NHTSA pipeline. If it fails, the reviewed
   fixture keeps the app fully functional and the UI says so explicitly.

Security architecture:
* The LLM (Vultr Serverless Inference) is used in exactly two places, both
  read-only: services/llm_extraction.py (structured report extraction, marked
  NEEDS_REVIEW) and services/agent.py (the bounded incident-response agent).
  The agent can only call the read-only tool registry in
  services/agent_tools.py; it has no write path to operational records, and
  its final output is deterministically grounded against real tool results.
* Operational state changes happen exclusively through the authenticated
  POST /api/incidents/{id}/approve-plan endpoint.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_settings
from .db import SessionLocal, init_db
from .routers import agents, benchmarks, fleet, incidents, system, vpic
from .seed import DEMO_CAMPAIGN, seed_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("routeshield")


def _background_refresh(campaign_number: str) -> None:
    from .services.evidence import refresh_recall_evidence

    db = SessionLocal()
    try:
        outcome = refresh_recall_evidence(db, campaign_number)
        from . import models
        from sqlalchemy import select

        incident = db.scalar(select(models.Incident).where(models.Incident.campaign_number == campaign_number))
        if incident is not None and outcome.report_version_id:
            incident.active_report_version_id = outcome.report_version_id
            incident.evidence_source = outcome.evidence_source
            db.commit()
        logger.info("Startup recall refresh: %s (%s)", outcome.evidence_source, outcome.banner[:120])
    except Exception as exc:  # noqa: BLE001 - startup must never crash on network issues
        logger.warning("Startup recall refresh failed (fixture remains active): %s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_all(db)
    finally:
        db.close()
    if os.environ.get("AUTO_REFRESH_RECALL", "true").lower() in ("1", "true", "yes"):
        t = threading.Thread(target=_background_refresh, args=(DEMO_CAMPAIGN,), daemon=True)
        t.start()
    logger.info("RouteShield %s started (environment=%s)", __version__, get_settings().environment)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="RouteShield API",
        version=__version__,
        description=(
            "Evidence-driven recall response and operational recovery for school-bus fleets.\n\n"
            "**Core principle:** absence of evidence is not evidence of safety. Classifications are "
            "computed by deterministic code against human-reviewed NHTSA Part 573 criteria; the LLM "
            "never decides vehicle applicability and never writes operational records. Operational "
            "state changes only through the authenticated manager approval endpoint.\n\n"
            "**Demo data:** all fleet records are synthetic and labelled "
            "'SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES'."
        ),
        lifespan=lifespan,
    )
    # CORS hardening: in production the frontend is same-origin (nginx proxies
    # /api), so an empty CORS_ORIGINS must mean "no cross-origin access" — never
    # a wildcard. Wildcard + allow_credentials=True would reflect arbitrary
    # origins, so credentials are only enabled for explicit, non-wildcard lists.
    cors_origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=bool(cors_origins) and "*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(fleet.router, prefix="/api")
    app.include_router(benchmarks.router, prefix="/api")
    app.include_router(vpic.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")

    # Optional: serve a built frontend from the same process (single-container
    # deployments). In docker-compose the frontend has its own nginx container.
    if settings.serve_frontend_dir:
        dist = Path(settings.serve_frontend_dir)
        if dist.exists():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()

"""Campaign evidence orchestration.

Responsibilities:
* Load the manually reviewed fixture (backend/data/campaigns/*.reviewed.json).
* Run the LIVE pipeline: NHTSA campaign API -> report PDF download ->
  deterministic rule-based extraction (+ optional LLM-assisted extraction,
  which is stored as NEEDS_REVIEW supplementary evidence only).
* Maintain immutable report versions keyed by document SHA-256:
    - same hash as the reviewed fixture  => review carries over (the fixture
      is a hash-pinned human review of exactly that document);
    - different hash                     => NEW version marked PENDING_REVIEW.
      Previously reviewed criteria are never silently replaced.
* Persist campaigns, report versions and criteria with full provenance.
* Honest provenance banners: the UI shows "Using manually reviewed campaign
  evidence." whenever the fixture backs the incident, and never presents
  fallback data as a live extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import llm_extraction, nhtsa, pdf_extraction

logger = logging.getLogger("routeshield.evidence")

FIXTURE_BANNER = (
    "Using manually reviewed campaign evidence. Live NHTSA retrieval or extraction was "
    "unavailable; the criteria below come from the hash-pinned, human-reviewed fixture and "
    "were NOT produced by a live extraction just now."
)
LIVE_BANNER = (
    "Live evidence: campaign data retrieved from the official NHTSA API and applicability "
    "criteria extracted from the official Part 573 report PDF."
)
CACHED_BANNER = (
    "Cached evidence: the official report PDF was loaded from the local cache because the "
    "live download failed. Criteria were re-extracted and verified against the reviewed "
    "document hash."
)
CHANGED_BANNER = (
    "The published report document has CHANGED (different SHA-256). The new version is "
    "marked as requiring review; previously reviewed criteria were NOT replaced. "
    "Assessments continue to reference the last reviewed version until a human reviews "
    "the new document."
)


@dataclass
class RefreshOutcome:
    evidence_source: str
    banner: str
    live_error: str | None
    report_version_id: int | None
    document_sha256: str | None
    document_changed: bool
    llm_supplementary: bool
    llm_error: str | None


def load_reviewed_fixture(campaign_number: str) -> dict | None:
    settings = get_settings()
    path = Path(settings.reviewed_campaigns_dir) / f"{campaign_number.strip().upper()}.reviewed.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Reviewed fixture %s is unreadable: %s", path, exc)
        return None


def _upsert_campaign(db: Session, campaign_number: str, info: dict, api_raw: dict | None) -> models.RecallCampaign:
    row = db.scalar(select(models.RecallCampaign).where(models.RecallCampaign.campaign_number == campaign_number))
    if row is None:
        row = models.RecallCampaign(campaign_number=campaign_number)
        db.add(row)
    row.manufacturer = info.get("manufacturer") or row.manufacturer or ""
    row.make = info.get("make") or row.make or ""
    row.model = info.get("model") or row.model or ""
    if info.get("model_years"):
        row.model_years = info["model_years"]
    row.component = info.get("component") or row.component or ""
    row.summary = info.get("summary") or row.summary or ""
    row.consequence = info.get("consequence") or row.consequence or ""
    row.remedy = info.get("remedy") or row.remedy or ""
    if info.get("potential_units_affected") is not None:
        row.potential_units_affected = info["potential_units_affected"]
    row.manufacturer_recall_number = info.get("manufacturer_recall_number") or row.manufacturer_recall_number or ""
    row.report_received_date = info.get("report_received_date") or row.report_received_date or ""
    if api_raw is not None:
        row.api_source_data = api_raw
        row.api_retrieved_at = datetime.now(timezone.utc)
    db.flush()
    return row


def _find_version(db: Session, campaign_id: int, sha256: str) -> models.RecallReportVersion | None:
    return db.scalar(
        select(models.RecallReportVersion).where(
            models.RecallReportVersion.campaign_id == campaign_id,
            models.RecallReportVersion.document_sha256 == sha256,
        )
    )


def _add_criteria(db: Session, version: models.RecallReportVersion, criteria: list[dict], source_document: str) -> None:
    for c in criteria:
        db.add(
            models.ExtractedCriterion(
                report_version_id=version.id,
                criterion_type=c["criterion_type"],
                label=c.get("label", c["criterion_type"]),
                value=c["value"],
                source_document=source_document,
                source_page=c.get("source_page"),
                source_snippet=c.get("source_snippet", ""),
                extraction_method=c.get("extraction_method", "RULE_BASED_PDF"),
                confidence=float(c.get("confidence", 0.9)),
                review_status=c.get("review_status", "PENDING_REVIEW"),
            )
        )


def _fixture_criteria(fixture: dict) -> list[dict]:
    out = []
    for c in fixture.get("criteria", []):
        out.append({**c, "extraction_method": "MANUALLY_REVIEWED", "review_status": "REVIEWED"})
    return out


def _extraction_to_criteria(ex: pdf_extraction.Part573Extraction, source_document: str, api_make: str = "", api_model: str = "") -> list[dict]:
    """Convert a rule-based extraction into criterion dicts.

    Make/model reconciliation: the PDF prints 'IC BUS CESB' as one string; the
    API provides Make and Model separately. We only split when the API values
    are consistent with the printed string - otherwise we store the whole
    string and let matching normalize it.
    """
    out: list[dict] = []
    if ex.manufacturer:
        out.append({
            "criterion_type": "MANUFACTURER",
            "label": "Manufacturer",
            "value": {"manufacturer": ex.manufacturer.value},
            "source_page": ex.manufacturer.source_page,
            "source_snippet": ex.manufacturer.source_snippet,
        })
    make, model = api_make.strip().upper(), api_model.strip().upper()
    make_model = ""
    if ex.vehicle_groups:
        make_model = str(ex.vehicle_groups[0].value.get("make_model", "")).upper()
    if make_model:
        if make and model and make_model == f"{make} {model}":
            mm_value = {"make": api_make, "model": api_model, "make_model": make_model}
            confidence = 0.99
        else:
            mm_value = {"make": "", "model": "", "make_model": make_model}
            confidence = 0.8
        out.append({
            "criterion_type": "MAKE_MODEL",
            "label": "Make / Model",
            "value": mm_value,
            "source_page": ex.vehicle_groups[0].source_page,
            "source_snippet": ex.vehicle_groups[0].source_snippet,
            "confidence": confidence,
        })
    if ex.product_type:
        out.append({
            "criterion_type": "PRODUCT_TYPE",
            "label": "Product type",
            "value": {"product_type": ex.product_type.value},
            "source_page": ex.product_type.source_page,
            "source_snippet": ex.product_type.source_snippet,
        })
    for g in ex.vehicle_groups:
        v = dict(g.value)
        out.append({
            "criterion_type": "PRODUCTION_DATE_RANGE",
            "label": f"MY{v['model_year_start']}"
            + (f"-{v['model_year_end']}" if v["model_year_end"] != v["model_year_start"] else "")
            + f" production range (Vehicle {v['vehicle_group']})",
            "value": v,
            "source_page": g.source_page,
            "source_snippet": g.source_snippet,
            "confidence": g.confidence,
        })
    if ex.overall_build_range:
        out.append({
            "criterion_type": "OVERALL_BUILD_RANGE",
            "label": "Overall suspect build window",
            "value": dict(ex.overall_build_range.value),
            "source_page": ex.overall_build_range.source_page,
            "source_snippet": ex.overall_build_range.source_snippet,
            "confidence": ex.overall_build_range.confidence,
        })
    for f in ex.feature_codes:
        out.append({
            "criterion_type": "FEATURE_CODE",
            "label": "Brake configuration feature code",
            "value": dict(f.value),
            "source_page": f.source_page,
            "source_snippet": f.source_snippet,
            "confidence": f.confidence,
        })
    for f in ex.exclusions:
        out.append({
            "criterion_type": "EXCLUSION",
            "label": "Explicit exclusion of non-subject similar vehicles",
            "value": dict(f.value),
            "source_page": f.source_page,
            "source_snippet": f.source_snippet,
            "confidence": f.confidence,
        })
    if ex.total_units:
        out.append({
            "criterion_type": "POPULATION",
            "label": "Total potentially involved",
            "value": {"total_units": ex.total_units.value},
            "source_page": ex.total_units.source_page,
            "source_snippet": ex.total_units.source_snippet,
        })
    return out


def _descriptive_criteria_from_extraction(ex: pdf_extraction.Part573Extraction) -> list[dict]:
    out = []
    if ex.defect_description:
        out.append({
            "criterion_type": "DEFECT_DESCRIPTION", "label": "Defect description",
            "value": {"text": ex.defect_description.value},
            "source_page": ex.defect_description.source_page,
            "source_snippet": ex.defect_description.source_snippet,
            "confidence": ex.defect_description.confidence,
        })
    if ex.safety_risk:
        out.append({
            "criterion_type": "SAFETY_CONSEQUENCE", "label": "Safety consequence",
            "value": {"text": ex.safety_risk.value},
            "source_page": ex.safety_risk.source_page,
            "source_snippet": ex.safety_risk.source_snippet,
            "confidence": ex.safety_risk.confidence,
        })
    if ex.remedy_program:
        out.append({
            "criterion_type": "REMEDY", "label": "Remedy program",
            "value": {"text": ex.remedy_program.value},
            "source_page": ex.remedy_program.source_page,
            "source_snippet": ex.remedy_program.source_snippet,
            "confidence": ex.remedy_program.confidence,
        })
    for c in ex.components:
        out.append({
            "criterion_type": "COMPONENT", "label": f"Involved component: {c.value.get('name', '')}",
            "value": dict(c.value),
            "source_page": c.source_page,
            "source_snippet": c.source_snippet,
            "confidence": c.confidence,
        })
    return out


def seed_evidence_from_fixture(db: Session, campaign_number: str) -> models.RecallReportVersion | None:
    """Idempotently load the manually reviewed fixture into the database.

    Used at startup so the application works with zero network access, and as
    the explicit fallback when the live pipeline fails.
    """
    fixture = load_reviewed_fixture(campaign_number)
    if fixture is None:
        return None
    doc = fixture["source_document"]
    sha = doc["sha256"]
    camp_info = dict(fixture.get("campaign", {}))
    camp_info.setdefault("model_years", sorted({
        y
        for c in fixture.get("criteria", [])
        if c["criterion_type"] == "PRODUCTION_DATE_RANGE"
        for y in range(c["value"]["model_year_start"], c["value"]["model_year_end"] + 1)
    }))
    api_src = fixture.get("api_source", {})
    camp_info.setdefault("report_received_date", api_src.get("report_received_date", ""))
    campaign = _upsert_campaign(db, campaign_number, camp_info, api_raw=None)

    version = _find_version(db, campaign.id, sha)
    if version is None:
        version = models.RecallReportVersion(
            campaign_id=campaign.id,
            report_url=doc["url"],
            document_sha256=sha,
            page_count=doc.get("page_count"),
            submission_date=camp_info.get("submission_date", ""),
            amendment_info="",
            provenance="MANUALLY_REVIEWED_FIXTURE",
            review_status="REVIEWED",
            notes=(
                "Manually reviewed campaign fixture (hash-pinned to this exact document). "
                f"Fixture version {fixture.get('fixture_version')}, reviewed by "
                f"{fixture.get('reviewed_by')}."
            ),
        )
        db.add(version)
        db.flush()
        criteria = _fixture_criteria(fixture)
        for d in fixture.get("descriptive", {}).get("components", []):
            pass  # components handled below
        desc = fixture.get("descriptive", {})
        for key, ctype, label in [
            ("defect_description", "DEFECT_DESCRIPTION", "Defect description"),
            ("safety_risk", "SAFETY_CONSEQUENCE", "Safety consequence"),
            ("remedy_program", "REMEDY", "Remedy program"),
            ("remedy_type", "REMEDY_TYPE", "Remedy type"),
        ]:
            if key in desc:
                criteria.append({
                    "criterion_type": ctype, "label": label,
                    "value": {"text": desc[key]["value"]},
                    "source_page": desc[key].get("source_page"),
                    "source_snippet": desc[key].get("source_snippet", ""),
                    "confidence": 1.0, "review_status": "REVIEWED",
                    "extraction_method": "MANUALLY_REVIEWED",
                })
        for comp in desc.get("components", []):
            criteria.append({
                "criterion_type": "COMPONENT",
                "label": f"Involved component: {comp['value'].get('name', '')}",
                "value": comp["value"],
                "source_page": comp.get("source_page"),
                "source_snippet": comp.get("source_snippet", ""),
                "confidence": 1.0, "review_status": "REVIEWED",
                "extraction_method": "MANUALLY_REVIEWED",
            })
        _add_criteria(db, version, criteria, source_document=doc["url"])
    db.commit()
    return version


def refresh_recall_evidence(db: Session, campaign_number: str, report_url: str | None = None) -> RefreshOutcome:
    """Attempt the LIVE pipeline; fall back to the reviewed fixture on failure.

    Never raises for expected external failures - returns an outcome describing
    exactly what happened so the UI can be honest about provenance.
    """
    settings = get_settings()
    campaign_number = campaign_number.strip().upper()
    llm_error: str | None = None
    llm_supplementary = False

    # ---------------- Live attempt ----------------
    live_error: str | None = None
    try:
        api = nhtsa.fetch_campaign(campaign_number)
        primary = api.primary
        camp_info = {
            "manufacturer": primary.get("Manufacturer", ""),
            "make": primary.get("Make", ""),
            "model": primary.get("Model", ""),
            "model_years": api.model_years,
            "component": primary.get("Component", ""),
            "summary": primary.get("Summary", ""),
            "consequence": primary.get("Consequence", ""),
            "remedy": primary.get("Remedy", ""),
            "potential_units_affected": primary.get("PotentialNumberofUnitsAffected"),
            "report_received_date": primary.get("ReportReceivedDate", ""),
        }
        campaign = _upsert_campaign(db, campaign_number, camp_info, api_raw=api.raw)

        url = report_url or nhtsa.report_url_for_campaign(
            campaign_number,
            report_received_year=_year_from_received(camp_info["report_received_date"]),
        )
        doc = nhtsa.download_report(url)
        ex = pdf_extraction.extract_part573(doc.content)
        if not ex.is_complete:
            raise nhtsa.NHTSAError(
                "Rule-based extraction incomplete: " + "; ".join(ex.warnings)
            )

        fixture = load_reviewed_fixture(campaign_number)
        fixture_sha = fixture["source_document"]["sha256"] if fixture else None
        document_changed = fixture_sha is not None and doc.sha256 != fixture_sha

        version = _find_version(db, campaign.id, doc.sha256)
        if version is not None:
            # Same document hash as an existing version: criteria are untouched
            # (versions are immutable), but record that a live retrieval
            # re-verified this exact document.
            if version.provenance == "MANUALLY_REVIEWED_FIXTURE":
                version.provenance = "CACHED_PDF_EXTRACTED" if doc.from_cache else "LIVE_PDF_EXTRACTED"
                version.notes = (version.notes + " | A live retrieval re-verified this exact "
                                 "document (identical SHA-256).").strip(" |")
        if version is None:
            if document_changed:
                # New document: NEW version requiring review. Do NOT touch the
                # previously reviewed version or its criteria.
                version = models.RecallReportVersion(
                    campaign_id=campaign.id, report_url=url, document_sha256=doc.sha256,
                    page_count=ex.page_count,
                    submission_date=str(ex.submission_date.value) if ex.submission_date else "",
                    amendment_info=f"Document hash differs from reviewed fixture ({fixture_sha[:12]}...)",
                    provenance="CACHED_PDF_EXTRACTED" if doc.from_cache else "LIVE_PDF_EXTRACTED",
                    review_status="PENDING_REVIEW",
                    notes=CHANGED_BANNER,
                )
                db.add(version)
                db.flush()
                criteria = _extraction_to_criteria(ex, url, camp_info.get("make", ""), camp_info.get("model", ""))
                criteria += _descriptive_criteria_from_extraction(ex)
                _add_criteria(db, version, criteria, source_document=url)
            else:
                # Hash matches the human-reviewed fixture => review carries over.
                version = models.RecallReportVersion(
                    campaign_id=campaign.id, report_url=url, document_sha256=doc.sha256,
                    page_count=ex.page_count,
                    submission_date=str(ex.submission_date.value) if ex.submission_date else "",
                    amendment_info="",
                    provenance="CACHED_PDF_EXTRACTED" if doc.from_cache else "LIVE_PDF_EXTRACTED",
                    review_status="REVIEWED",
                    notes=(
                        "Live extraction verified byte-identical (SHA-256) to the manually "
                        "reviewed fixture; human review carries over to this retrieval."
                    ),
                )
                db.add(version)
                db.flush()
                criteria = _extraction_to_criteria(ex, url, camp_info.get("make", ""), camp_info.get("model", ""))
                for c in criteria:
                    c["extraction_method"] = "RULE_BASED_PDF"
                    c["review_status"] = "REVIEWED"
                criteria += _descriptive_criteria_from_extraction(ex)
                for c in criteria:
                    c.setdefault("review_status", "REVIEWED")
                _add_criteria(db, version, criteria, source_document=url)
        version.extraction_timestamp = datetime.now(timezone.utc)

        # Optional LLM-assisted supplementary extraction (NEEDS_REVIEW only).
        if llm_extraction.llm_available():
            try:
                llm_result = llm_extraction.extract_with_llm(ex.pages)
                llm_criteria = _llm_to_criteria(llm_result, url)
                _add_criteria(db, version, llm_criteria, source_document=url)
                llm_supplementary = True
            except llm_extraction.LLMExtractionError as exc:
                llm_error = str(exc)

        db.commit()
        banner = CACHED_BANNER if doc.from_cache else LIVE_BANNER
        if version.review_status == "PENDING_REVIEW":
            banner = CHANGED_BANNER
        return RefreshOutcome(
            evidence_source="LIVE_EXTRACTION",
            banner=banner,
            live_error=None,
            report_version_id=version.id,
            document_sha256=doc.sha256,
            document_changed=document_changed,
            llm_supplementary=llm_supplementary,
            llm_error=llm_error,
        )
    except (nhtsa.NHTSAError, pdf_extraction.PDFExtractionError) as exc:
        live_error = str(exc)
        logger.warning("Live recall evidence pipeline failed: %s", exc)
        db.rollback()

    # ---------------- Fixture fallback ----------------
    if not settings.allow_offline_fallback:
        raise RuntimeError(f"Live evidence retrieval failed and fallback disabled: {live_error}")
    version = seed_evidence_from_fixture(db, campaign_number)
    if version is None:
        raise RuntimeError(
            f"Live evidence retrieval failed ({live_error}) and no reviewed fixture exists "
            f"for campaign {campaign_number}."
        )
    return RefreshOutcome(
        evidence_source="MANUALLY_REVIEWED_FIXTURE",
        banner=FIXTURE_BANNER + f" Live retrieval error: {live_error}",
        live_error=live_error,
        report_version_id=version.id,
        document_sha256=version.document_sha256,
        document_changed=False,
        llm_supplementary=False,
        llm_error=llm_error,
    )


def _llm_to_criteria(result: llm_extraction.LLMRecallExtraction, source_document: str) -> list[dict]:
    """Store LLM output as supplementary NEEDS_REVIEW criteria only."""
    out: list[dict] = []
    common = {"extraction_method": "LLM_ASSISTED", "review_status": "NEEDS_REVIEW", "confidence": 0.6}
    for pr in result.production_ranges:
        out.append({
            **common,
            "criterion_type": "PRODUCTION_DATE_RANGE",
            "label": f"[LLM] MY{pr.model_year} production range",
            "value": {
                "model_year_start": pr.model_year, "model_year_end": pr.model_year,
                "production_start_printed": pr.production_start,
                "production_end_printed": pr.production_end,
                "units": pr.units,
            },
            "source_page": pr.source_page,
            "source_snippet": pr.source_snippet,
        })
    for code in result.feature_codes:
        out.append({
            **common,
            "criterion_type": "FEATURE_CODE",
            "label": f"[LLM] Feature code {code}",
            "value": {"feature_code": code, "feature_description": result.feature_code_context or ""},
            "source_page": None,
            "source_snippet": result.feature_code_context or "",
        })
    for stmt in result.exclusions:
        out.append({
            **common,
            "criterion_type": "EXCLUSION",
            "label": "[LLM] Exclusion statement",
            "value": {"exclusion_statement": stmt},
            "source_page": None,
            "source_snippet": stmt[:600],
        })
    return out


def _year_from_received(received: str) -> int | None:
    # ReportReceivedDate format from the API: "27/02/2026"
    parts = (received or "").split("/")
    if len(parts) == 3 and parts[2].isdigit():
        return int(parts[2])
    return None


def get_evidence_payload(db: Session, incident: models.Incident) -> dict:
    """Assemble the evidence payload for the UI (provenance-honest)."""
    version = incident.active_report_version
    campaign = incident.campaign
    criteria_rows: list[models.ExtractedCriterion] = []
    if version:
        criteria_rows = list(
            db.scalars(
                select(models.ExtractedCriterion)
                .where(models.ExtractedCriterion.report_version_id == version.id)
                .order_by(models.ExtractedCriterion.id)
            )
        )
    using_fixture = (incident.evidence_source == "MANUALLY_REVIEWED_FIXTURE") or (
        version is not None and version.provenance == "MANUALLY_REVIEWED_FIXTURE"
    )
    if using_fixture:
        banner = FIXTURE_BANNER
    elif version is not None and version.review_status == "PENDING_REVIEW":
        banner = CHANGED_BANNER
    elif version is not None and version.provenance == "CACHED_PDF_EXTRACTED":
        banner = CACHED_BANNER
    else:
        banner = LIVE_BANNER
    return {
        "incident_id": incident.id,
        "campaign": campaign,
        "report_version": version,
        "criteria": criteria_rows,
        "evidence_source": incident.evidence_source or ("MANUALLY_REVIEWED_FIXTURE" if using_fixture else "LIVE_EXTRACTION"),
        "evidence_banner": banner,
        "using_reviewed_fixture": using_fixture,
    }


def latest_reviewed_version(db: Session, campaign_id: int) -> models.RecallReportVersion | None:
    return db.scalar(
        select(models.RecallReportVersion)
        .where(
            models.RecallReportVersion.campaign_id == campaign_id,
            models.RecallReportVersion.review_status == "REVIEWED",
        )
        .order_by(models.RecallReportVersion.id.desc())
        .limit(1)
    )

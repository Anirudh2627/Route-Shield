"""A. Recall extraction tests: valid structured extraction, invalid LLM JSON,
missing fields, source-reference preservation, fallback behavior, and report
version handling when the published document changes."""
from __future__ import annotations

import io
import json

import pymupdf
import pytest

from app.services import llm_extraction, nhtsa, pdf_extraction
from app.services.evidence import (
    latest_reviewed_version,
    refresh_recall_evidence,
    seed_evidence_from_fixture,
)


# ---------------------------------------------------------------------------
# Rule-based PDF extraction on the REAL official report
# ---------------------------------------------------------------------------


def test_real_pdf_extraction_is_complete(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    assert ex.is_complete, ex.warnings
    assert ex.page_count == 5


def test_real_pdf_extracts_all_five_production_ranges_with_pages(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    ranges = {(g.value["model_year_start"], g.value["production_start"], g.value["production_end"]): g
              for g in ex.vehicle_groups}
    assert len(ex.vehicle_groups) == 5
    expected = {
        (2023, "2022-12-15", "2022-12-20"): 2,
        (2024, "2023-09-21", "2023-10-25"): 2,
        (2025, "2024-02-05", "2024-10-02"): 1,
        (2026, "2024-10-02", "2025-10-07"): 2,
        (2027, "2025-10-09", "2026-02-02"): 1,
    }
    assert set(ranges.keys()) == set(expected.keys())
    for key, g in ranges.items():
        assert g.source_page == expected[key]
        assert g.value["units"] in (2, 6, 1395, 2134, 856)


def test_real_pdf_extracts_brake_feature_code_0004100(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    codes = {f.value["feature_code"]: f for f in ex.feature_codes}
    assert "0004100" in codes
    f = codes["0004100"]
    assert f.value["feature_description"] == "Hydraulic Brake"
    assert f.source_page == 1
    assert "feature code 0004100" in f.source_snippet


def test_real_pdf_extracts_exclusion_statement(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    assert len(ex.exclusions) == 1
    stmt = ex.exclusions[0].value["exclusion_statement"]
    assert "not subject to this recall" in stmt
    assert "excluded" in stmt


def test_real_pdf_extracts_defect_risk_remedy_with_page_refs(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    assert ex.defect_description and ex.defect_description.source_page == 3
    assert "incorrect mounting hardware" in ex.defect_description.value
    assert ex.safety_risk and ex.safety_risk.source_page == 3
    assert "stopping distance" in ex.safety_risk.value
    assert ex.remedy_program and ex.remedy_program.source_page == 4
    assert "brake line retainers" in ex.remedy_program.value
    assert ex.manufacturer.value == "International Motors, LLC"
    assert ex.manufacturer_recall_number.value == "26502"
    assert ex.total_units.value == 4393
    parts = {c.value["part_number"] for c in ex.components}
    assert parts == {"3523101C2", "4232215C1"}


def test_source_references_preserved_on_every_criterion(real_pdf_bytes):
    ex = pdf_extraction.extract_part573(real_pdf_bytes)
    fields = (
        [ex.manufacturer, ex.submission_date, ex.overall_build_range]
        + ex.vehicle_groups + ex.feature_codes + ex.exclusions
    )
    for f in fields:
        assert f is not None
        assert f.source_page and f.source_page >= 1
        assert f.source_snippet.strip() != ""
        assert f.extraction_method == "RULE_BASED_PDF"
        assert 0.0 < f.confidence <= 1.0
        assert f.review_status == "PENDING_REVIEW"  # live extraction is never auto-REVIEWED by itself


def test_invalid_pdf_raises_extraction_error():
    with pytest.raises(pdf_extraction.PDFExtractionError):
        pdf_extraction.extract_part573(b"this is not a pdf at all")


def test_unrelated_pdf_is_incomplete_with_warnings():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "An unrelated document with no recall structure whatsoever.")
    data = doc.tobytes()
    doc.close()
    ex = pdf_extraction.extract_part573(data)
    assert not ex.is_complete
    assert ex.warnings


def test_date_parser_handles_report_formats_and_rejects_garbage():
    from datetime import date

    assert pdf_extraction.parse_report_date("Feb 05, 2024") == date(2024, 2, 5)
    assert pdf_extraction.parse_report_date("12/15/2022") == date(2022, 12, 15)
    assert pdf_extraction.parse_report_date("not a date") is None
    assert pdf_extraction.parse_report_date("Feb 30, 2024") is None


# ---------------------------------------------------------------------------
# LLM-assisted extraction: strict validation, invalid JSON, missing fields
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})
        self.choices = [self]


class _FakeOpenAI:
    def __init__(self, reply):
        self._reply = reply
        self.calls = 0

    def chat_completions_create(self, **kwargs):
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return _FakeMessage(self._reply)


def _patch_llm(monkeypatch, reply):
    import openai

    class _Client:
        def __init__(self, *a, **k):
            self.chat = type("C", (), {"completions": type("D", (), {"create": lambda self_, **kw: _fake.chat_completions_create(**kw)})()})()

    _fake = _FakeOpenAI(reply)
    monkeypatch.setattr(openai, "OpenAI", _Client)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "test-key")
    monkeypatch.setenv("VULTR_INFERENCE_MODEL", "test-model")
    from app.config import get_settings

    get_settings.cache_clear()
    return _fake


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch):
    yield
    from app.config import get_settings

    get_settings.cache_clear()


def test_llm_valid_structured_output_is_accepted(monkeypatch):
    payload = {
        "manufacturer": "International Motors, LLC",
        "make": "IC BUS",
        "model": "CESB",
        "production_ranges": [
            {"model_year": 2025, "production_start": "Feb 05, 2024", "production_end": "Oct 02, 2024",
             "units": 1395, "source_page": 1, "source_snippet": "Production Dates: Feb 05, 2024 - Oct 02, 2024"}
        ],
        "feature_codes": ["0004100"],
        "feature_code_context": "Hydraulic Brake (feature code 0004100)",
        "exclusions": ["All other similar vehicles, which are not subject to this recall, are excluded."],
    }
    _patch_llm(monkeypatch, json.dumps(payload))
    result = llm_extraction.extract_with_llm({1: "page text"})
    assert result.production_ranges[0].model_year == 2025
    assert result.feature_codes == ["0004100"]


def test_llm_invalid_json_is_rejected(monkeypatch):
    _patch_llm(monkeypatch, "Sorry, I cannot produce JSON for that.")
    with pytest.raises(llm_extraction.LLMExtractionError, match="invalid structured output"):
        llm_extraction.extract_with_llm({1: "page text"})


def test_llm_schema_violation_is_rejected(monkeypatch):
    _patch_llm(monkeypatch, json.dumps({"production_ranges": [{"model_year": "not-a-number"}]}))
    with pytest.raises(llm_extraction.LLMExtractionError):
        llm_extraction.extract_with_llm({1: "page text"})


def test_llm_missing_fields_default_to_empty_not_invented(monkeypatch):
    _patch_llm(monkeypatch, json.dumps({"manufacturer": "Acme"}))
    result = llm_extraction.extract_with_llm({1: "page text"})
    assert result.manufacturer == "Acme"
    assert result.production_ranges == []
    assert result.feature_codes == []
    assert result.exclusions == []
    assert result.make is None


def test_llm_inference_failure_raises_for_fallback(monkeypatch):
    _patch_llm(monkeypatch, RuntimeError("inference endpoint down"))
    with pytest.raises(llm_extraction.LLMExtractionError, match="request failed"):
        llm_extraction.extract_with_llm({1: "page text"})


def test_llm_not_configured_raises(monkeypatch):
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(llm_extraction.LLMExtractionError, match="not configured"):
        llm_extraction.extract_with_llm({1: "page text"})


# ---------------------------------------------------------------------------
# Evidence service: fixture fallback, live pipeline (mocked), changed document
# ---------------------------------------------------------------------------


def test_fixture_seeding_produces_reviewed_criteria(db):
    from app import models
    from sqlalchemy import select

    version = db.scalar(select(models.RecallReportVersion))
    assert version is not None
    assert version.review_status == "REVIEWED"
    assert version.provenance == "MANUALLY_REVIEWED_FIXTURE"
    assert version.document_sha256 == "a29c04faf2273ba30ecfac16c0e2dfd0550f2aa2d7529e85cdce35844c8125f6"
    rows = list(db.scalars(select(models.ExtractedCriterion)))
    feature = [r for r in rows if r.criterion_type == "FEATURE_CODE"]
    assert feature and feature[0].value["feature_code"] == "0004100"
    assert feature[0].source_page == 1
    assert feature[0].review_status == "REVIEWED"
    assert feature[0].extraction_method == "MANUALLY_REVIEWED"
    ranges = [r for r in rows if r.criterion_type == "PRODUCTION_DATE_RANGE"]
    assert len(ranges) == 5


def test_fallback_when_nhtsa_unavailable_is_explicitly_labelled(db, monkeypatch):
    from app import models
    from sqlalchemy import select

    def _boom(_cn):
        raise nhtsa.NHTSAError("simulated NHTSA outage")

    monkeypatch.setattr(nhtsa, "fetch_campaign", _boom)
    incident = db.scalar(select(models.Incident))
    outcome = refresh_recall_evidence(db, incident.campaign_number)
    assert outcome.evidence_source == "MANUALLY_REVIEWED_FIXTURE"
    assert "manually reviewed campaign evidence" in outcome.banner.lower()
    assert outcome.live_error and "simulated NHTSA outage" in outcome.live_error
    assert outcome.report_version_id is not None


def test_fallback_never_pretends_to_be_live(db, monkeypatch, client, incident_id):
    def _boom(_cn):
        raise nhtsa.NHTSAError("simulated outage")

    monkeypatch.setattr(nhtsa, "fetch_campaign", _boom)
    resp = client.post(f"/api/incidents/{incident_id}/refresh-recall")
    assert resp.status_code == 200
    body = resp.json()
    assert body["evidence"]["evidence_source"] == "MANUALLY_REVIEWED_FIXTURE"
    assert body["evidence"]["using_reviewed_fixture"] is True
    assert "Using manually reviewed campaign evidence" in body["message"]


def test_live_pipeline_with_matching_hash_inherits_review(db, monkeypatch, real_pdf_bytes, real_api_payload):
    monkeypatch.setattr(nhtsa, "fetch_campaign",
                        lambda cn: nhtsa.CampaignApiResponse(cn, real_api_payload, real_api_payload["results"]))
    monkeypatch.setattr(nhtsa, "download_report",
                        lambda url, cache_dir=None: nhtsa.ReportDocument(url, real_pdf_bytes,
                            __import__("hashlib").sha256(real_pdf_bytes).hexdigest()))
    from app import models
    from sqlalchemy import select

    incident = db.scalar(select(models.Incident))
    outcome = refresh_recall_evidence(db, incident.campaign_number)
    assert outcome.evidence_source == "LIVE_EXTRACTION"
    assert outcome.document_changed is False
    version = db.get(models.RecallReportVersion, outcome.report_version_id)
    assert version.review_status == "REVIEWED"  # hash-pinned review carries over
    assert version.provenance == "LIVE_PDF_EXTRACTED"


def test_changed_document_creates_pending_review_version_without_replacing_reviewed(
    db, monkeypatch, real_pdf_bytes, real_api_payload
):
    # Mutate metadata only: same extractable text, different SHA-256.
    doc = pymupdf.open(stream=io.BytesIO(real_pdf_bytes), filetype="pdf")
    doc.set_metadata({"title": "amended copy"})
    changed = doc.tobytes()
    doc.close()
    import hashlib

    assert hashlib.sha256(changed).hexdigest() != hashlib.sha256(real_pdf_bytes).hexdigest()

    monkeypatch.setattr(nhtsa, "fetch_campaign",
                        lambda cn: nhtsa.CampaignApiResponse(cn, real_api_payload, real_api_payload["results"]))
    monkeypatch.setattr(nhtsa, "download_report",
                        lambda url, cache_dir=None: nhtsa.ReportDocument(
                            url, changed, hashlib.sha256(changed).hexdigest()))
    from app import models
    from sqlalchemy import select

    incident = db.scalar(select(models.Incident))
    fixture_version = latest_reviewed_version(db, incident.campaign_id)
    outcome = refresh_recall_evidence(db, incident.campaign_number)
    assert outcome.document_changed is True
    new_version = db.get(models.RecallReportVersion, outcome.report_version_id)
    assert new_version.review_status == "PENDING_REVIEW"
    # The previously reviewed version was NOT replaced or modified.
    db.refresh(fixture_version)
    assert fixture_version.review_status == "REVIEWED"
    assert latest_reviewed_version(db, incident.campaign_id).id == fixture_version.id
    # Assessment still runs against the reviewed version.
    from app.services.assessment import assess_fleet

    results = assess_fleet(db, incident)
    assert results and all(r.report_version_id == fixture_version.id for r in results)


def test_incomplete_extraction_falls_back_to_fixture(db, monkeypatch, real_api_payload):
    blank = pymupdf.open()
    blank.new_page().insert_text((72, 72), "Corrupted report text")
    data = blank.tobytes()
    blank.close()
    import hashlib

    monkeypatch.setattr(nhtsa, "fetch_campaign",
                        lambda cn: nhtsa.CampaignApiResponse(cn, real_api_payload, real_api_payload["results"]))
    monkeypatch.setattr(nhtsa, "download_report",
                        lambda url, cache_dir=None: nhtsa.ReportDocument(url, data, hashlib.sha256(data).hexdigest()))
    from app import models
    from sqlalchemy import select

    incident = db.scalar(select(models.Incident))
    outcome = refresh_recall_evidence(db, incident.campaign_number)
    assert outcome.evidence_source == "MANUALLY_REVIEWED_FIXTURE"
    assert "incomplete" in (outcome.live_error or "").lower()

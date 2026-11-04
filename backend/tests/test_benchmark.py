"""G. Benchmark tests: determinism, correct metric computation, and the tie
between benchmark criteria and the manually reviewed campaign fixture."""
from __future__ import annotations

import copy

import pytest

from app.benchmark.cases import CASES, CATEGORY_INCOMPLETE
from app.benchmark.runner import (
    BENCHMARK_CAMPAIGN,
    PINNED_FIXTURE_SHA256,
    benchmark_criteria,
    benchmark_provenance,
    run_benchmark,
)
from app.services.evidence import load_reviewed_fixture


# ---------------------------------------------------------------------------
# Fixture tie: criteria come FROM the reviewed fixture, never a private copy
# ---------------------------------------------------------------------------


def test_benchmark_criteria_are_loaded_from_reviewed_fixture():
    c = benchmark_criteria()
    fixture = load_reviewed_fixture(BENCHMARK_CAMPAIGN)
    assert fixture is not None
    assert c.document_sha256 == fixture["source_document"]["sha256"] == PINNED_FIXTURE_SHA256
    assert c.feature_code == "0004100"
    assert c.feature_description == "Hydraulic Brake"
    assert len(c.ranges) == 5
    assert c.make == "IC BUS" and c.model == "CESB"
    assert c.exclusion_statement
    prov = benchmark_provenance()
    assert prov["hash_matches_criteria"] is True
    assert prov["document_sha256"] == PINNED_FIXTURE_SHA256


def test_benchmark_criteria_match_fixture_rows_exactly():
    """Every production window in the criteria must equal the fixture's."""
    c = benchmark_criteria()
    fixture = load_reviewed_fixture(BENCHMARK_CAMPAIGN)
    fixture_rows = sorted(
        (r["value"]["model_year_start"], r["value"]["production_start"], r["value"]["production_end"])
        for r in fixture["criteria"] if r["criterion_type"] == "PRODUCTION_DATE_RANGE"
    )
    criteria_rows = sorted(
        (r.model_year_start, r.production_start.isoformat(), r.production_end.isoformat())
        for r in c.ranges
    )
    assert criteria_rows == fixture_rows


def test_benchmark_refuses_fixture_with_changed_document_hash(monkeypatch):
    """Silent divergence guard: if the reviewed fixture's document hash
    changes, the benchmark must refuse to run until the pin is consciously
    updated (ground-truth labels are tied to the pinned document)."""
    fixture = copy.deepcopy(load_reviewed_fixture(BENCHMARK_CAMPAIGN))
    fixture["source_document"]["sha256"] = "b" * 64
    monkeypatch.setattr("app.services.evidence.load_reviewed_fixture", lambda cn: fixture)
    with pytest.raises(RuntimeError, match="pinned"):
        benchmark_criteria()
    with pytest.raises(RuntimeError, match="pinned"):
        run_benchmark()


def test_benchmark_refuses_fixture_missing_required_criteria(monkeypatch):
    fixture = copy.deepcopy(load_reviewed_fixture(BENCHMARK_CAMPAIGN))
    fixture["criteria"] = [c for c in fixture["criteria"] if c["criterion_type"] != "FEATURE_CODE"]
    monkeypatch.setattr("app.services.evidence.load_reviewed_fixture", lambda cn: fixture)
    with pytest.raises(RuntimeError, match="feature code"):
        benchmark_criteria()


def test_benchmark_refuses_unreviewed_fixture_criteria(monkeypatch):
    fixture = copy.deepcopy(load_reviewed_fixture(BENCHMARK_CAMPAIGN))
    fixture["criteria"][0]["review_status"] = "PENDING_REVIEW"
    monkeypatch.setattr("app.services.evidence.load_reviewed_fixture", lambda cn: fixture)
    with pytest.raises(RuntimeError, match="not REVIEWED"):
        benchmark_criteria()


def test_benchmark_refuses_missing_fixture(monkeypatch):
    monkeypatch.setattr("app.services.evidence.load_reviewed_fixture", lambda cn: None)
    with pytest.raises(RuntimeError, match="missing"):
        benchmark_criteria()


# ---------------------------------------------------------------------------
# Determinism and metric computation
# ---------------------------------------------------------------------------


def test_benchmark_is_deterministic():
    a = run_benchmark()
    b = run_benchmark()
    assert a["metrics"] == b["metrics"]
    assert a["confusion_matrix"] == b["confusion_matrix"]
    assert [c["predicted"] for c in a["cases"]] == [c["predicted"] for c in b["cases"]]
    assert a["sample_count"] == b["sample_count"] == len(CASES)


def test_sample_count_reported():
    result = run_benchmark()
    assert result["sample_count"] == len(CASES) >= 40
    assert result["label"] == "SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY"


def test_false_confirmation_rate_formula():
    """false_confirmation_rate = incomplete-evidence cases classified as
    CAMPAIGN_CRITERIA_MATCH / ALL incomplete-evidence cases."""
    result = run_benchmark()
    incomplete = [c for c in result["cases"] if c["category"] in CATEGORY_INCOMPLETE]
    expected_hits = sum(1 for c in incomplete if c["predicted"] == "CAMPAIGN_CRITERIA_MATCH")
    expected_rate = expected_hits / len(incomplete) if incomplete else None
    assert result["metrics"]["false_confirmation_rate"] == round(expected_rate, 6)
    assert result["metrics"]["false_confirmation_rate"] == 0.0, (
        "Engine must never confirm a match on incomplete evidence"
    )


def test_abstention_rate_formula():
    result = run_benchmark()
    incomplete = [c for c in result["cases"] if c["category"] in CATEGORY_INCOMPLETE]
    hits = sum(1 for c in incomplete if c["predicted"] == "VERIFICATION_REQUIRED")
    assert result["metrics"]["correct_abstention_rate"] == round(hits / len(incomplete), 6)
    assert hits == len(incomplete)


def test_confusion_matrix_totals_match_sample_count():
    result = run_benchmark()
    total = sum(sum(row.values()) for row in result["confusion_matrix"].values())
    assert total == result["sample_count"]
    # Diagonal equals overall correct count:
    diag = sum(result["confusion_matrix"][k][k] for k in result["confusion_matrix"])
    assert diag == sum(1 for c in result["cases"] if c["correct"])


def test_ground_truth_labels_cover_all_required_scenarios():
    categories = {c["category"] for c in CASES}
    assert {"complete_match", "incomplete_evidence", "out_of_range",
            "configuration_exclusion", "conflicting_attributes"} <= categories
    labels = {c["expected_classification"] for c in CASES}
    assert labels == {"CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED", "OUTSIDE_REPORT_VERSION"}


def test_benchmark_api_endpoints(client):
    r = client.get("/api/benchmarks/latest")
    assert r.status_code == 404  # nothing run yet in this fresh DB
    r = client.post("/api/benchmarks/run")
    assert r.status_code == 200
    body = r.json()
    assert body["sample_count"] == len(CASES)
    assert body["metrics"]["false_confirmation_rate"] == 0.0
    r = client.get("/api/benchmarks/latest")
    assert r.status_code == 200
    assert r.json()["id"] == body["id"]

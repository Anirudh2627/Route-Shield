"""G. Benchmark tests: determinism and correct metric computation."""
from __future__ import annotations

from app.benchmark.cases import CASES, CATEGORY_INCOMPLETE
from app.benchmark.runner import run_benchmark


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

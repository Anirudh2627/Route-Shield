"""Benchmark runner - measures the deterministic classification engine.

Metrics are computed programmatically at runtime from synthetic cases with
hand-labelled ground truth. Nothing is hardcoded:

* correct_match_rate          - complete-match cases classified as
                                CAMPAIGN_CRITERIA_MATCH
* correct_abstention_rate     - incomplete-evidence cases classified as
                                VERIFICATION_REQUIRED (correct abstention)
* false_confirmation_rate     - incomplete-evidence cases incorrectly
                                classified as CAMPAIGN_CRITERIA_MATCH,
                                divided by ALL incomplete-evidence cases
* outside_accuracy            - out-of-range/exclusion cases classified as
                                OUTSIDE_REPORT_VERSION
* confusion matrix            - predicted vs expected over the 3 classes
* sample_count                - number of tested examples

The runner executes the SAME pure `classify_vehicle` function used by the
production assessment path, against the reviewed 26V111 criteria.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..services.assessment import ENGINE_VERSION, CampaignCriteria, ProductionRange, classify_vehicle
from .cases import CASES, CATEGORY_INCOMPLETE

LABEL = "SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY"

CLASSES = ["CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED", "OUTSIDE_REPORT_VERSION"]


def benchmark_criteria() -> CampaignCriteria:
    """The reviewed 26V111 criteria, encoded exactly as the human-reviewed
    fixture states them (source: RCLRPT-26V111-4068.pdf)."""
    from datetime import date as _d

    ranges = [
        ProductionRange(2023, 2023, _d(2022, 12, 15), _d(2022, 12, 20), 2, "MY2023 (Vehicle 5)", 2),
        ProductionRange(2024, 2024, _d(2023, 9, 21), _d(2023, 10, 25), 6, "MY2024 (Vehicle 4)", 2),
        ProductionRange(2025, 2025, _d(2024, 2, 5), _d(2024, 10, 2), 1395, "MY2025 (Vehicle 1)", 1),
        ProductionRange(2026, 2026, _d(2024, 10, 2), _d(2025, 10, 7), 2134, "MY2026 (Vehicle 3)", 2),
        ProductionRange(2027, 2027, _d(2025, 10, 9), _d(2026, 2, 2), 856, "MY2027 (Vehicle 2)", 1),
    ]
    return CampaignCriteria(
        make="IC BUS", model="CESB", make_model="IC BUS CESB", product_type="School Bus",
        ranges=ranges,
        overall_start=_d(2022, 12, 15), overall_end=_d(2026, 2, 2),
        feature_code="0004100", feature_description="Hydraulic Brake",
        exclusion_statement="All other similar vehicles, which are not subject to this recall, are excluded.",
        exclusion_page=1, make_model_page=1, feature_page=1,
        document_sha256="a29c04faf2273ba30ecfac16c0e2dfd0550f2aa2d7529e85cdce35844c8125f6",
    )


def run_benchmark() -> dict:
    """Execute all cases and compute metrics. Deterministic: same inputs and
    engine version always produce identical metrics."""
    criteria = benchmark_criteria()
    started = time.perf_counter()

    confusion = {expected: {pred: 0 for pred in CLASSES} for expected in CLASSES}
    case_results: list[dict] = []
    for case in CASES:
        result = classify_vehicle(case, criteria)
        predicted = result.classification
        expected = case["expected_classification"]
        confusion[expected][predicted] += 1
        case_results.append({
            "asset_id": case["asset_id"],
            "category": case["category"],
            "expected": expected,
            "predicted": predicted,
            "correct": expected == predicted,
            "note": case.get("note", ""),
            "reasons": result.reasons[:2],
        })

    duration_ms = int((time.perf_counter() - started) * 1000)

    def rate(expected: str, predicted: str, subset=None) -> tuple[int, int]:
        selected = [
            (c, r) for c, r in zip(CASES, case_results)
            if (subset is None or c["category"] in subset)
            and c["expected_classification"] == expected
        ]
        hits = sum(1 for _, r in selected if r["predicted"] == predicted)
        return hits, len(selected)

    incomplete = CATEGORY_INCOMPLETE
    match_hits, match_total = rate("CAMPAIGN_CRITERIA_MATCH", "CAMPAIGN_CRITERIA_MATCH")
    abstain_hits, abstain_total = rate("VERIFICATION_REQUIRED", "VERIFICATION_REQUIRED", incomplete)
    false_conf_hits, false_conf_total = rate("VERIFICATION_REQUIRED", "CAMPAIGN_CRITERIA_MATCH", incomplete)
    outside_hits, outside_total = rate("OUTSIDE_REPORT_VERSION", "OUTSIDE_REPORT_VERSION")

    def safe_div(a: int, b: int) -> float | None:
        return round(a / b, 6) if b else None

    metrics = {
        "correct_match_rate": safe_div(match_hits, match_total),
        "correct_match_count": f"{match_hits}/{match_total}",
        "correct_abstention_rate": safe_div(abstain_hits, abstain_total),
        "correct_abstention_count": f"{abstain_hits}/{abstain_total}",
        "false_confirmation_rate": safe_div(false_conf_hits, false_conf_total),
        "false_confirmation_count": f"{false_conf_hits}/{false_conf_total}",
        "outside_accuracy": safe_div(outside_hits, outside_total),
        "outside_count": f"{outside_hits}/{outside_total}",
        "overall_accuracy": safe_div(
            sum(1 for c in case_results if c["correct"]), len(case_results)
        ),
        "overall_count": f"{sum(1 for c in case_results if c['correct'])}/{len(case_results)}",
    }
    return {
        "run_at": datetime.now(timezone.utc),
        "engine_version": ENGINE_VERSION,
        "campaign_number": "26V111",
        "sample_count": len(CASES),
        "duration_ms": duration_ms,
        "metrics": metrics,
        "confusion_matrix": confusion,
        "cases": case_results,
        "label": LABEL,
    }

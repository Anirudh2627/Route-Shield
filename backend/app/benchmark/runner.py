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
from datetime import date, datetime, timezone

from ..services.assessment import ENGINE_VERSION, CampaignCriteria, ProductionRange, classify_vehicle
from .cases import CASES, CATEGORY_INCOMPLETE

LABEL = "SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY"

# The benchmark is pinned to the manually reviewed campaign fixture for 26V111
# and its document hash; see benchmark_criteria().
BENCHMARK_CAMPAIGN = "26V111"
# Ground-truth labels in cases.py were authored against THIS exact reviewed
# document (RCLRPT-26V111-4068.pdf). If the fixture hash ever changes, the
# benchmark refuses to run until the cases are consciously re-reviewed and the
# pin updated - silent divergence is impossible.
PINNED_FIXTURE_SHA256 = "a29c04faf2273ba30ecfac16c0e2dfd0550f2aa2d7529e85cdce35844c8125f6"

CLASSES = ["CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED", "OUTSIDE_REPORT_VERSION"]


def _reviewed_fixture() -> dict:
    """Load the manually reviewed campaign fixture that backs the benchmark.

    The benchmark is pinned to this fixture on purpose: criteria must never be
    re-typed here, or the measured numbers would silently drift away from the
    reviewed report. Any divergence fails loudly instead.
    """
    from ..services.evidence import load_reviewed_fixture

    fixture = load_reviewed_fixture(BENCHMARK_CAMPAIGN)
    if fixture is None:
        raise RuntimeError(
            f"Reviewed campaign fixture for {BENCHMARK_CAMPAIGN} is missing. The benchmark "
            "cannot run without the human-reviewed criteria it is pinned to."
        )
    return fixture


def benchmark_criteria() -> CampaignCriteria:
    """Build the matching criteria directly from the reviewed 26V111 fixture.

    Source: backend/data/campaigns/26V111.reviewed.json (RCLRPT-26V111-4068.pdf).
    The fixture's document SHA-256 is carried onto the criteria, and required
    criterion types are validated so a partially edited fixture cannot produce
    a benchmark that looks successful while matching on less evidence.
    """
    fixture = _reviewed_fixture()
    rows = fixture.get("criteria", [])
    doc = fixture.get("source_document", {})
    sha = str(doc.get("sha256", ""))
    if not sha:
        raise RuntimeError("Reviewed fixture is missing source_document.sha256; refusing to benchmark.")
    if sha != PINNED_FIXTURE_SHA256:
        raise RuntimeError(
            f"Reviewed fixture document hash ({sha}) differs from the pinned benchmark hash "
            f"({PINNED_FIXTURE_SHA256}). Ground-truth cases were labelled against the pinned "
            "document; re-review the cases and update the pin deliberately. Refusing to "
            "benchmark on silently diverged criteria."
        )

    make = model = make_model = product_type = ""
    make_model_page = feature_page = exclusion_page = None
    feature_code = feature_description = exclusion_statement = ""
    overall_start = overall_end = None
    ranges: list[ProductionRange] = []
    # Descriptive (non-matching) criterion types are present in the fixture but
    # never influence classification; they are verified as REVIEWED, not parsed.
    DESCRIPTIVE_TYPES = {
        "MANUFACTURER", "POPULATION", "DEFECT_DESCRIPTION", "SAFETY_CONSEQUENCE",
        "REMEDY", "REMEDY_TYPE", "COMPONENT",
    }

    for row in rows:
        if row.get("review_status") != "REVIEWED":
            raise RuntimeError(
                f"Benchmark criterion '{row.get('label')}' is not REVIEWED in the fixture; "
                "unreviewed criteria must never back the measured benchmark."
            )
        ctype, value, page = row.get("criterion_type"), row.get("value") or {}, row.get("source_page")
        if ctype in DESCRIPTIVE_TYPES:
            continue
        if ctype == "MAKE_MODEL":
            make = str(value.get("make", ""))
            model = str(value.get("model", ""))
            make_model = str(value.get("make_model", ""))
            make_model_page = page
        elif ctype == "PRODUCT_TYPE":
            product_type = str(value.get("product_type", ""))
        elif ctype == "PRODUCTION_DATE_RANGE":
            ranges.append(ProductionRange(
                model_year_start=int(value["model_year_start"]),
                model_year_end=int(value["model_year_end"]),
                production_start=date.fromisoformat(value["production_start"]),
                production_end=date.fromisoformat(value["production_end"]),
                units=value.get("units"),
                label=row.get("label", ""),
                source_page=page,
            ))
        elif ctype == "OVERALL_BUILD_RANGE":
            overall_start = date.fromisoformat(value["start"])
            overall_end = date.fromisoformat(value["end"])
        elif ctype == "FEATURE_CODE":
            feature_code = str(value.get("feature_code", ""))
            feature_description = str(value.get("feature_description", ""))
            feature_page = page
        elif ctype == "EXCLUSION":
            exclusion_statement = str(value.get("exclusion_statement", ""))
            exclusion_page = page

    missing = [
        name for name, ok in [
            ("make/model", bool(make_model or (make and model))),
            ("production date ranges", bool(ranges)),
            ("feature code", bool(feature_code)),
            ("exclusion statement", bool(exclusion_statement)),
            ("overall build range", bool(overall_start and overall_end)),
        ] if not ok
    ]
    if missing:
        raise RuntimeError(
            "Reviewed fixture is missing benchmark-required criteria: " + ", ".join(missing) +
            ". Fix the fixture (with review) rather than weakening the benchmark."
        )
    for r in ranges:
        if r.production_start > r.production_end:
            raise RuntimeError(f"Fixture range '{r.label}' has an inverted production window.")
        if not (overall_start <= r.production_start and r.production_end <= overall_end):
            raise RuntimeError(
                f"Fixture range '{r.label}' falls outside the documented overall suspect build "
                "window; the fixture no longer agrees with the reviewed report."
            )
    if len({(r.model_year_start, r.model_year_end) for r in ranges}) != len(ranges):
        raise RuntimeError("Fixture contains duplicate model-year ranges.")

    ranges.sort(key=lambda r: (r.model_year_start, r.production_start))
    return CampaignCriteria(
        make=make, model=model, make_model=make_model, product_type=product_type,
        ranges=ranges,
        overall_start=overall_start, overall_end=overall_end,
        feature_code=feature_code, feature_description=feature_description,
        exclusion_statement=exclusion_statement,
        exclusion_page=exclusion_page, make_model_page=make_model_page, feature_page=feature_page,
        document_sha256=sha,
    )


def benchmark_provenance() -> dict:
    """Provenance of the criteria the benchmark was measured against."""
    fixture = _reviewed_fixture()
    doc = fixture.get("source_document", {})
    criteria = benchmark_criteria()
    return {
        "campaign_number": BENCHMARK_CAMPAIGN,
        "fixture_version": fixture.get("fixture_version"),
        "reviewed_by": fixture.get("reviewed_by"),
        "reviewed_at": fixture.get("reviewed_at"),
        "source_document_url": doc.get("url"),
        "document_sha256": doc.get("sha256"),
        "page_count": doc.get("page_count"),
        "criteria_document_sha256": criteria.document_sha256,
        "production_ranges": len(criteria.ranges),
        "feature_code": criteria.feature_code,
        "hash_matches_criteria": doc.get("sha256") == criteria.document_sha256,
    }


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
        "campaign_number": BENCHMARK_CAMPAIGN,
        "sample_count": len(CASES),
        "duration_ms": duration_ms,
        "metrics": metrics,
        "confusion_matrix": confusion,
        "cases": case_results,
        "label": LABEL,
    }

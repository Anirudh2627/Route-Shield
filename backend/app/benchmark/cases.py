"""Synthetic ground-truth test cases for the deterministic classifier.

Labels were assigned by a human reading the reviewed 26V111 criteria
(IC BUS CESB, MY2023-2027 with per-model-year production windows, Hydraulic
Brake feature code 0004100, explicit exclusion of all other similar
vehicles) - NOT by running the engine.

Case categories:
* complete_match        - every required criterion evidenced and satisfied
* incomplete_evidence   - critical evidence missing/ambiguous => the correct
                          behaviour is abstention (VERIFICATION_REQUIRED)
* out_of_range          - positive evidence the vehicle is outside THIS
                          report version
* configuration_exclusion - recorded brake code contradicts the required one
* conflicting_attributes  - internally inconsistent records

"SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY"
"""
from __future__ import annotations

MATCH = "CAMPAIGN_CRITERIA_MATCH"
VERIF = "VERIFICATION_REQUIRED"
OUT = "OUTSIDE_REPORT_VERSION"


def _v(asset_id, make, model, my, build, code, category, expected, config=None, note=""):
    return {
        "asset_id": asset_id,
        "make": make,
        "model": model,
        "model_year": my,
        "build_date": build,
        "brake_feature_code": code,
        "configuration": config or {},
        "category": category,
        "expected_classification": expected,
        "note": note,
    }


IC = ("IC BUS", "CESB")

CASES: list[dict] = [
    # ---------------- Complete criteria matches (10) -------------------------
    _v("BM-001", *IC, 2023, "2022-12-15", "0004100", "complete_match", MATCH,
       note="MY2023 window start boundary (Dec 15, 2022)"),
    _v("BM-002", *IC, 2023, "2022-12-20", "0004100", "complete_match", MATCH,
       note="MY2023 window end boundary (Dec 20, 2022)"),
    _v("BM-003", *IC, 2024, "2023-09-21", "0004100", "complete_match", MATCH,
       note="MY2024 window start boundary"),
    _v("BM-004", *IC, 2024, "2023-10-25", "0004100", "complete_match", MATCH,
       note="MY2024 window end boundary"),
    _v("BM-005", *IC, 2025, "2024-02-05", "0004100", "complete_match", MATCH,
       note="MY2025 window start boundary"),
    _v("BM-006", *IC, 2025, "2024-06-15", "0004100", "complete_match", MATCH,
       note="MY2025 mid-window"),
    _v("BM-007", *IC, 2026, "2024-10-02", "0004100", "complete_match", MATCH,
       note="MY2026 window start boundary"),
    _v("BM-008", *IC, 2026, "2025-05-01", "0004100", "complete_match", MATCH,
       note="MY2026 mid-window"),
    _v("BM-009", *IC, 2027, "2025-10-09", "0004100", "complete_match", MATCH,
       note="MY2027 window start boundary"),
    _v("BM-010", *IC, 2027, "2026-02-02", "0004100", "complete_match", MATCH,
       note="MY2027 window end boundary (Feb 02, 2026)"),
    # ---------------- Incomplete evidence => abstention (12) ------------------
    _v("BM-011", *IC, 2025, None, "0004100", "incomplete_evidence", VERIF,
       note="Build date missing"),
    _v("BM-012", *IC, 2026, "2025-01-15", None, "incomplete_evidence", VERIF,
       note="Brake feature code missing"),
    _v("BM-013", *IC, 2025, None, None, "incomplete_evidence", VERIF,
       note="Both build date and feature code missing"),
    _v("BM-014", *IC, None, "2025-03-03", "0004100", "incomplete_evidence", VERIF,
       note="Model year missing"),
    _v("BM-015", "", "", 2025, "2024-05-05", "0004100", "incomplete_evidence", VERIF,
       note="Make and model missing (build date inside the MY2025 window)"),
    _v("BM-016", *IC, 2025, "2024-05-05", None, "incomplete_evidence", VERIF,
       config={"note": "Hydraulic Brake per work order #4412"},
       note="Narrative mentions hydraulic but code not machine-recorded"),
    _v("BM-017", *IC, 2026, "2025-02-02", None, "incomplete_evidence", VERIF,
       config={"brake_system": "not recorded"}, note="Configuration explicitly 'not recorded'"),
    _v("BM-018", *IC, 2027, "2025-12-01", "", "incomplete_evidence", VERIF,
       note="Empty-string feature code"),
    _v("BM-019", *IC, 2024, None, None, "incomplete_evidence", VERIF,
       note="No build date, no code, no config"),
    _v("BM-020", *IC, 2023, "2022-12-18", "0004100", "conflicting_attributes", VERIF,
       config={"brake_system": "Air Brake (retrofit note)"},
       note="Code matches but configuration narrative contradicts it"),
    _v("BM-021", *IC, 2026, "2025-06-06", None, "incomplete_evidence", VERIF,
       config={"brake_system": "hydraulic", "feature_code": "illegible on scan"},
       note="Config names hydraulic system but authoritative code is illegible"),
    _v("BM-022", *IC, 2025, "2024-04-04", None, "incomplete_evidence", VERIF,
       note="Code missing while every other criterion matches"),
    # ---------------- Outside production ranges (10) --------------------------
    _v("BM-023", *IC, 2023, "2022-12-14", "0004100", "out_of_range", OUT,
       note="One day before MY2023 window start"),
    _v("BM-024", *IC, 2023, "2022-12-21", "0004100", "out_of_range", OUT,
       note="One day after MY2023 window end"),
    _v("BM-025", *IC, 2024, "2023-09-20", "0004100", "out_of_range", OUT,
       note="One day before MY2024 window start"),
    _v("BM-026", *IC, 2025, "2024-10-03", "0004100", "out_of_range", OUT,
       note="One day after MY2025 window end (and outside MY2025 list)"),
    _v("BM-027", *IC, 2024, "2024-03-02", "0004100", "out_of_range", OUT,
       note="Inside overall suspect window but outside the MY2024 documented window"),
    _v("BM-028", *IC, 2026, "2025-10-08", "0004100", "out_of_range", OUT,
       note="Gap day between MY2026 end and MY2027 start"),
    _v("BM-029", *IC, 2027, "2026-02-03", "0004100", "out_of_range", OUT,
       note="One day after the final documented build date"),
    _v("BM-030", *IC, 2022, "2021-07-15", "0004100", "out_of_range", OUT,
       note="Model year not among documented model years"),
    _v("BM-031", *IC, 2028, "2026-06-01", "0004100", "out_of_range", OUT,
       note="Future model year outside the report"),
    _v("BM-032", *IC, 2025, "2023-01-15", "0004100", "out_of_range", OUT,
       note="Build date precedes the documented period for MY2025"),
    # ---------------- Configuration exclusions (4) ----------------------------
    _v("BM-033", *IC, 2026, "2025-01-10", "0004200", "configuration_exclusion", OUT,
       config={"brake_system": "Air Brake"}, note="Air brake code inside production window"),
    _v("BM-034", *IC, 2025, "2024-07-07", "0004999", "configuration_exclusion", OUT,
       note="Different feature code inside production window"),
    _v("BM-035", *IC, 2023, "2022-12-16", "0004300", "configuration_exclusion", OUT,
       note="Non-hydraulic code at start of MY2023 window"),
    _v("BM-036", *IC, 2027, "2026-01-05", "0004101", "configuration_exclusion", OUT,
       note="Near-miss feature code is still an exclusion (exact code required)"),
    # ---------------- Wrong make/model (4) ------------------------------------
    _v("BM-037", "BLUE BIRD", "VISION", 2025, "2024-08-01", None, "out_of_range", OUT,
       config={"brake_system": "Air"}, note="Different manufacturer"),
    _v("BM-038", "IC BUS", "CE", 2025, "2024-08-01", "0004100", "out_of_range", OUT,
       note="Same make, different model"),
    _v("BM-039", "THOMAS BUILT", "SAF-T-LINER", 2024, "2023-10-01", "0004100", "out_of_range", OUT,
       note="Different manufacturer inside production window"),
    _v("BM-040", "IC BUS", "CESB", 2026, "2025-04-04", "0004100", "complete_match", MATCH,
       config={"brake_system": "Hydraulic"}, note="Full evidence, mid-window"),
]

CATEGORY_INCOMPLETE = {"incomplete_evidence", "conflicting_attributes"}

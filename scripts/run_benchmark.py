#!/usr/bin/env python3
"""Reproducible benchmark of the deterministic classification engine.

Usage:
    python scripts/run_benchmark.py            # run + print metrics
    python scripts/run_benchmark.py --json     # machine-readable output
    python scripts/run_benchmark.py --verify-determinism  # run twice, compare

Metrics are computed programmatically at runtime from synthetic cases with
hand-labelled ground truth. Nothing is hardcoded.

SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.benchmark.runner import benchmark_provenance, run_benchmark  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument("--verify-determinism", action="store_true",
                        help="run twice and verify identical metrics")
    parser.add_argument("--save", type=str, default="",
                        help="write the benchmark JSON result to this path")
    args = parser.parse_args()

    prov = benchmark_provenance()
    result = run_benchmark()
    result["provenance"] = prov

    if args.verify_determinism:
        second = run_benchmark()
        same = (
            result["metrics"] == second["metrics"]
            and result["confusion_matrix"] == second["confusion_matrix"]
            and [c["predicted"] for c in result["cases"]] == [c["predicted"] for c in second["cases"]]
        )
        if not same:
            print("DETERMINISM CHECK: FAILED - two runs produced different results")
            return 1
        print("DETERMINISM CHECK: PASSED - two consecutive runs produced identical metrics")

    if args.save:
        payload = {**result, "run_at": result["run_at"].isoformat()}
        Path(args.save).write_text(json.dumps(payload, indent=2, default=str))
        print(f"Saved benchmark result to {args.save}")

    if args.json:
        print(json.dumps({**result, "run_at": result["run_at"].isoformat()}, indent=2, default=str))
        return 0

    m = result["metrics"]
    print("=" * 72)
    print(result["label"])
    print("=" * 72)
    print(f"Engine version        : {result['engine_version']}")
    print(f"Campaign              : {result['campaign_number']} (reviewed criteria)")
    print(f"Criteria source       : {prov.get('source_document_url')}")
    print(f"Document SHA-256      : {prov.get('document_sha256')}")
    print(f"Criteria hash match   : {prov.get('hash_matches_criteria')} "
          f"| ranges {prov.get('production_ranges')} | feature code {prov.get('feature_code')}")
    print(f"Sample count          : {result['sample_count']}")
    print(f"Duration              : {result['duration_ms']} ms")
    print(f"Run at                : {result['run_at'].isoformat()}")
    print("-" * 72)
    print(f"Correct campaign-match rate     : {fmt(m['correct_match_rate'])}  ({m['correct_match_count']})")
    print(f"Correct abstention rate         : {fmt(m['correct_abstention_rate'])}  ({m['correct_abstention_count']})")
    print(f"FALSE-CONFIRMATION RATE         : {fmt(m['false_confirmation_rate'])}  ({m['false_confirmation_count']})")
    print(f"Outside-report accuracy         : {fmt(m['outside_accuracy'])}  ({m['outside_count']})")
    print(f"Overall accuracy                : {fmt(m['overall_accuracy'])}  ({m['overall_count']})")
    print("-" * 72)
    print("Confusion matrix (rows=expected, cols=predicted):")
    classes = ["CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED", "OUTSIDE_REPORT_VERSION"]
    header = f"{'':28}" + "".join(f"{c[:12]:>14}" for c in classes)
    print(header)
    for exp in classes:
        row = f"{exp[:27]:28}" + "".join(f"{result['confusion_matrix'][exp][p]:>14}" for p in classes)
        print(row)
    wrong = [c for c in result["cases"] if not c["correct"]]
    if wrong:
        print("-" * 72)
        print(f"Misclassified cases ({len(wrong)}):")
        for c in wrong:
            print(f"  {c['asset_id']}: expected {c['expected']}, got {c['predicted']} ({c['note']})")
    else:
        print("-" * 72)
        print("All cases classified as expected.")
    return 0


def fmt(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())

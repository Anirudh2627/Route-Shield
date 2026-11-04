"""Benchmark endpoints.

Runs the reproducible synthetic benchmark of the deterministic classification
engine. Metrics are computed at runtime - never hardcoded.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..benchmark.runner import run_benchmark
from ..db import get_db
from ..schemas import BenchmarkRunOut

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get(
    "/latest",
    response_model=BenchmarkRunOut,
    summary="Latest stored benchmark run (404 if never run)",
)
def latest(db: Session = Depends(get_db)) -> models.BenchmarkRun:
    row = db.scalar(select(models.BenchmarkRun).order_by(models.BenchmarkRun.id.desc()).limit(1))
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(404, "No benchmark run recorded yet. POST /api/benchmarks/run to execute one.")
    return row


@router.post("/run", response_model=BenchmarkRunOut, summary="Execute the synthetic benchmark now")
def run(db: Session = Depends(get_db)) -> models.BenchmarkRun:
    result = run_benchmark()
    row = models.BenchmarkRun(
        run_at=result["run_at"],
        engine_version=result["engine_version"],
        campaign_number=result["campaign_number"],
        sample_count=result["sample_count"],
        duration_ms=result["duration_ms"],
        metrics=result["metrics"],
        confusion_matrix=result["confusion_matrix"],
        cases=result["cases"],
        label=result["label"],
    )
    db.add(row)
    db.commit()
    return row

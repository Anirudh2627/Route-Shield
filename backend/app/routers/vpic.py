"""vPIC VIN decoding endpoints - strictly single, human-triggered, audited.

NHTSA warns its APIs are not intended for bulk third-party VIN lookups:
RouteShield therefore exposes ONE-VIN-AT-A-TIME endpoints only, requires an
authenticated manager, caches results, throttles outbound requests, and
refuses to submit synthetic demonstration identifiers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..auth import get_current_manager
from ..db import get_db
from ..schemas import DecodeVinRequest
from ..services import vpic_service
from ..services.vpic_service import VINPolicyError, VPICError

router = APIRouter(prefix="/vpic", tags=["vpic"])

_LIMITATION_NOTE = (
    "vPIC decodes the VIN string only. Fields it does not return remain UNKNOWN and are never "
    "inferred (including brake configuration / feature codes). A decode result is NEVER "
    "automatically converted into recall confirmation."
)


@router.post("/decode-single-vin", summary="Decode ONE real VIN via vPIC (manager-triggered, cached)")
def decode_single_vin(
    payload: DecodeVinRequest,
    db: Session = Depends(get_db),
    manager: models.ManagerUser = Depends(get_current_manager),
) -> dict:
    try:
        result = vpic_service.decode_single_vin(db, payload.vin, payload.model_year, manager.username)
    except VINPolicyError as exc:
        raise HTTPException(422, str(exc))
    except VPICError as exc:
        raise HTTPException(502, str(exc))
    result["limitations"] = _LIMITATION_NOTE
    return result


@router.post("/recall-check-single-vin",
             summary="Individual VIN recall check via official NHTSA API (manager-triggered, single VIN)")
def recall_check_single_vin(
    payload: DecodeVinRequest,
    db: Session = Depends(get_db),
    manager: models.ManagerUser = Depends(get_current_manager),
) -> dict:
    """The only path that can produce a real VIN-level recall result - and
    only when an actual individually retrieved source result exists."""
    try:
        result = vpic_service.recall_check_single_vin(db, payload.vin, manager.username)
    except VINPolicyError as exc:
        raise HTTPException(422, str(exc))
    except VPICError as exc:
        raise HTTPException(502, str(exc))
    result["note"] = (
        "Single human-triggered check. This is never automated or bulk-executed. "
        "Results apply only to this VIN at the retrieval timestamp."
    )
    return result

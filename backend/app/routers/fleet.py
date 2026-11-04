"""Fleet register read endpoints (vehicles and routes).

All seeded records are synthetic and flagged. Operational state shown here is
persisted server-side; it only changes through the approval flow.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import RouteOut, VehicleOut

router = APIRouter(prefix="/fleet", tags=["fleet"])


@router.get("/vehicles", response_model=list[VehicleOut], summary="Fleet register (synthetic demo data)")
def list_vehicles(db: Session = Depends(get_db)) -> list[models.FleetVehicle]:
    return list(db.scalars(select(models.FleetVehicle).order_by(models.FleetVehicle.asset_id)))


@router.get("/routes", response_model=list[RouteOut], summary="Route register (synthetic demo data)")
def list_routes(db: Session = Depends(get_db)) -> list[models.FleetRoute]:
    return list(db.scalars(select(models.FleetRoute).order_by(models.FleetRoute.route_id)))

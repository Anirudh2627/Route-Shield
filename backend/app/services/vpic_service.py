"""NHTSA vPIC VIN decoding - strictly single, human-triggered requests.

Policy enforced here:
* ONE VIN at a time; there is no bulk endpoint anywhere in RouteShield.
* Human-triggered only: every decode requires an authenticated manager and is
  audited. NHTSA explicitly warns its APIs are not for bulk third-party VIN
  lookups; automated bulk VIN recall requests are NOT implemented.
* Synthetic demonstration vehicles are NEVER submitted to vPIC (guarded by
  asset-id prefix, DB synthetic flag, and stored synthetic VIN patterns).
* Results are cached (VINDecodeCache). Missing decoded fields stay UNKNOWN -
  we never infer a brake configuration or feature code that vPIC did not
  return, and a decode result is NEVER automatically converted into recall
  confirmation.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import audit

logger = logging.getLogger("routeshield.vpic")

SYNTHETIC_MARKERS = ("SYNTHETIC", "DEMO-", "SAMPLE")

# vPIC DecodeVinValuesExtended returns ~140 fields; these are the ones we
# surface prominently. Everything else is stored in decoded_values as-is.
KEY_FIELDS = [
    "Make", "Manufacturer", "Model", "ModelYear", "VehicleType", "BodyClass",
    "DriveType", "GVWR", "EngineModel", "EngineConfiguration", "BrakeSystemType",
    "PlantCity", "PlantState", "PlantCountry", "Series", "Trim", "VIN",
]

_rate_lock = threading.Lock()
_last_request_ts: float = 0.0


class VINPolicyError(Exception):
    """Raised when a VIN request violates RouteShield policy."""


class VPICError(Exception):
    """Raised when the vPIC API call fails."""


def is_synthetic_identifier(value: str | None) -> bool:
    if not value:
        return False
    upper = value.upper()
    return any(marker in upper for marker in SYNTHETIC_MARKERS)


def _guard_synthetic(db: Session, vin: str) -> None:
    if is_synthetic_identifier(vin):
        raise VINPolicyError(
            "This VIN is flagged as a synthetic demonstration identifier. Synthetic "
            "vehicles are NEVER submitted to NHTSA or vPIC."
        )
    row = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.vin == vin))
    if row is not None and row.synthetic:
        raise VINPolicyError(
            f"VIN belongs to synthetic fleet record {row.asset_id}; it must never be "
            "submitted to vPIC."
        )


def _throttle() -> None:
    """Respect rate limits: enforce a minimum interval between vPIC calls."""
    global _last_request_ts
    settings = get_settings()
    with _rate_lock:
        wait = settings.vpic_min_request_interval_seconds - (time.monotonic() - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


def decode_single_vin(db: Session, vin: str, model_year: str | None, actor: str) -> dict:
    """Decode ONE VIN via vPIC (cache-first). Human-triggered, audited."""
    settings = get_settings()
    vin = vin.strip().upper()
    _guard_synthetic(db, vin)

    cached = db.scalar(select(models.VINDecodeCache).where(models.VINDecodeCache.vin == vin))
    if cached is not None:
        audit.record_event(
            db, actor=actor, action="VIN_DECODE_CACHE_HIT", entity_type="VIN", entity_id=vin,
            result="CACHED", detail="Returned cached vPIC decode; no outbound request made.",
            flush_only=False,
        )
        return {
            "vin": cached.vin,
            "requested_model_year": cached.requested_model_year,
            "decoded_values": cached.decoded_values,
            "unavailable_fields": cached.unavailable_fields,
            "decoded_at": cached.decoded_at,
            "source": cached.source,
            "requested_by": cached.requested_by,
            "cached": True,
        }

    _throttle()
    url = (
        f"{settings.vpic_api_base}/api/vehicles/DecodeVinValuesExtended/{vin}"
        f"?format=json" + (f"&modelyear={model_year}" if model_year else "")
    )
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise VPICError(f"vPIC request failed: {exc}") from exc
    except ValueError as exc:
        raise VPICError(f"vPIC returned invalid JSON: {exc}") from exc

    results = payload.get("Results") or []
    values = results[0] if results else {}
    if not values or values.get("ErrorCode", "0") not in ("0", "", "0 - VIN decoded clean"):
        error_code = values.get("ErrorCode", "?")
        error_text = values.get("ErrorText", "")
        # Error codes 1-11 are warnings with usable data; >=100 are failures.
        try:
            primary_code = int(str(error_code).split(",")[0].split(" - ")[0] or "0")
        except ValueError:
            primary_code = 0
        if primary_code >= 100:
            raise VPICError(f"vPIC could not decode VIN (error {error_code}: {error_text})")

    decoded = {k: v for k, v in values.items() if isinstance(v, (str, int, float)) and str(v).strip() != ""}
    unavailable = sorted(k for k, v in values.items() if v is None or str(v).strip() == "")
    brake_field = decoded.get("BrakeSystemType")  # may be absent -> stays UNKNOWN

    row = models.VINDecodeCache(
        vin=vin,
        requested_model_year=model_year or "",
        decoded_values=decoded,
        unavailable_fields=unavailable,
        source="vpic.nhtsa.dot.gov DecodeVinValuesExtended",
        requested_by=actor,
    )
    db.add(row)
    audit.record_event(
        db, actor=actor, action="VIN_DECODED_VPIC", entity_type="VIN", entity_id=vin,
        new_state={"fields_decoded": len(decoded), "fields_unavailable": len(unavailable),
                   "brake_system_type": brake_field or "NOT_RETURNED"},
        result="DECODED",
        detail=(
            "Single human-triggered vPIC decode. A decode is NOT recall confirmation. "
            + ("" if brake_field else "vPIC did not return a brake system type; brake configuration remains UNKNOWN and was not inferred.")
        ),
        flush_only=False,
    )
    return {
        "vin": row.vin,
        "requested_model_year": row.requested_model_year,
        "decoded_values": decoded,
        "unavailable_fields": unavailable,
        "decoded_at": row.decoded_at,
        "source": row.source,
        "requested_by": actor,
        "cached": False,
    }


def recall_check_single_vin(db: Session, vin: str, actor: str) -> dict:
    """Single human-triggered individual VIN recall check against the official
    NHTSA recalls API. This is the ONLY path that can produce a real
    VIN-confirmed recall status - and only when an actual source result is
    retrieved. Never automated, never bulk."""
    settings = get_settings()
    vin = vin.strip().upper()
    _guard_synthetic(db, vin)
    _throttle()
    url = f"{settings.nhtsa_api_base}/recalls/vin"
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
            resp = client.get(url, params={"vin": vin})
            if resp.status_code in (401, 403):
                # NHTSA protects VIN-level lookups from third-party automated
                # access. RouteShield reports this honestly instead of ever
                # fabricating a VIN-confirmed state.
                raise VPICError(
                    "NHTSA declined the individual VIN recall lookup (HTTP "
                    f"{resp.status_code}). VIN-specific recall verification is therefore "
                    "UNAVAILABLE for this VIN; no VIN-confirmed status will be recorded. "
                    "NHTSA's VIN search is not offered for third-party automated access - "
                    "verify manually at https://www.nhtsa.gov/recalls if required."
                )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise VPICError(f"NHTSA VIN recall check failed: {exc}") from exc
    except ValueError as exc:
        raise VPICError(f"NHTSA VIN recall check returned invalid JSON: {exc}") from exc

    results = payload.get("results")
    if results is None:
        raise VPICError("NHTSA VIN recall check returned no result set; status remains unavailable.")
    audit.record_event(
        db, actor=actor, action="VIN_RECALL_CHECK", entity_type="VIN", entity_id=vin,
        new_state={"recalls_returned": len(results)},
        result="RETRIEVED",
        detail="Single human-triggered individual VIN recall check against api.nhtsa.gov/recalls/vin.",
        flush_only=False,
    )
    return {"vin": vin, "results": results, "retrieved_at": datetime.now(timezone.utc).isoformat()}

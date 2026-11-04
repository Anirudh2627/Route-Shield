"""NHTSA integration client.

Two official sources are used:
* Recalls campaign API: https://api.nhtsa.gov/recalls/campaignNumber
* Static Part 573 report PDFs: https://static.nhtsa.gov/odi/rcl/...

All network access is defensive: timeouts, explicit errors, and a local cache
of downloaded report PDFs keyed by SHA-256. This module only READS external
data; it never writes operational records.

NOTE: NHTSA warns its VIN-level APIs are not for bulk third-party lookups.
RouteShield only ever performs single, human-triggered VIN decodes
(see services/vpic_service.py) and never bulk VIN recall requests.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import get_settings

logger = logging.getLogger("routeshield.nhtsa")


class NHTSAError(Exception):
    """Raised when live NHTSA retrieval fails."""


@dataclass
class CampaignApiResponse:
    campaign_number: str
    raw: dict
    results: list[dict] = field(default_factory=list)

    @property
    def primary(self) -> dict:
        return self.results[0] if self.results else {}

    @property
    def model_years(self) -> list[int]:
        years = set()
        for r in self.results:
            try:
                years.add(int(r.get("ModelYear")))
            except (TypeError, ValueError):
                continue
        return sorted(years)


@dataclass
class ReportDocument:
    url: str
    content: bytes
    sha256: str
    page_count: int | None = None
    from_cache: bool = False


def _client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True)


def fetch_campaign(campaign_number: str) -> CampaignApiResponse:
    """Retrieve campaign information through the official NHTSA API."""
    settings = get_settings()
    url = f"{settings.nhtsa_api_base}/recalls/campaignNumber"
    try:
        with _client() as client:
            resp = client.get(url, params={"campaignNumber": campaign_number})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise NHTSAError(f"NHTSA campaign API request failed: {exc}") from exc
    except ValueError as exc:
        raise NHTSAError(f"NHTSA campaign API returned invalid JSON: {exc}") from exc

    results = data.get("results") or []
    if not results:
        raise NHTSAError(
            f"NHTSA campaign API returned no results for campaign {campaign_number}"
        )
    return CampaignApiResponse(campaign_number=campaign_number, raw=data, results=results)


def report_url_for_campaign(campaign_number: str, report_received_year: int | None = None) -> str:
    """Best-effort canonical URL of the Part 573 report for a campaign.

    NHTSA publishes reports as RCLRPT-<campaign>-<seq>.pdf under the year the
    report was received. The exact sequence number is discoverable from the
    recalls/campaigns lookup UI; for the demo campaign we use the documented
    official URL. For unknown campaigns callers should pass an explicit URL.
    """
    settings = get_settings()
    cn = campaign_number.strip().upper()
    year = report_received_year
    return f"{settings.nhtsa_static_base}/odi/rcl/{year}/RCLRPT-{cn}-4068.pdf"


def download_report(url: str, cache_dir: Path | None = None) -> ReportDocument:
    """Download the official report PDF, caching by content hash.

    If the download fails but a cached copy of the same URL exists, the cached
    copy is returned with from_cache=True (explicitly surfaced in the UI).
    """
    settings = get_settings()
    cache_dir = cache_dir or settings.report_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha256(url.encode()).hexdigest()[:16]
    meta_path = cache_dir / f"{slug}.meta"
    pdf_path = cache_path = cache_dir / f"{slug}.pdf"

    try:
        with _client() as client:
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.content
        if not content.startswith(b"%PDF"):
            raise NHTSAError(f"Downloaded document from {url} is not a PDF")
        pdf_path.write_bytes(content)
        meta_path.write_text(url)
        return ReportDocument(url=url, content=content, sha256=hashlib.sha256(content).hexdigest())
    except (httpx.HTTPError, NHTSAError, OSError) as exc:
        if cache_path.exists():
            content = cache_path.read_bytes()
            logger.warning("Live PDF download failed (%s); using cached copy for %s", exc, url)
            return ReportDocument(
                url=url,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                from_cache=True,
            )
        raise NHTSAError(f"Report download failed and no cache available: {exc}") from exc


def ping() -> bool:
    """Lightweight reachability probe for the health endpoint."""
    settings = get_settings()
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{settings.nhtsa_api_base}/recalls/campaignNumber",
                              params={"campaignNumber": "26V111"})
            return resp.status_code == 200
    except httpx.HTTPError:
        return False

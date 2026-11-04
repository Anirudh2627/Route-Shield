"""Deterministic PDF text extraction for NHTSA Part 573 recall reports.

This module is the rule-based backbone of evidence extraction:

1. Extract text page-by-page with PyMuPDF (page boundaries preserved).
2. Parse the fixed Part 573 layout with regular expressions.
3. Emit structured criteria, each carrying: value, source page, verbatim
   snippet, extraction method, confidence, review status.
4. Validate the extraction; incomplete extractions are reported as such so
   the evidence layer can fall back to the manually reviewed fixture.

No LLM is involved here. Extraction never decides vehicle applicability -
that is done by services/assessment.py against REVIEWED criteria only.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

logger = logging.getLogger("routeshield.pdf")

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
}


class PDFExtractionError(Exception):
    """Raised when the PDF cannot be read at all."""


@dataclass
class ExtractedField:
    """One extracted value with full provenance."""

    value: object
    source_page: int | None
    source_snippet: str
    extraction_method: str = "RULE_BASED_PDF"
    confidence: float = 0.95
    review_status: str = "PENDING_REVIEW"


@dataclass
class Part573Extraction:
    manufacturer: ExtractedField | None = None
    submission_date: ExtractedField | None = None
    manufacturer_recall_number: ExtractedField | None = None
    total_units: ExtractedField | None = None
    vehicle_groups: list[ExtractedField] = field(default_factory=list)
    overall_build_range: ExtractedField | None = None
    feature_codes: list[ExtractedField] = field(default_factory=list)
    exclusions: list[ExtractedField] = field(default_factory=list)
    product_type: ExtractedField | None = None
    defect_description: ExtractedField | None = None
    safety_risk: ExtractedField | None = None
    remedy_program: ExtractedField | None = None
    components: list[ExtractedField] = field(default_factory=list)
    page_count: int | None = None
    pages: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """An extraction is usable for matching only if every criterion the
        assessment engine requires was found and validated."""
        return bool(
            self.manufacturer
            and self.vehicle_groups
            and self.feature_codes
            and self.exclusions
            and not self.warnings
        )

    def summary(self) -> dict:
        return {
            "complete": self.is_complete,
            "vehicle_groups": len(self.vehicle_groups),
            "feature_codes": [f.value for f in self.feature_codes],
            "exclusions": len(self.exclusions),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Page-level text extraction
# ---------------------------------------------------------------------------


def extract_pages(pdf_bytes: bytes) -> dict[int, str]:
    """Extract text per page. Returns {1-based page number: text}."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise PDFExtractionError("PyMuPDF is not installed") from exc
    try:
        doc = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    except Exception as exc:
        raise PDFExtractionError(f"Could not open PDF: {exc}") from exc
    pages: dict[int, str] = {}
    try:
        for i, page in enumerate(doc):
            pages[i + 1] = page.get_text("text")
    finally:
        doc.close()
    if not pages:
        raise PDFExtractionError("PDF contained no pages")
    return pages


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def parse_report_date(text: str) -> date | None:
    """Parse dates as printed in Part 573 reports.

    Supported: 'Feb 05, 2024', 'February 5, 2024', '12/15/2022'.
    Returns None when the string cannot be parsed (never guesses).
    """
    text = text.strip().rstrip(".")
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$", text)
    if m:
        mon = m.group(1)[:3].title()
        if mon in MONTHS:
            try:
                return date(int(m.group(3)), MONTHS[mon], int(m.group(2)))
            except ValueError:
                return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _snippet(page_text: str, start: int, end: int, pad: int = 40) -> str:
    s = max(0, start - pad)
    e = min(len(page_text), end + pad)
    return re.sub(r"\s+", " ", page_text[s:e]).strip()


def _flat(page_text: str) -> str:
    """Normalize whitespace for regex matching while keeping offsets aligned.

    We collapse newlines to single spaces so label/value pairs that PyMuPDF
    emits on separate lines can be matched with one regex.
    """
    return re.sub(r"\s+", " ", page_text)


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------


def _find_label_value(pages: dict[int, str], label_pattern: str) -> ExtractedField | None:
    pat = re.compile(label_pattern + r":?\s*([^\n]{1,200})", re.IGNORECASE)
    for pno, text in pages.items():
        m = pat.search(text)
        if m:
            value = m.group(1).strip()
            if value:
                return ExtractedField(
                    value=value,
                    source_page=pno,
                    source_snippet=_snippet(text, m.start(), m.end()),
                )
    return None


def _parse_vehicle_groups(pages: dict[int, str]) -> list[ExtractedField]:
    """Parse 'Vehicle N:' blocks: model-year range, make/model, production
    dates, units. Each block becomes one ExtractedField (dict value)."""
    groups: list[ExtractedField] = []
    block_pat = re.compile(
        r"Vehicle\s+(\d+)\s*:\s*"
        r"(\d{4})\s*-\s*(\d{4})\s+([A-Z0-9 ]+?)\s+"
        r"Product Category:\s*(?P<cat>.+?)\s+"
        r"Product Type:\s*(?P<ptype>.+?)\s+"
        r"Fuel / Propulsion:\s*(?P<fuel>.+?)\s+"
        r"Production Dates:\s*(?P<pstart>[A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s*-\s*(?P<pend>[A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s+"
        r"Number of potentially involved:\s*(?P<units>[\d,]+)",
        re.DOTALL,
    )
    for pno, text in pages.items():
        flat = _flat(text)
        for m in block_pat.finditer(flat):
            my_start, my_end = int(m.group(2)), int(m.group(3))
            make_model = re.sub(r"\s+", " ", m.group(4)).strip()
            pstart = parse_report_date(m.group("pstart"))
            pend = parse_report_date(m.group("pend"))
            if pstart is None or pend is None or pstart > pend:
                continue  # invalid block: skip, validation flags incompleteness
            try:
                units = int(m.group("units").replace(",", ""))
            except ValueError:
                units = None
            value = {
                "vehicle_group": int(m.group(1)),
                "model_year_start": my_start,
                "model_year_end": my_end,
                # NOTE: make/model are NOT naively split here ("IC BUS CESB"
                # would split wrongly). The evidence layer reconciles the full
                # string against Make/Model returned by the NHTSA API.
                "make_model": make_model,
                "product_type": re.sub(r"\s+", " ", m.group("ptype")).strip(),
                "production_start": pstart.isoformat(),
                "production_end": pend.isoformat(),
                "units": units,
            }
            groups.append(
                ExtractedField(
                    value=value,
                    source_page=pno,
                    source_snippet=_snippet(flat, m.start(), m.end(), pad=20),
                )
            )
    groups.sort(key=lambda f: (f.value["model_year_start"], f.value["production_start"]))
    return groups


_SUSPECT_PAT = re.compile(
    r"The suspect population was identified by certain vehicles built\s+"
    r"(\d{1,2}/\d{1,2}/\d{4})\s+thru\s+(\d{1,2}/\d{1,2}/\d{4})\s+with\s+"
    r"(.+?)\s*\(feature code\s+([A-Z0-9]+)\)\s*\.\s*"
    r"(All other similar vehicles[^.]*\.)?",
    re.IGNORECASE | re.DOTALL,
)


def _parse_feature_and_exclusions(pages: dict[int, str]) -> tuple[list[ExtractedField], list[ExtractedField], ExtractedField | None]:
    feature_fields: dict[str, ExtractedField] = {}
    exclusion_fields: dict[str, ExtractedField] = {}
    overall: ExtractedField | None = None
    for pno, text in pages.items():
        flat = _flat(text)
        for m in _SUSPECT_PAT.finditer(flat):
            start, end = parse_report_date(m.group(1)), parse_report_date(m.group(2))
            feature_desc = re.sub(r"\s+", " ", m.group(3)).strip()
            code = m.group(4).strip().upper()
            snippet = _snippet(flat, m.start(), m.end(), pad=10)
            if start and end and (overall is None or (start, end) != (overall.value.get("start"), overall.value.get("end"))):
                overall = ExtractedField(
                    value={"start": start.isoformat(), "end": end.isoformat()},
                    source_page=pno,
                    source_snippet=snippet,
                )
            if code not in feature_fields:
                feature_fields[code] = ExtractedField(
                    value={"feature_code": code, "feature_description": feature_desc},
                    source_page=pno,
                    source_snippet=snippet,
                    confidence=0.97,
                )
            exclusion_text = (m.group(5) or "").strip()
            if exclusion_text and exclusion_text not in exclusion_fields:
                exclusion_fields[exclusion_text] = ExtractedField(
                    value={"exclusion_statement": exclusion_text, "feature_code": code},
                    source_page=pno,
                    source_snippet=snippet,
                    confidence=0.97,
                )
    return list(feature_fields.values()), list(exclusion_fields.values()), overall


def _parse_section(pages: dict[int, str], header: str, stop_headers: list[str]) -> ExtractedField | None:
    """Parse a free-text section: 'header:' up to the next known header/footer."""
    stops = "|".join(re.escape(s) for s in stop_headers)
    pat = re.compile(
        re.escape(header) + r":?\s*(?P<body>.+?)(?=(?:" + stops + r")|$)",
        re.DOTALL | re.IGNORECASE,
    )
    for pno, text in pages.items():
        flat = _flat(text)
        m = pat.search(flat)
        if m:
            body = re.sub(r"\s+", " ", m.group("body")).strip()
            # Cut trailing footer boilerplate.
            body = re.split(r"Part 573 Safety Recall Report", body)[0].strip()
            if len(body) > 10:
                return ExtractedField(
                    value=body,
                    source_page=pno,
                    source_snippet=_snippet(flat, m.start(), min(m.end(), m.start() + 400), pad=10),
                    confidence=0.9,
                )
    return None


def _parse_components(pages: dict[int, str]) -> list[ExtractedField]:
    out: list[ExtractedField] = []
    # Non-greedy across flattened text; part numbers end at the next label,
    # footer boilerplate, or a short alphanumeric boundary.
    pat = re.compile(
        r"Component Name\s+(?P<idx>\d+)\s*:\s*(?P<name>.+?)\s*"
        r"Component Description:\s*(?P<desc>.+?)\s*"
        r"Component Part Number:\s*(?P<pn>[A-Za-z0-9\-]+)",
        re.IGNORECASE,
    )
    for pno, text in pages.items():
        flat = _flat(text)
        for m in pat.finditer(flat):
            out.append(
                ExtractedField(
                    value={
                        "index": int(m.group("idx")),
                        "name": m.group("name").strip(),
                        "description": m.group("desc").strip(),
                        "part_number": m.group("pn").strip(),
                    },
                    source_page=pno,
                    source_snippet=_snippet(flat, m.start(), m.end(), pad=10),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_part573(pdf_bytes: bytes) -> Part573Extraction:
    """Extract structured criteria from a Part 573 report PDF."""
    pages = extract_pages(pdf_bytes)
    ex = Part573Extraction(pages=pages, page_count=len(pages))

    ex.manufacturer = _find_label_value(pages, r"Manufacturer Name")
    ex.submission_date = _find_label_value(pages, r"Submission Date")
    ex.manufacturer_recall_number = _find_label_value(pages, r"Manufacturer Recall No\.")
    total = _find_label_value(pages, r"Total number of potentially involved")
    if total:
        try:
            total.value = int(str(total.value).replace(",", ""))
        except ValueError:
            total = None
    ex.total_units = total

    ex.vehicle_groups = _parse_vehicle_groups(pages)
    ex.feature_codes, ex.exclusions, ex.overall_build_range = _parse_feature_and_exclusions(pages)

    if ex.vehicle_groups:
        first = ex.vehicle_groups[0].value
        ex.product_type = ExtractedField(
            value=first["product_type"],
            source_page=ex.vehicle_groups[0].source_page,
            source_snippet=ex.vehicle_groups[0].source_snippet,
        )

    footers = [
        "FMVSS1:",
        "Description of the safety risk",
        "Description of the cause:",
        "Identification of any warning",
        "Component Manufacturer",
        "Involved Components",
        "Chronology",
        "Related NHTSA Recall Number:",
        "Description of Remedy",
        "Remedy Type:",
        "Consumer Advisories:",
        "How remedy component differs",
        "Identify how/when recall condition",
        "Reimbursement Plan",
        "Recall Schedule",
        "Part 573 Safety Recall Report",
    ]
    ex.defect_description = _parse_section(pages, "Description of the defect or noncompliance", footers)
    ex.safety_risk = _parse_section(
        pages, "Description of the safety risk, including crash, fire, death, injury", footers
    )
    ex.remedy_program = _parse_section(pages, "Description of remedy program", footers)
    ex.components = _parse_components(pages)

    # ---- Validation --------------------------------------------------------
    if not ex.manufacturer:
        ex.warnings.append("Manufacturer name not found in report text")
    if not ex.vehicle_groups:
        ex.warnings.append("No vehicle groups with production date ranges could be parsed")
    if not ex.feature_codes:
        ex.warnings.append("No feature-code criterion found (required for matching)")
    if not ex.exclusions:
        ex.warnings.append("No exclusion statement found")
    for g in ex.vehicle_groups:
        v = g.value
        if v["model_year_start"] > v["model_year_end"]:
            ex.warnings.append(f"Vehicle group {v['vehicle_group']} has inverted model-year range")
    years = {(g.value["model_year_start"], g.value["model_year_end"]) for g in ex.vehicle_groups}
    if len(ex.vehicle_groups) != len(years):
        ex.warnings.append("Duplicate model-year ranges detected across vehicle groups")
    for f in ex.feature_codes:
        if not re.match(r"^[A-Z0-9]{4,12}$", str(f.value["feature_code"])):
            ex.warnings.append(f"Implausible feature code: {f.value['feature_code']}")
    if ex.overall_build_range:
        ov = ex.overall_build_range.value
        group_starts = [g.value["production_start"] for g in ex.vehicle_groups]
        group_ends = [g.value["production_end"] for g in ex.vehicle_groups]
        if group_starts and min(group_starts) < ov["start"]:
            ex.warnings.append("A vehicle group starts before the overall suspect build range")
        if group_ends and max(group_ends) > ov["end"]:
            ex.warnings.append("A vehicle group ends after the overall suspect build range")

    logger.info("Part 573 extraction: %s", ex.summary())
    return ex


def extraction_timestamp() -> datetime:
    return datetime.now(timezone.utc)

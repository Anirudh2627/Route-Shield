"""LLM-assisted extraction via Vultr Serverless Inference (OpenAI-compatible).

ARCHITECTURAL CONSTRAINT (non-negotiable):
    The LLM has NO write access to operational records. This module only:
      * reads report text,
      * asks the model for a strict structured JSON extraction,
      * validates the output against the `LLMRecallExtraction` Pydantic schema.
    Its output is marked extraction_method="LLM_ASSISTED" and
    review_status="NEEDS_REVIEW" - a human must review it before it can back
    matching. The LLM NEVER classifies vehicles, NEVER proposes approvals that
    auto-apply, and NEVER decides whether a bus is affected.

If the API key is missing, the request fails, or the JSON is invalid, this
module raises LLMExtractionError and the caller continues with the
rule-based extraction. Nothing depends on the LLM succeeding.
"""
from __future__ import annotations

import json
import logging

from ..config import get_settings
from ..schemas import LLMRecallExtraction

logger = logging.getLogger("routeshield.llm")

_EXTRACTION_SYSTEM_PROMPT = """You are a recall-document evidence extractor for RouteShield.
You receive text pages from an NHTSA Part 573 Safety Recall Report.
Extract ONLY what the document explicitly states. Rules:
- Do not invent criteria, dates, feature codes or exclusions.
- If a field is not present in the text, return null (or an empty list).
- Preserve exact values as printed (dates, codes, counts).
- Cite the page number where each production range appears.
- Return strict JSON matching the provided schema. No prose outside the JSON."""

_EXTRACTION_USER_TEMPLATE = """Extract the recall applicability criteria from this Part 573 report text.

Return JSON with fields: manufacturer, manufacturer_recall_number, submission_date, make, model,
product_type, total_units_affected, production_ranges (list of {{model_year, production_start,
production_end, units, source_page, source_snippet}}), overall_build_start, overall_build_end,
feature_codes (list of strings), feature_code_context, exclusions (list of statements),
defect_description, safety_consequence, remedy, components (list of names).

REPORT TEXT (pages delimited):
{pages_text}
"""


class LLMExtractionError(Exception):
    """LLM extraction unavailable or invalid - caller must fall back."""


def llm_available() -> bool:
    settings = get_settings()
    return bool(settings.llm_enabled and settings.vultr_inference_api_key and settings.vultr_inference_model)


def extract_with_llm(pages: dict[int, str], max_chars_per_page: int = 6000) -> LLMRecallExtraction:
    """Ask the Vultr Serverless Inference model for a structured extraction.

    Raises LLMExtractionError on ANY failure (not configured, HTTP error,
    invalid JSON, schema violation). Output is validated by Pydantic before
    it is returned; invalid model output is rejected outright.
    """
    if not llm_available():
        raise LLMExtractionError(
            "Vultr Serverless Inference is not configured "
            "(VULTR_INFERENCE_API_KEY / VULTR_INFERENCE_MODEL)."
        )
    settings = get_settings()
    pages_text = "\n\n".join(
        f"--- PAGE {pno} ---\n{text[:max_chars_per_page]}" for pno, text in sorted(pages.items())
    )
    user_prompt = _EXTRACTION_USER_TEMPLATE.format(pages_text=pages_text)

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.vultr_inference_api_key,
            base_url=settings.vultr_inference_base_url,
            timeout=settings.http_timeout_seconds * 2,
        )
        response = client.chat.completions.create(
            model=settings.vultr_inference_model,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - any inference failure => fallback
        raise LLMExtractionError(f"Vultr Serverless Inference request failed: {exc}") from exc

    try:
        payload = json.loads(_strip_code_fences(content))
        parsed = LLMRecallExtraction.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMExtractionError(f"LLM returned invalid structured output: {exc}") from exc

    logger.info("LLM extraction succeeded (needs human review before use)")
    return parsed


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()

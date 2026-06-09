"""
Pydantic extraction output schema — validates AI/PDF extraction payloads.

Amendment V1.2, Section 9.1 — Visual Bounding-Box Attestation:
  Every element extracted via AI (Claude API) or PDF parsing MUST include:
    - ``page_number``           : integer ≥ 1
    - ``bounding_box_coordinates``: BoundingBox with x_min, y_min, x_max, y_max

  PROHIBITED: any extraction payload that omits page_number or
  bounding_box_coordinates. Such payloads MUST be rejected at the
  validation layer — they cannot be ingested into financial_line_items.

  Purpose: bounding-box provenance enables human reviewers to locate the
  exact source location in the original PDF for any extracted value, and
  satisfies the SOX 404 requirement for traceable data lineage.

Data quality watermarking (Amendment V1.2 §6.2):
  AI-extracted values must be annotated with lineage context when written
  to Excel cells (Sheet 8 and cell-level comments). The lineage string is:

      [Lineage Context: AI-Assisted Non-XBRL Extraction |
       Document Source: Page {page_number} |
       Confidence: {confidence_pct}%]

  The ``ExtractionElement`` model carries the fields needed to build this
  string. The Excel export layer (services/export/) is responsible for
  rendering it into cell comments (Amendment V1.2 §6.2 — TODO M6).

Sign convention (Amendment V1.2 §2.2):
  Extracted values are stored as reported by the source document.
  Sign inversion for outflow/expense items is applied in the
  normalisation layer (services/extraction/normaliser/), NOT here.

Milestone: Phase 2 (schema pre-provisioned by Amendment V1.2 compliance sweep)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Bounding box (Amendment V1.2 §9.1)
# ---------------------------------------------------------------------------


class BoundingBox(BaseModel):
    """
    Pixel or point coordinates of an extracted element within its PDF page.

    All coordinates are relative to the top-left corner of the page
    (x increases right, y increases down). Units are typically PDF points
    (1 pt = 1/72 inch) but the normalisation layer handles unit conversion.

    Amendment V1.2 §9.1: presence is mandatory on every AI/PDF extraction.
    """

    x_min: float = Field(..., description="Left edge of the bounding box.")
    y_min: float = Field(..., description="Top edge of the bounding box.")
    x_max: float = Field(..., description="Right edge of the bounding box.")
    y_max: float = Field(..., description="Bottom edge of the bounding box.")

    @model_validator(mode="after")
    def _validate_ordering(self) -> "BoundingBox":
        if self.x_max <= self.x_min:
            raise ValueError(f"x_max ({self.x_max}) must be > x_min ({self.x_min})")
        if self.y_max <= self.y_min:
            raise ValueError(f"y_max ({self.y_max}) must be > y_min ({self.y_min})")
        return self


# ---------------------------------------------------------------------------
# Individual extracted element (Amendment V1.2 §9.1)
# ---------------------------------------------------------------------------


class ExtractionElement(BaseModel):
    """
    A single financial data point extracted from an AI/PDF source.

    Amendment V1.2 §9.1: ``page_number`` and ``bounding_box_coordinates``
    are required (not Optional). Payloads missing either field are rejected
    by Pydantic validation before they reach the ingestion layer.

    Attributes:
        concept_label:   Human-readable label as it appears in the source
                         document (e.g. 'Total Revenue', 'Net Income').
        canonical_field: Normalised field name mapped to an XBRL concept
                         (e.g. 'us-gaap:Revenues'). None if unmapped.
        raw_value:       The value string as extracted from the source
                         (may include currency symbols, commas, parentheses).
        parsed_value:    The numeric value after cleaning. None if parsing
                         failed; caller should treat as extraction error.
        currency:        ISO 4217 currency code detected in the source cell.
        unit_multiplier: Scale factor implied by the table header
                         (e.g. 1000 for "$ thousands", 1000000 for "$ millions").
        statement_type:  Statement classification: BS | IS | CF.
        page_number:     1-indexed page number where the element was found.
                         REQUIRED (Amendment V1.2 §9.1).
        bounding_box_coordinates: Pixel/point coordinates of the element.
                         REQUIRED (Amendment V1.2 §9.1).
        confidence_pct:  0–100 confidence score from the AI model.
        extraction_method: Source of the extraction: 'ai' | 'pdf' | 'ocr'.
    """

    concept_label: str = Field(..., description="Label as it appears in the source document.")
    canonical_field: str | None = Field(
        None,
        description="Normalised XBRL concept tag. None if unmapped.",
    )
    raw_value: str = Field(..., description="Raw value string from source, before parsing.")
    parsed_value: Decimal | None = Field(
        None,
        description="Numeric value after cleaning. None on parse failure.",
    )
    currency: str | None = Field(
        None,
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code (e.g. 'USD', 'INR').",
    )
    unit_multiplier: int = Field(
        default=1,
        description="Scale implied by the table header (1, 1000, or 1000000).",
    )
    statement_type: Literal["BS", "IS", "CF"] = Field(
        ...,
        description="Statement classification: BS | IS | CF.",
    )

    # Amendment V1.2 §9.1 — MANDATORY provenance fields.
    page_number: int = Field(
        ...,
        ge=1,
        description=(
            "1-indexed PDF page number. "
            "REQUIRED by Amendment V1.2 §9.1 — payloads without this field "
            "are rejected before ingestion."
        ),
    )
    bounding_box_coordinates: BoundingBox = Field(
        ...,
        description=(
            "Pixel/point coordinates of the element on the page. "
            "REQUIRED by Amendment V1.2 §9.1 — payloads without this field "
            "are rejected before ingestion."
        ),
    )

    # AI quality metadata (Amendment V1.2 §6.2 — lineage watermarking).
    confidence_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="AI model confidence score (0–100).",
    )
    extraction_method: Literal["ai", "pdf", "ocr"] = Field(
        default="ai",
        description="How this element was extracted.",
    )

    @property
    def lineage_comment(self) -> str:
        """
        Build the Amendment V1.2 §6.2 lineage context string for Excel cell comments.

        Format:
            [Lineage Context: AI-Assisted Non-XBRL Extraction |
             Document Source: Page {page_number} |
             Confidence: {confidence_pct:.0f}%]
        """
        return (
            f"[Lineage Context: AI-Assisted Non-XBRL Extraction | "
            f"Document Source: Page {self.page_number} | "
            f"Confidence: {self.confidence_pct:.0f}%]"
        )


# ---------------------------------------------------------------------------
# Full extraction payload (one document → many elements)
# ---------------------------------------------------------------------------


class ExtractionPayload(BaseModel):
    """
    Complete extraction output from one AI/PDF pass over a filing document.

    Attributes:
        source_file_hash:   SHA-256 hex digest of the source document.
                            REQUIRED — links all extracted elements back to
                            stored_documents.content_hash (Amendment V1.2 §4.2).
        accession_number:   SEC accession number of the filing.
        elements:           List of extracted elements. Each must pass
                            ExtractionElement validation including bounding
                            box attestation (Amendment V1.2 §9.1).
        model_version:      AI model identifier used for extraction.
        extraction_timestamp: ISO 8601 UTC timestamp of extraction.
    """

    source_file_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 hex digest of the source document (64 hex chars).",
    )
    accession_number: str = Field(..., description="SEC EDGAR accession number.")
    elements: list[ExtractionElement] = Field(
        default_factory=list,
        description="Extracted elements. Each must include page + bounding box.",
    )
    model_version: str | None = Field(
        None,
        description="AI model identifier (e.g. 'claude-opus-4-8').",
    )
    extraction_timestamp: str | None = Field(
        None,
        description="ISO 8601 UTC timestamp of when extraction was performed.",
    )

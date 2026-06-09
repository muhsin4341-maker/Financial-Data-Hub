"""
Document parser abstract base — Strategy Pattern foundation.

Architecture position:

  ParserFactory (factory.py)
    ↓  selects concrete parser by document_type / country_code / reporting_standard
  DocumentParser (base.py) ← this module
    ↓  abstract contract
  SEC10KParser   (sec_10k.py)   — US-GAAP / SEC filings
  IndiaBRSRParser (india_brsr.py) — SEBI BRSR / IND-AS / ESG

Design rationale (Engineering Spec §7.1, Amendment V1.2):
  A single monolithic parser coupled exclusively to SEC/US-GAAP structures
  breaks on non-US documents (SEBI BRSR, MCA AOC, IFRS annual reports).
  The Strategy Pattern decouples document-type-specific extraction logic
  from the task layer, which only interacts with the ``DocumentParser``
  interface.  Adding support for a new filing type requires:
    1. Subclass ``DocumentParser``.
    2. Register the new parser in ``ParserFactory._REGISTRY``.
  No changes to the extraction task or AIExtractionService are needed.

Thread-safety:
  Parser instances are stateless (no instance-level mutable state after
  __init__).  Creating a fresh instance per task invocation is the
  recommended pattern.

Milestone: M4.2 — International Parser Strategy (Task 2)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# ParsedLineItem — common output unit from all parsers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedLineItem:
    """
    A single financial line item produced by any DocumentParser implementation.

    Fields mirror ``FinancialLineItemCreate`` closely so the task layer can
    pass them straight through to ``FinancialLineItemRepository.bulk_upsert()``.

    Attributes:
        concept_label:    Human-readable label as it appears in the source document.
        canonical_field:  XBRL/taxonomy concept tag (e.g. ``us-gaap:Revenues``),
                          or a ``raw:`` prefixed slug when unknown.
        statement_type:   ``IS`` | ``BS`` | ``CF``.
        value_reported:   Numeric value in the reported currency (already sign-correct).
        reported_currency: ISO 4217 code (e.g. ``USD``, ``INR``), or None.
        value_usd:        USD-equivalent value; None for non-USD until FX pass.
        unit_multiplier:  Scaling factor applied (1, 1_000, or 1_000_000).
        fiscal_year:      4-digit fiscal year.
        fiscal_period:    ``FY`` | ``Q1`` | ``Q2`` | ``Q3`` | ``Q4``.
        filing_date:      Date the document was filed with the regulator.
        reporting_standard: Accounting standard string (US_GAAP | IFRS | IND_AS).
        reporting_framework: Free-text filing framework (SEC_10K, SEBI_BRSR, MCA_AOC …).
        page_number:      1-indexed page number; None when unavailable.
        confidence_pct:   Parser confidence 0–100.
        source_file_hash: SHA-256 hex digest of the source document, or None.
        extraction_method: Always ``"parser"`` for DocumentParser subclasses.
    """

    concept_label: str
    canonical_field: str
    statement_type: str  # "IS" | "BS" | "CF"
    value_reported: float
    reported_currency: str | None
    value_usd: float | None
    unit_multiplier: int
    fiscal_year: int
    fiscal_period: str
    filing_date: date
    reporting_standard: str
    reporting_framework: str
    page_number: int | None = field(default=None)
    confidence_pct: float = field(default=90.0)
    source_file_hash: str | None = field(default=None)
    extraction_method: str = field(default="parser")


# ---------------------------------------------------------------------------
# ParseResult — aggregate returned by DocumentParser.parse()
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """
    Aggregate output returned by ``DocumentParser.parse()``.

    Attributes:
        line_items:         Extracted financial line items (may be empty on failure).
        source_file_hash:   SHA-256 hex digest of the parsed document bytes.
        parser_name:        Identifier of the concrete parser used (e.g. ``sec_10k``).
        reporting_framework: Framework detected or assumed (e.g. ``SEBI_BRSR``).
        metadata:           Parser-specific metadata (page counts, table counts, etc.)
        warnings:           Non-fatal issues encountered during parsing.
        errors:             Fatal errors; if non-empty, ``line_items`` is usually empty.
    """

    line_items: list[ParsedLineItem] = field(default_factory=list)
    source_file_hash: str | None = field(default=None)
    parser_name: str = field(default="unknown")
    reporting_framework: str = field(default="UNKNOWN")
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when no fatal errors were recorded."""
        return len(self.errors) == 0

    @property
    def item_count(self) -> int:
        """Total number of extracted line items."""
        return len(self.line_items)


# ---------------------------------------------------------------------------
# DocumentParser — abstract base
# ---------------------------------------------------------------------------


class DocumentParser(ABC):
    """
    Abstract base class for all document-type-specific financial parsers.

    Concrete subclasses implement the Strategy interface by overriding
    ``parse()``.  The ``ParserFactory`` selects the appropriate concrete
    strategy based on (document_type, country_code, reporting_standard).

    Subclassing contract:
      1. Override ``parser_name`` class attribute with a unique slug.
      2. Override ``supported_frameworks`` with the list of framework strings
         this parser handles (used by the factory for validation).
      3. Implement ``parse()`` — the only required abstract method.
      4. Optionally override ``can_handle()`` for custom selection logic
         beyond simple framework matching.

    Args:
        settings: Optional settings override.  Defaults to ``get_settings()``.
    """

    # Subclasses must define these.
    parser_name: str = "base"
    supported_frameworks: tuple[str, ...] = ()

    def __init__(self, settings: Any | None = None) -> None:
        from apps.api.core.config import get_settings
        import structlog

        self._settings = settings or get_settings()
        self._log = structlog.get_logger(self.__class__.__module__).bind(
            parser=self.parser_name
        )

    # -------------------------------------------------------------------------
    # Abstract method — subclasses must implement
    # -------------------------------------------------------------------------

    @abstractmethod
    async def parse(
        self,
        *,
        file_bytes: bytes,
        company_id: str,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        source_file_hash: str | None = None,
    ) -> ParseResult:
        """
        Parse a document and extract financial line items.

        Args:
            file_bytes:         Raw bytes of the document (PDF, HTML, XBRL, etc.)
            company_id:         Company UUID as string (for lineage tagging).
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      ``FY`` | ``Q1`` | ``Q2`` | ``Q3`` | ``Q4``.
            filing_date:        Date the document was filed with the regulator.
            reporting_standard: Accounting standard: ``US_GAAP`` | ``IFRS`` | ``IND_AS``.
            source_file_hash:   SHA-256 hex digest of ``file_bytes`` (pre-computed
                                by the caller); computed internally if None.

        Returns:
            ``ParseResult`` containing extracted ``ParsedLineItem`` objects,
            metadata, and any warnings / fatal errors.
        """

    # -------------------------------------------------------------------------
    # Optional hook — override for custom selection logic
    # -------------------------------------------------------------------------

    @classmethod
    def can_handle(
        cls,
        *,
        document_type: str,
        country_code: str,
        reporting_standard: str,
    ) -> bool:
        """
        Return True when this parser strategy can handle the given document.

        The default implementation matches ``reporting_standard`` against
        ``supported_frameworks``.  Override for more nuanced logic.

        Args:
            document_type:      File type hint: ``pdf``, ``xbrl``, ``html``, etc.
            country_code:       ISO 3166-1 alpha-2 (``US``, ``IN``, ``GB`` …).
            reporting_standard: Accounting standard string.

        Returns:
            True if this parser should be selected; False otherwise.
        """
        return reporting_standard.upper() in (f.upper() for f in cls.supported_frameworks)

    # -------------------------------------------------------------------------
    # Shared utility — SHA-256 hash
    # -------------------------------------------------------------------------

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        """Return the SHA-256 hex digest of ``data``."""
        import hashlib

        return hashlib.sha256(data).hexdigest()

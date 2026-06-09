"""
ParserFactory — Strategy selector for document-type-specific parsers.

Architecture position:

  extraction_tasks.py (Celery task)
    ↓  calls ParserFactory.get_parser(...)
  ParserFactory (this module)
    ↓  resolves to correct DocumentParser subclass
  DocumentParser subclass (sec_10k.py | india_brsr.py | …)
    ↓  returns ParseResult

Usage example:

    parser = ParserFactory.get_parser(
        document_type="pdf",
        country_code="IN",
        reporting_standard="IND_AS",
    )
    result = await parser.parse(file_bytes=..., ...)

Adding a new parser:
  1. Create a new module (e.g. ``eu_ifrs.py``) with a ``DocumentParser`` subclass.
  2. Add an import + entry to ``ParserFactory._REGISTRY`` in this file.
  3. No other files need to change.

Milestone: M4.2 — International Parser Strategy (Task 2)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.ingestion.parsers.base import DocumentParser


# ---------------------------------------------------------------------------
# Registry — maps canonical framework strings to parser classes
# ---------------------------------------------------------------------------

# Populated lazily on first call to avoid circular-import issues at module load.
# Each entry is (document_type_glob | "*", country_code | "*", reporting_standard).
# The registry is ordered; the first match wins.
_REGISTRY_LOCK: threading.Lock = threading.Lock()
_REGISTRY_LOADED: bool = False
_REGISTRY: list[type[DocumentParser]] = []


def _load_registry() -> None:
    """
    Populate the registry with all known parser classes (lazy, thread-safe, once).

    Uses a module-level lock to prevent a race condition where two threads both
    observe ``_REGISTRY_LOADED = False`` and simultaneously import + overwrite
    ``_REGISTRY``.  After the lock is acquired a second check (double-checked
    locking pattern) ensures only the first thread performs the work.
    """
    global _REGISTRY_LOADED, _REGISTRY  # noqa: PLW0603

    # Fast path — no lock needed after the first load
    if _REGISTRY_LOADED:
        return

    with _REGISTRY_LOCK:
        # Double-checked lock — re-test inside the critical section
        if _REGISTRY_LOADED:
            return

        from services.ingestion.parsers.india_brsr import IndiaBRSRParser
        from services.ingestion.parsers.sec_10k import SEC10KParser

        # Order matters: more specific parsers first.
        # If multiple parsers return True from can_handle(), the first wins.
        _REGISTRY = [
            IndiaBRSRParser,  # IND_AS / SEBI_BRSR / MCA_AOC / IN+IFRS
            SEC10KParser,     # US_GAAP / SEC form types (broad fallback for US)
        ]
        _REGISTRY_LOADED = True


# ---------------------------------------------------------------------------
# ParserFactory
# ---------------------------------------------------------------------------


class ParserFactory:
    """
    Selects and instantiates the appropriate ``DocumentParser`` strategy.

    The factory is a stateless class with only class methods — there is no
    instance state to manage.  Thread-safe under read-only access.

    Methods:
        get_parser:  Primary entry point — returns a ready-to-use parser.
        list_parsers: Returns all registered parser names (useful for diagnostics).
    """

    @classmethod
    def get_parser(
        cls,
        *,
        document_type: str = "pdf",
        country_code: str = "US",
        reporting_standard: str = "US_GAAP",
        settings: Any | None = None,
    ) -> "DocumentParser":
        """
        Return the best-matching parser strategy for the given document context.

        Selection algorithm:
          1. Iterate the registry in order.
          2. Call ``parser_class.can_handle(document_type, country_code, reporting_standard)``.
          3. Return the first match, instantiated with ``settings``.
          4. If nothing matches, fall back to ``SEC10KParser`` (US-GAAP default)
             and emit a warning log.

        Args:
            document_type:      File type hint: ``pdf``, ``xbrl``, ``ixbrl``,
                                ``html``, ``sec_10k``, ``sec_10q``, etc.
            country_code:       ISO 3166-1 alpha-2 (``US``, ``IN``, ``GB`` …).
                                Case-insensitive.
            reporting_standard: Accounting standard: ``US_GAAP``, ``IFRS``,
                                ``IND_AS``, ``SEBI_BRSR``, ``MCA_AOC``, etc.
                                Case-insensitive.
            settings:           Optional Settings override (defaults to
                                ``get_settings()`` inside the parser).

        Returns:
            Instantiated ``DocumentParser`` subclass.

        Raises:
            Never raises — always returns a parser (worst case: the default
            ``SEC10KParser`` with a warning log).
        """
        import structlog

        log = structlog.get_logger(__name__)
        _load_registry()

        dt_norm = (document_type or "pdf").lower().strip()
        cc_norm = (country_code or "US").upper().strip()
        rs_norm = (reporting_standard or "US_GAAP").upper().strip()

        for parser_cls in _REGISTRY:
            if parser_cls.can_handle(
                document_type=dt_norm,
                country_code=cc_norm,
                reporting_standard=rs_norm,
            ):
                log.debug(
                    "parser_factory.selected",
                    parser=parser_cls.parser_name,
                    document_type=dt_norm,
                    country_code=cc_norm,
                    reporting_standard=rs_norm,
                )
                return parser_cls(settings=settings)

        # Fallback — SEC10KParser handles the broadest range of documents.
        from services.ingestion.parsers.sec_10k import SEC10KParser

        log.warning(
            "parser_factory.fallback",
            document_type=dt_norm,
            country_code=cc_norm,
            reporting_standard=rs_norm,
            fallback="sec_10k",
            resolution=(
                "No registered parser matched the document context.  "
                "Falling back to SEC10KParser (US-GAAP / AI extraction).  "
                "Add a new parser subclass and register it in "
                "services/ingestion/parsers/factory.py to eliminate this warning."
            ),
        )
        return SEC10KParser(settings=settings)

    @classmethod
    def list_parsers(cls) -> list[dict[str, Any]]:
        """
        Return metadata for all registered parser classes.

        Useful for diagnostics, admin endpoints, and the parser selection UI.

        Returns:
            List of dicts: ``[{'name': str, 'frameworks': list[str]}, …]``
        """
        _load_registry()
        return [
            {
                "name": p.parser_name,
                "frameworks": list(p.supported_frameworks),
                "class": p.__name__,
            }
            for p in _REGISTRY
        ]

    @classmethod
    def get_parser_for_job(cls, job: Any, settings: Any | None = None) -> "DocumentParser":
        """
        Convenience wrapper — select parser from a ``FinancialJob`` ORM object.

        Reads ``job.reporting_standard`` and ``job.country_code`` (if present)
        to determine the correct strategy.  Falls back gracefully when the
        attributes are absent or None.

        Args:
            job:      FinancialJob ORM instance (or any object with compatible attrs).
            settings: Optional Settings override.

        Returns:
            Instantiated DocumentParser.
        """
        reporting_standard = str(
            getattr(job, "reporting_standard", None) or "US_GAAP"
        )
        country_code = str(getattr(job, "country_code", None) or "US")
        document_type = str(getattr(job, "document_type", None) or "pdf")

        return cls.get_parser(
            document_type=document_type,
            country_code=country_code,
            reporting_standard=reporting_standard,
            settings=settings,
        )

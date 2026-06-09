"""
Acquisition source protocol and public re-exports.

This module defines:

  BaseSource
    A ``typing.Protocol`` that every acquisition source must structurally
    satisfy.  Sources do NOT need to inherit from ``BaseSource`` explicitly —
    Python's structural subtyping means any class with the required methods
    passes ``isinstance(obj, BaseSource)`` checks at runtime.

  Re-exports of the shared dataclasses produced by SEC EDGAR:
    FilingMetadata        — one discovered filing (accession number, dates, URLs)
    FilingDiscoveryResult — wrapper returned by ``discover_filings``

  Re-export of the concrete implementation:
    SECEdgarSource        — default source for US regulatory filings

Usage in SourceRegistry::

    from services.acquisition.source_registry.sources import (
        BaseSource,
        FilingDiscoveryResult,
        FilingMetadata,
        SECEdgarSource,
    )

    source: BaseSource = SECEdgarSource(user_agent=settings.edgar_user_agent)
    assert isinstance(source, BaseSource)   # runtime check passes
    result: FilingDiscoveryResult = await source.discover_filings("0000320193")

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Shared dataclasses are defined inside sec_edgar.py because all current data
# fields are SEC-specific (accession_number, form_type, edgar_url, etc.).
# They are re-exported here so callers can import from the package root rather
# than reaching into the sec_edgar module directly.
from services.acquisition.source_registry.sources.sec_edgar import (
    FilingDiscoveryResult,
    FilingMetadata,
    SECEdgarSource,
)

__all__ = [
    "BaseSource",
    "FilingDiscoveryResult",
    "FilingMetadata",
    "SECEdgarSource",
]


@runtime_checkable
class BaseSource(Protocol):
    """
    Structural protocol for all financial-data acquisition sources.

    Any class that implements the three required methods below satisfies this
    protocol and will pass ``isinstance(obj, BaseSource)`` at runtime.

    Required methods
    ----------------
    discover_filings(cik, ...)
        Async.  Discovers available filings for a company identified by its
        CIK string.  Returns a ``FilingDiscoveryResult`` containing a list of
        ``FilingMetadata`` objects ordered newest-first.

    build_document_url(cik, accession_number, filename)
        Synchronous.  Builds the authoritative download URL for a single filing
        document.  No network call is made; the URL is constructed from the
        known pattern of the underlying data source.

    close()
        Async.  Releases any held resources (HTTP client sessions, connection
        pools).  Safe to call multiple times.  The ``SourceRegistry`` calls
        this during application shutdown.

    Extending the protocol
    ----------------------
    Additional optional helpers (``get_submissions``, ``get_filing_metadata``,
    etc.) are NOT part of this protocol — they are implementation details of
    ``SECEdgarSource``.  Adding a new source only requires the three methods
    above; richer functionality can be added progressively.
    """

    async def discover_filings(
        self,
        cik: str,
        *,
        form_types: frozenset[str] | None = None,
        max_filings: int | None = None,
        include_older: bool = False,
    ) -> FilingDiscoveryResult:
        """
        Discover available filings for the company identified by ``cik``.

        Args:
            cik:          CIK string (zero-padded to 10 digits, e.g.
                          ``"0000320193"``).
            form_types:   Optional set of form types to filter by
                          (e.g. ``frozenset({"10-K", "10-Q"})``).
                          ``None`` means return all supported types.
            max_filings:  Optional cap on the total number of filings returned.
                          ``None`` means no cap — return everything discovered.
            include_older: When ``True``, also fetch paginated older filings
                          beyond the most-recent block.  Defaults to ``False``
                          (only recent filings, ≤1000 for SEC EDGAR).

        Returns:
            ``FilingDiscoveryResult`` with ``filings`` list and metadata.

        Raises:
            Any source-specific exception on network or parsing failure.
        """
        ...

    def build_document_url(
        self,
        cik: str,
        accession_number: str,
        filename: str,
    ) -> str:
        """
        Build the full download URL for a single filing document.

        Args:
            cik:              CIK string (same format as ``discover_filings``).
            accession_number: SEC-format accession number with dashes
                              (e.g. ``"0000320193-23-000077"``).
            filename:         Filename from the filing index
                              (e.g. ``"aapl-20230930.htm"``).

        Returns:
            Absolute URL string.  No network call is made.
        """
        ...

    async def close(self) -> None:
        """
        Release held resources.

        Called by ``SourceRegistry`` on application shutdown and after any
        one-shot usage.  Implementations must be idempotent — multiple ``close``
        calls must not raise.
        """
        ...

"""
Company resolver provider interface.

Defines:
  CompanyResolutionError  — raised by CompanyResolverService when all
                            resolution strategies are exhausted.
  CompanyInfo             — canonical resolved company data record.
  CompanyResolverProvider — abstract base class all resolver implementations
                            must satisfy.

Concrete implementations:
  SECCompanyResolver  — US companies via SEC EDGAR public APIs.
  Future: NSECompanyResolver, BSECompanyResolver.

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class CompanyResolutionError(Exception):
    """
    Raised by CompanyResolverService when a ticker or CIK cannot be resolved
    to a canonical CompanyInfo record after all strategies are exhausted.

    Attributes
    ----------
    query:
        The original ticker or CIK string that could not be resolved.
    strategies_tried:
        Human-readable list of strategies that were attempted before failure.

    Usage::

        try:
            info = await resolver.resolve_by_ticker("UNKNOWN")
        except CompanyResolutionError as exc:
            logger.error("resolution failed", query=exc.query)
            # The acquisition job transitions to FAILED with exc.message.

    This exception is intentionally NOT a subclass of ValueError so that
    callers can distinguish resolution failures from programming errors.

    The AcquisitionJobService catches this exception and transitions the
    FinancialJob to FAILED with exc's message written to error_message.
    """

    def __init__(
        self,
        query: str,
        *,
        strategies_tried: list[str] | None = None,
        reason: str | None = None,
    ) -> None:
        self.query = query
        self.strategies_tried: list[str] = strategies_tried or []
        self.reason = reason

        # Build a descriptive message that surfaces directly in job error logs.
        strategies_str = (
            ", ".join(self.strategies_tried) if self.strategies_tried else "none recorded"
        )
        detail = f" ({reason})" if reason else ""
        message = (
            f"Could not resolve company identifier {query!r}{detail}. "
            f"Strategies tried: [{strategies_str}]. "
            "Verify the ticker is valid and SEC EDGAR is reachable."
        )
        super().__init__(message)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class CompanyInfo:
    """
    Canonical company identification record.

    Produced by all CompanyResolverProvider implementations and cached
    by CompanyResolverService. Fields match the VG-06 validation contract.
    """

    ticker: str
    company_name: str
    cik: str          # 10-digit zero-padded SEC CIK, e.g. '0000320193'
    exchange: str | None
    country: str      # ISO 3166-1 alpha-2, e.g. 'US'


class CompanyResolverProvider(ABC):
    """
    Abstract interface for company identifier resolution.

    Implementations must handle their own HTTP transport and connection
    management. CompanyResolverService is responsible for caching.

    Each provider is designed for one data source (SEC, NSE, BSE, etc.)
    and resolves identifiers within that source's namespace.
    """

    @abstractmethod
    async def resolve_ticker(self, ticker: str) -> CompanyInfo | None:
        """
        Look up a company by its stock ticker symbol.

        Args:
            ticker: Uppercase ticker symbol (e.g. 'AAPL').

        Returns:
            CompanyInfo if found, None if the ticker is unknown to this provider.
        """

    @abstractmethod
    async def resolve_cik(self, cik: str) -> CompanyInfo | None:
        """
        Look up a company by its SEC Central Index Key.

        Args:
            cik: 10-digit zero-padded CIK string (e.g. '0000320193').

        Returns:
            CompanyInfo if found, None if the CIK is unknown to this provider.
        """

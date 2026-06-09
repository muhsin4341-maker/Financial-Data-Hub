"""
Company Resolver package — public API.

Single import surface for all company resolution components::

    from services.acquisition.company_resolver import (
        AliasManager,
        CompanyInfo,
        CompanyResolutionError,
        CompanyResolverService,
    )

Milestone: M3.2 — Company Resolver
"""

from services.acquisition.company_resolver.alias_manager import AliasManager
from services.acquisition.company_resolver.provider import (
    CompanyInfo,
    CompanyResolutionError,
    CompanyResolverProvider,
)
from services.acquisition.company_resolver.resolver import CompanyResolverService
from services.acquisition.company_resolver.sec_resolver import SECCompanyResolver

__all__ = [
    "AliasManager",
    "CompanyInfo",
    "CompanyResolutionError",
    "CompanyResolverProvider",
    "CompanyResolverService",
    "SECCompanyResolver",
]

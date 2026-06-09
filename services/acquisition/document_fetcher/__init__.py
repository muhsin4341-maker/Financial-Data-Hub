"""
Document fetcher package — M3.5.

Public API::

    from services.acquisition.document_fetcher import (
        SECFilingDocumentFetcher,
        FilingDocument,
        ContentDeduplicator,
        DocumentFetchError,
        DocumentNotAvailableError,
        UnsupportedContentTypeError,
    )
"""

from services.acquisition.document_fetcher.deduplicator import ContentDeduplicator
from services.acquisition.document_fetcher.fetcher import (
    DocumentFetchError,
    DocumentNotAvailableError,
    DocumentParseError,
    FilingDocument,
    SECFilingDocumentFetcher,
    UnsupportedContentTypeError,
    compute_content_hash,
)

__all__ = [
    "ContentDeduplicator",
    "DocumentFetchError",
    "DocumentNotAvailableError",
    "DocumentParseError",
    "FilingDocument",
    "SECFilingDocumentFetcher",
    "UnsupportedContentTypeError",
    "compute_content_hash",
]

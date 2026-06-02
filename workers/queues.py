"""
Celery queue name constants.

Engineering Spec Part 2, Section 9.2 Decision 1.
Four dedicated queues with separate worker pools.
"""

QUEUE_FETCH   = "fetch"    # Document download, source discovery, company resolution (I/O bound)
QUEUE_AI      = "ai"       # Claude API extraction calls (API-rate-bound)
QUEUE_COMPUTE = "compute"  # Validation, normalisation, ratio calculation (CPU bound)
QUEUE_EXPORT  = "export"   # Excel file generation (memory bound)
QUEUE_DEFAULT = "default"  # Notifications, cache invalidation, misc

ALL_QUEUES = [QUEUE_FETCH, QUEUE_AI, QUEUE_COMPUTE, QUEUE_EXPORT, QUEUE_DEFAULT]

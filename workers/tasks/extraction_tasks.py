"""
Extraction Tasks — Celery task definitions.

Milestone: M4
All tasks must be idempotent (Engineering Spec Part 2, Section 9.2 Decision 2).
Tasks pass only job_id / record IDs between steps — no large payloads.
"""
from workers.celery_app import celery_app

# TODO M4: Implement tasks

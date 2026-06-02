"""
Celery application factory and task routing configuration.

Engineering Spec Part 2, Section 9.3.
Milestone: M0 (configuration) / M2 (first tasks added)
"""
from celery import Celery
from workers.queues import (
    QUEUE_AI, QUEUE_COMPUTE, QUEUE_DEFAULT, QUEUE_EXPORT, QUEUE_FETCH
)

# TODO M1: Import settings and use settings.redis_celery_broker_url
BROKER_URL = "redis://localhost:6379/1"
RESULT_BACKEND = "redis://localhost:6379/2"


def create_celery() -> Celery:
    """Celery application factory."""
    app = Celery("fdh", broker=BROKER_URL, backend=RESULT_BACKEND)

    app.conf.update(
        # Serialisation
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",

        # Timezone
        timezone="UTC",
        enable_utc=True,

        # Task behaviour
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_soft_time_limit=300,
        task_time_limit=360,

        # Queue routing
        task_routes={
            "workers.tasks.acquisition_tasks.*":  {"queue": QUEUE_FETCH},
            "workers.tasks.ingestion_tasks.*":    {"queue": QUEUE_FETCH},
            "workers.tasks.extraction_tasks.*":   {"queue": QUEUE_AI},
            "workers.tasks.validation_tasks.*":   {"queue": QUEUE_COMPUTE},
            "workers.tasks.export_tasks.*":       {"queue": QUEUE_EXPORT},
            "workers.tasks.notification_tasks.*": {"queue": QUEUE_DEFAULT},
        },

        # Default queue
        task_default_queue=QUEUE_DEFAULT,

        # Beat schedule (recurring tasks — M8)
        beat_schedule={
            # TODO M8: currency_rates_update — daily 06:00 UTC
            # TODO M8: expired_exports_cleanup — daily 02:00 UTC
            # TODO M8: source_health_check — every 4 hours
            # TODO M8: completed_jobs_archival — weekly
        },

        # Retry defaults per queue type are set on individual task decorators
    )

    # Auto-discover tasks in workers/tasks/
    app.autodiscover_tasks(["workers.tasks"])

    return app


celery_app = create_celery()

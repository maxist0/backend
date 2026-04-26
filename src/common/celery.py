#src/common/celery.py
from celery import Celery
import os


# Celery application instance
celery = Celery(
    "worker",
    broker=os.getenv("CELERY_BROKER_URL"),
    backend=os.getenv("CELERY_RESULT_BACKEND"),
    include=[
        "src.utils.tasks",         # general tasks
        "src.utils.email_tasks",   # transactional email tasks
    ],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Berlin",
    task_track_started=True,
    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Result expiry (24 h)
    result_expires=86400,
)
from celery import Celery

from app.config import settings
from app.observability import configure_langsmith

# Set in the worker master before prefork; children inherit os.environ, so the
# graph runs in review_pr / analyze_issue / auto_pr tasks auto-trace too.
configure_langsmith()

celery_app = Celery(
    "revet",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)

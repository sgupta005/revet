import asyncio
import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="index_repo",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def index_repo(
    repo_full_name: str,
    installation_id: int,
    changed_paths: list[str] | None = None,
) -> None:
    from ai.indexing.pipeline import run_index

    asyncio.run(run_index(repo_full_name, installation_id, changed_paths))


@celery_app.task(name="review_pr")
def review_pr(repo_full_name: str, installation_id: int, pr_number: int) -> None:
    logger.info(
        "review_pr queued repo=%s installation=%s pr=%s",
        repo_full_name,
        installation_id,
        pr_number,
    )


@celery_app.task(name="analyze_issue")
def analyze_issue(repo_full_name: str, installation_id: int, issue_number: int) -> None:
    logger.info(
        "analyze_issue queued repo=%s installation=%s issue=%s",
        repo_full_name,
        installation_id,
        issue_number,
    )


@celery_app.task(name="auto_pr")
def auto_pr(repo_full_name: str, installation_id: int, issue_number: int) -> None:
    logger.info(
        "auto_pr queued repo=%s installation=%s issue=%s",
        repo_full_name,
        installation_id,
        issue_number,
    )

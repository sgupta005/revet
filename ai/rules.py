"""Repo-scoped custom review rules (PRD §F7).

Rules are **per-repo** (decision 2026-07-02): each repository owns its own rule
set. The same loader is shared by every rule-aware feature — PR review (Phase 7),
issue analysis (Phase 8), and auto-PR (Phase 9) — so custom rules are injected
consistently everywhere. A fixed `MAX_RULES` cap bounds prompt size.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select

from ai.constants import MAX_RULES
from app.db.models import Repository, Rule


async def load_repo_and_rules(
    engine: AsyncEngine, repo: str
) -> tuple[int | None, list[str]]:
    """Return `(repo_id, rule_texts)` for `repo`.

    `repo_id` is None when the repo isn't in our DB (the feature still runs, but no
    activity row can be written). `rule_texts` are `"name: body"` strings, capped
    at `MAX_RULES` to keep the injected prompt bounded (PRD §F7).
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        repo_row = (
            await session.execute(
                select(Repository).where(Repository.full_name == repo)
            )
        ).scalar_one_or_none()
        if repo_row is None:
            return None, []
        rules = (
            await session.execute(
                select(Rule).where(Rule.repository_id == repo_row.id).limit(MAX_RULES)
            )
        ).scalars().all()
        return repo_row.id, [f"{r.name}: {r.body}" for r in rules]

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IndexingStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PRKind(str, Enum):
    REVIEW = "review"
    AUTO_PR = "auto-pr"


class Installation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    github_installation_id: int = Field(unique=True, index=True)
    account_login: str
    account_type: str  # "User" or "Organization"
    created_at: datetime = Field(default_factory=_utcnow)


class Repository(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    installation_id: int = Field(foreign_key="installation.id", index=True)
    full_name: str = Field(unique=True, index=True)  # "owner/repo"
    github_repo_id: int
    indexing_status: IndexingStatus = Field(default=IndexingStatus.NOT_STARTED)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Rule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    installation_id: int = Field(foreign_key="installation.id", index=True)
    name: str
    body: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PullRequest(SQLModel, table=True):
    __tablename__ = "pull_request"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    github_pr_number: int
    kind: PRKind
    state: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Issue(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    github_issue_number: int
    state: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

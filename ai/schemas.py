from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
FixAction = Literal["create", "update", "delete"]


class ReviewFinding(BaseModel):
    """A single issue raised by a PR reviewer; the structured-output shape every
    reviewer (correctness/security/quality/custom-rules) emits."""

    file: str = Field(description="Repository-relative path of the affected file.")
    line: int = Field(description="1-based line number the finding refers to.")
    severity: Severity = Field(description="Impact of the finding.")
    category: str = Field(
        description="Reviewer perspective, e.g. correctness, security, quality, custom-rule."
    )
    comment: str = Field(description="What is wrong and how to fix it.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Reviewer confidence in this finding, 0–1."
    )


class FixFile(BaseModel):
    """One file the auto-PR plan intends to create, update, or delete."""

    path: str = Field(description="Repository-relative path to change.")
    action: FixAction = Field(description="Whether to create, update, or delete the file.")
    rationale: str = Field(description="Why this file must change to resolve the issue.")


class FixPlan(BaseModel):
    """The strict JSON plan an auto-PR produces before generating file contents."""

    summary: str = Field(description="One-line summary of the fix.")
    approach: str = Field(description="How the change resolves the issue.")
    files: list[FixFile] = Field(description="Files to change, with action and rationale.")


class RelevanceGrade(BaseModel):
    """Corrective-RAG document grade deciding whether retrieved context is usable."""

    relevant: bool = Field(
        description="True if the retrieved code is relevant enough to answer the question."
    )

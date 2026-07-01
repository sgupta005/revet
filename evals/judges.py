"""LLM-as-judge evaluators (PRD §F8).

Each evaluator uses the modern langsmith signature
``(inputs, outputs, reference_outputs) -> dict`` and returns
``{"key", "score", "comment"}`` with a 0–1 score. The judge is a cheap model
(`GRADER_MODEL`) at temperature 0 emitting a structured `JudgeVerdict`.
"""

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage

from ai.constants import GRADER_MODEL
from ai.llm import make_chat_model


class JudgeVerdict(BaseModel):
    score: float = Field(ge=0.0, le=1.0, description="Quality score from 0 (bad) to 1 (excellent).")
    reasoning: str = Field(description="One or two sentences justifying the score.")


def _judge(system: str, human: str) -> JudgeVerdict:
    model = make_chat_model(GRADER_MODEL, temperature=0).with_structured_output(JudgeVerdict)
    return model.invoke([SystemMessage(system), HumanMessage(human)])


_RELEVANCE_SYSTEM = (
    "You judge whether an assistant's answer about a codebase is grounded and relevant. "
    "Score 1.0 when the answer is accurate, on-topic, and cites specific files; lower it "
    "when it is vague, ungrounded, or contradicts the reference. Judge substance, not wording."
)

_REVIEW_SYSTEM = (
    "You judge whether a PR review is useful. Score 1.0 when it surfaces the expected class "
    "of problem with actionable, specific findings; lower it when it misses the expected issue "
    "or is generic/noisy."
)

_PLAN_SYSTEM = (
    "You judge whether an automated fix plan is correct. Score 1.0 when the planned files "
    "clearly overlap the expected files and the approach would plausibly resolve the issue; "
    "lower it for wrong/missing files or an implausible approach."
)


def retrieval_relevance(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Groundedness / retrieval-relevance of a chat answer vs. the reference."""
    verdict = _judge(
        _RELEVANCE_SYSTEM,
        f"Question:\n{inputs.get('question', '')}\n\n"
        f"Assistant answer:\n{outputs.get('answer', '')}\n\n"
        f"Reference answer:\n{reference_outputs.get('reference', '')}",
    )
    return {"key": "retrieval_relevance", "score": verdict.score, "comment": verdict.reasoning}


def review_usefulness(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Usefulness of a PR review vs. the expected class of problem."""
    findings = outputs.get("findings", [])
    rendered = "\n".join(f"- {f.get('file')}:{f.get('line')} [{f.get('severity')}] {f.get('comment')}" for f in findings)
    verdict = _judge(
        _REVIEW_SYSTEM,
        f"Expected problem:\n{reference_outputs.get('expected', '')}\n\n"
        f"Review findings:\n{rendered or '(no findings)'}",
    )
    return {"key": "review_usefulness", "score": verdict.score, "comment": verdict.reasoning}


def plan_correctness(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Correctness of an auto-PR / issue plan vs. the expected target files."""
    plan = outputs.get("plan") or {}
    files = ", ".join(f.get("path", "") for f in plan.get("files", [])) or outputs.get("files", "")
    verdict = _judge(
        _PLAN_SYSTEM,
        f"Expected files: {reference_outputs.get('expected_files', [])}\n\n"
        f"Planned summary: {plan.get('summary', '')}\n"
        f"Planned approach: {plan.get('approach', '')}\n"
        f"Planned files: {files}",
    )
    return {"key": "plan_correctness", "score": verdict.score, "comment": verdict.reasoning}

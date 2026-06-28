import os

from app.config import settings


def configure_langsmith() -> None:
    """Export LangSmith settings into os.environ so LangChain/LangGraph auto-trace
    every graph run — the SDK reads os.environ directly, but pydantic-settings only
    loads .env into the Settings object, so without this bridge tracing never turns
    on when the process isn't launched with the vars already exported. No-op unless
    LANGSMITH_TRACING is set."""
    if not settings.langsmith_tracing:
        return
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": settings.langsmith_api_key,
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
    }
    os.environ.update({k: v for k, v in env.items() if v})

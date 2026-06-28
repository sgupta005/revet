from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str

    openai_api_key: str
    llm_model: str = "openai:gpt-4o"
    embedding_model: str = "text-embedding-3-small"

    github_app_id: str
    github_app_private_key: str  # PEM with literal \n
    github_webhook_secret: str

    # Phase 5 — user-to-server OAuth + sessions (frontend auth). The OAuth secret
    # is backend-only and never exposed; the browser holds only an opaque session.
    github_oauth_client_id: str
    github_oauth_client_secret: str
    frontend_origin: str = "http://localhost:3000"
    session_ttl: int = 7 * 24 * 3600  # session lifetime in seconds

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "revet"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

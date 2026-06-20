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

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "revet"

    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

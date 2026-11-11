from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Agent config from env vars / .env. See .env.example for every key."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # ServiceNow (the mock locally; a real instance would be a URL + creds swap)
    snow_base_url: str = "http://127.0.0.1:8080"
    snow_username: str = "triage.agent"
    snow_password: SecretStr = SecretStr("change-me")
    snow_token: SecretStr | None = None
    snow_timeout_s: float = 10.0
    snow_max_retries: int = 3
    snow_backoff_base_s: float = 0.5

    # LLM: provider-agnostic; Gemini by default, Azure OpenAI as a config swap
    llm_provider: Literal["gemini", "azure_openai"] = "gemini"
    llm_model: str = ""  # Gemini model ID (Azure uses the deployment name instead)
    llm_temperature: float | None = None  # None = the provider/library default
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2  # SDK-level retries on 429/5xx
    gemini_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str | None = None
    azure_openai_deployment: str | None = None

    # Local state
    checkpoint_db: str = "data/checkpoints.db"
    run_log: str = "runs/runs.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    """Mock server config. Reads env vars / .env (SNOW_USERNAME, SNOW_MOCK_DB, ...)."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    snow_username: str = "triage.agent"
    snow_password: SecretStr = SecretStr("change-me")
    snow_token: SecretStr | None = None

    snow_mock_db: str = "data/snow.db"
    snow_mock_admin: bool = True
    snow_mock_fault_rate: float = Field(default=0.0, ge=0.0, le=1.0)

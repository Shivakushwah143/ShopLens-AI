from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="General Browser AI Agent", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    database_url: str = Field(default="sqlite+aiosqlite:///./agent.db", alias="DATABASE_URL")
    llm_provider: str = Field(
        default="gemini",
        validation_alias=AliasChoices("LLM_PROVIDER"),
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "GEMINI_API_KEY"),
    )
    llm_model_planner: str = Field(
        default="gemini-3.1-flash",
        validation_alias=AliasChoices("LLM_MODEL_PLANNER", "GEMINI_MODEL_PLANNER"),
    )
    llm_model_worker: str = Field(
        default="gemini-3.1-flash",
        validation_alias=AliasChoices("LLM_MODEL_WORKER", "GEMINI_MODEL_WORKER"),
    )
    llm_model_final: str = Field(
        default="gemini-3.1-pro",
        validation_alias=AliasChoices("LLM_MODEL_FINAL", "GEMINI_MODEL_FINAL"),
    )
    gemini_api_base: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        validation_alias=AliasChoices("GEMINI_API_BASE"),
    )
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_timeout_ms: int = Field(default=30000, alias="BROWSER_TIMEOUT_MS")
    max_web_sources: int = Field(default=8, alias="MAX_WEB_SOURCES")
    max_video_results: int = Field(default=5, alias="MAX_VIDEO_RESULTS")
    screenshot_dir: Path = Field(default=Path("artifacts/screenshots"), alias="SCREENSHOT_DIR")
    allow_live_browser: bool = Field(default=True, alias="ALLOW_LIVE_BROWSER")

    # Optional: Official Search API keys (set in .env for better search reliability)
    bing_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BING_API_KEY", "BING_SEARCH_KEY"),
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    return settings

"""Runtime configuration loaded from .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    atlassian_email: str = Field(...)
    atlassian_api_token: SecretStr = Field(...)
    jira_cloud_id: str = Field(default="00000000-0000-0000-0000-000000000000")
    # The account may be an agent on many desks; everything is hard-scoped to this project.
    jira_project: str = Field(default="HELP")
    anthropic_api_key: SecretStr | None = Field(default=None)

    draft_model: str = Field(default="claude-haiku-5-5")
    compare_model: str = Field(default="claude-sonnet-5-5")
    # Optional: pseudonymize PII before each model call (see scrub.py).
    scrub_pii_before_llm: bool = Field(default=False)

    corpus_db_path: Path = Field(default=Path("./.autodrafter/corpus.db"))
    # KB articles: a clone of kb-repo wins; otherwise the snapshot bundled in kb/issues.
    kb_issues_dir: Path = Field(default_factory=lambda: _default_kb_dir())
    out_dir: Path = Field(default=Path("./out"))
    log_level: str = Field(default="INFO")
    http_timeout_seconds: float = Field(default=30.0)
    http_max_retries: int = Field(default=5)

    @property
    def jira_base_url(self) -> str:
        return f"https://api.atlassian.com/ex/jira/{self.jira_cloud_id}/rest/api/3"

    @property
    def servicedesk_base_url(self) -> str:
        return f"https://api.atlassian.com/ex/jira/{self.jira_cloud_id}/rest/servicedeskapi"

    def basic_auth(self) -> tuple[str, str]:
        return (self.atlassian_email, self.atlassian_api_token.get_secret_value())


KB_DIR_CANDIDATES = (Path("./kb-repo/content/issues"), Path("./kb/issues"))


def _default_kb_dir() -> Path:
    return next((p for p in KB_DIR_CANDIDATES if p.exists()), KB_DIR_CANDIDATES[0])


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

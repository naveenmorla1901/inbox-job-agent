from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_client_secrets: str = "secrets/client_secret.json"
    gmail_token_file: str = "secrets/token.json"
    gmail_token_json: str = ""
    gmail_query: str = "in:inbox -category:promotions"
    gmail_initial_lookback_days: int = 3
    gmail_max_results: int = 60
    gmail_apply_label: bool = False
    gmail_label_name: str = "JobAgent"
    # projects/PROJECT_ID/topics/gmail-inbox — enables Gmail push on new mail
    gmail_pubsub_topic: str = ""

    database_url: str = "sqlite:///data/jobagent.db"

    profile_path: str = "config/profile.yaml"
    profile_yaml: str = ""  # inline profile for hosts where you only have env vars
    min_job_score: float = 0.45
    max_jobs_per_email: int = 40
    scrape_job_pages: bool = True
    scrape_timeout: int = 15

    # Ordered "provider:model" chains. The first one that answers wins, so put the
    # cheapest model that can do the job first and keep the big ones as backup.
    llm_chain: str = ""
    llm_chain_classify: str = ""  # high volume, short prompts: use a small fast model
    llm_chain_extract: str = ""  # rare, long prompts: worth a stronger model
    llm_cooldown_seconds: int = 900  # how long to skip a provider after it rate limits
    llm_gemini_gap_seconds: int = 8  # rest a Gemini key after a hit so the other account is used next
    llm_classify_body_chars: int = 4000

    llm_provider: str = "none"  # legacy single-provider setting, still honoured
    gemini_api_key: str = ""
    gemini_api_key_2: str = ""  # second Google AI Studio account; rotated with gemini_api_key
    gemini_model: str = "gemini-2.0-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    deepseek_api_key: str = ""
    nvidia_api_key: str = ""
    openrouter_api_key: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_on_jobs: bool = True
    notify_on_outreach: bool = True

    api_token: str = "change-me"
    port: int = 8000

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p


class Skill(BaseModel):
    name: str
    weight: float = 1.0
    aliases: list[str] = Field(default_factory=list)

    def terms(self) -> list[str]:
        return [self.name.lower(), *(a.lower() for a in self.aliases)]


class Profile(BaseModel):
    name: str = ""
    target_titles: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)
    exclude_titles: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    max_years_experience: int = 5
    resume_text: str = ""
    job_alert_senders: list[str] = Field(default_factory=list)

    def skill_weight_total(self) -> float:
        return sum(s.weight for s in self.skills) or 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_profile() -> Profile:
    settings = get_settings()
    if settings.profile_yaml.strip():
        return Profile(**(yaml.safe_load(settings.profile_yaml) or {}))

    path = settings.path(settings.profile_path)
    if not path.exists():
        example = path.with_name("profile.example.yaml")
        if not example.exists():
            raise FileNotFoundError(f"No profile at {path} and no example to fall back to")
        path = example
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile(**data)

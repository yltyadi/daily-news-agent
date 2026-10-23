from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class ScheduleConfig(BaseModel):
    timezone: str = "Asia/Almaty"
    daily_time: str = "21:00"
    monthly_day: int = 1
    monthly_time: str = "09:00"

    def to_utc_crons(self) -> tuple[str, str]:
        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)

        d_hour, d_min = [int(x) for x in self.daily_time.strip().split(":")]
        daily_local = now.replace(hour=d_hour, minute=d_min, second=0, microsecond=0)
        daily_utc = daily_local.astimezone(timezone.utc)
        daily_cron = f"{daily_utc.minute} {daily_utc.hour} * * *"

        m_hour, m_min = [int(x) for x in self.monthly_time.strip().split(":")]
        monthly_local = now.replace(day=1, hour=m_hour, minute=m_min, second=0, microsecond=0)
        monthly_utc = monthly_local.astimezone(timezone.utc)
        monthly_cron = f"{monthly_utc.minute} {monthly_utc.hour} {self.monthly_day} * *"

        return daily_cron, monthly_cron


class PricingConfig(BaseModel):
    input_usd: float = 0.75
    output_usd: float = 3.75
    cached_input_usd: float = 0.075


class LLMConfig(BaseModel):
    router_model: str = "gemini-3.1-flash-lite"
    router_thinking_level: str = "minimal"
    router_pricing_per_1m_tokens: PricingConfig = Field(
        default_factory=lambda: PricingConfig(input_usd=0.25, output_usd=1.50, cached_input_usd=0.025)
    )

    model: str = "gemini-3.8-flash"
    thinking_level: str = "low"
    temperature: float = 0.2
    batch_size_items: int = 40
    max_concurrent_llm_calls: int = 5
    pricing_per_1m_tokens: PricingConfig = Field(default_factory=PricingConfig)


class TelegramConfig(BaseModel):
    enabled: bool = True
    read_nested_articles: bool = True
    channels: list[str] = Field(default_factory=list)


class GmailConfig(BaseModel):
    enabled: bool = True
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    folder: str = "INBOX"
    allowed_senders: list[str] = Field(default_factory=list)


class DeliveryConfig(BaseModel):
    publish_telegraph: bool = True
    send_telegram_bot: bool = True
    send_email_digest: bool = True
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465


class CategoryConfig(BaseModel):
    slug: str
    title: str
    description: str
    skill_file: str

    def load_skill_text(self) -> str:
        path = ROOT_DIR / self.skill_file
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return f"Категория: {self.title}\nОписание: {self.description}"


class GeneralConfig(BaseModel):
    lookback_hours: int = 24
    monthly_lookback_days: int = 32
    language: str = "ru"
    save_local_markdown: bool = True
    daily_digests_dir: str = "data/daily_digests"


def _clean_env_secret(key: str, placeholders: tuple[str, ...] = ()) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        return ""
    low = val.lower()
    for ph in placeholders:
        if ph in low:
            return ""
    return val


class AppConfig(BaseModel):
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    categories: list[CategoryConfig] = Field(default_factory=list)
    cross_links_skill_file: str = "skills/99_cross_links.md"
    monthly_skill_file: str = "skills/100_monthly_summary.md"
    usage_history_file: str = "data/usage_history.json"

    # Секреты из переменных окружения (игнорируют шаблонные значения из .env.example)
    gemini_api_key: str = Field(
        default_factory=lambda: _clean_env_secret("GEMINI_API_KEY", ("your_gemini_api_key",))
    )
    gmail_user: str = Field(
        default_factory=lambda: _clean_env_secret("GMAIL_USER", ("your_email@", "example.com"))
    )
    gmail_app_password: str = Field(
        default_factory=lambda: _clean_env_secret("GMAIL_APP_PASSWORD", ("xxxx", "your_app_password"))
    )
    recipient_email: str = Field(
        default_factory=lambda: (
            _clean_env_secret("RECIPIENT_EMAIL", ("your_email@", "example.com"))
            or _clean_env_secret("GMAIL_USER", ("your_email@", "example.com"))
        )
    )
    tg_bot_token: str = Field(
        default_factory=lambda: _clean_env_secret("TG_BOT_TOKEN", ("your_bot_token", "1234567890:"))
    )
    tg_chat_id: str = Field(
        default_factory=lambda: _clean_env_secret("TG_CHAT_ID", ("123456789", "your_chat_id"))
    )
    telegraph_access_token: str = Field(default_factory=lambda: os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip())

    def load_cross_links_skill(self) -> str:
        path = ROOT_DIR / self.cross_links_skill_file
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return "Найди связи между категориями новостей за день."

    def load_monthly_skill(self) -> str:
        path = ROOT_DIR / self.monthly_skill_file
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return "Подведи подробные итоги месяца по всем категориям, выдели главные тренды и инсайты."


def load_config(config_path: Path | str | None = None) -> AppConfig:
    path = Path(config_path) if config_path else (ROOT_DIR / "config.yaml")
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig(**raw)

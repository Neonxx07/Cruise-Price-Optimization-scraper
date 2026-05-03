"""CruiseIntel Intelligence System — Configuration"""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    # ── App ─────────────────────────────────────────────────────
    app_name: str = "Cruise Intelligence System"
    app_version: str = "1.0.0"
    debug: bool = False

    # ── API ─────────────────────────────────────────────────────
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ── Database ────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./cruise_intel.db"

    # ── Scraper ─────────────────────────────────────────────────
    # Path to Chrome/Edge user data dir for authenticated sessions
    browser_user_data_dir: str = ""
    browser_headless: bool = True
    scraper_timeout_ms: int = 30000
    scraper_retry_attempts: int = 3
    scraper_retry_delay_ms: int = 3000

    # ── Proxy (design-ready, not required) ──────────────────────
    proxy_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""

    # ── Cache ───────────────────────────────────────────────────
    cache_ttl_hours: int = 12

    # ── Scheduler ───────────────────────────────────────────────
    scheduler_enabled: bool = False
    scheduler_interval_minutes: int = 60

    # ── URLs ────────────────────────────────────────────────────
    espresso_base_url: str = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
    ncl_search_url: str = "https://seawebagents.ncl.com/tva/search/"

    # ── Logging ─────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton
settings = Settings()

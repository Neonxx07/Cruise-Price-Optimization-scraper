"""CruiseIntel Intelligence System — Configuration"""

from pydantic_settings import BaseSettings
from pathlib import Path

# Where the saved login session (cookies + localStorage, via Playwright's
# storage_state) is written — logging in once here (via "Check login" /
# `main.py login`) keeps that session on disk, so scans and later app
# restarts reuse it instead of starting a fresh, logged-out browser every
# time. Lives next to the app, not in the repo (see .gitignore) since it
# holds real session cookies.
_DEFAULT_BROWSER_PROFILE_DIR = str(Path(__file__).resolve().parent.parent / "browser-profile")


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
    # Directory holding the saved login session (storage_state.json).
    # Non-empty by default so login persists across scans and app restarts —
    # set to "" (e.g. in .env) to force a fresh incognito-style session every run.
    browser_user_data_dir: str = _DEFAULT_BROWSER_PROFILE_DIR
    browser_headless: bool = True
    scraper_timeout_ms: int = 30000
    scraper_retry_attempts: int = 3
    scraper_retry_delay_ms: int = 3000
    # ESPRESSO's SSO login can bounce through an OAuth redirect chain
    # (login -> auth.cruisingpower.com -> oauth callback -> reservations.do)
    # that can take longer than the generic action timeout above.
    scraper_login_timeout_ms: int = 60000
    # How long to poll for the price category to finish client-side
    # template rendering before giving up (see espresso._read_category).
    scraper_category_poll_timeout_ms: int = 8000
    # Randomized delay between bookings in a batch — a real agent doesn't
    # click through bookings every 0.5s, and hitting the portal that fast
    # back-to-back appears to be what degrades sessions/tokens mid-batch.
    scraper_interbooking_delay_min_s: float = 4.0
    scraper_interbooking_delay_max_s: float = 9.0
    # If this many bookings in a row fail, pause for a longer cooldown
    # before continuing — a burst of failures usually means the portal
    # session/token state needs time to recover, not instant retries.
    scraper_cooldown_after_failures: int = 3
    scraper_cooldown_seconds: float = 120.0

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
    espresso_home_url: str = "https://secure.cruisingpower.com/home"
    espresso_base_url: str = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
    ncl_search_url: str = "https://seawebagents.ncl.com/tva/search/"
    goccl_search_url: str = "https://www.goccl.com/BookingEngine/BookingSearch/SearchForReservations.aspx"

    # ── GoCCL ───────────────────────────────────────────────────
    # Guests count matters: GoCCL's offer-code comparison table shows
    # "Average Per Person," but the review screen's GROSS AMOUNT is the
    # full per-cabin total — see scraper/goccl.py for the comparison math.
    goccl_default_guests_count: int = 2

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

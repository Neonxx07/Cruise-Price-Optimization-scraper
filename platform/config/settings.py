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
    # CONFIRMED REAL RISK, fixed 2026-08-13: previously hardcoded as
    # allow_origins=["*"] + allow_credentials=True in api/main.py — a
    # well-known dangerous CORS combination (browsers actually forbid a
    # literal wildcard with credentials, so servers/frameworks fall back
    # to reflecting whatever Origin the request sent, which means ANY
    # website open in the user's browser could make a credentialed
    # request to this API and read the response — a real risk for a
    # localhost-bound service, since browser-based cross-origin requests
    # to 127.0.0.1 are not blocked by same-origin policy on the
    # requesting page). Confirmed via investigation that NOTHING in this
    # project (GUI, CLI, extension) actually calls this API cross-origin
    # today — the extension talks directly to cruise-line portals, the
    # GUI/CLI call BookingService in-process, never over HTTP — so the
    # safe default is NO allowed cross-origin access at all. Empty list
    # by default; set this explicitly (e.g. via .env) only if a real
    # browser-based frontend is ever built against this API.
    cors_allowed_origins: list[str] = []

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

    # ── URLs ────────────────────────────────────────────────────
    espresso_home_url: str = "https://secure.cruisingpower.com/home"
    espresso_base_url: str = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
    ncl_search_url: str = "https://seawebagents.ncl.com/tva/search/"
    goccl_search_url: str = "https://www.goccl.com/BookingEngine/BookingSearch/SearchForReservations.aspx"
    msc_home_url: str = "https://www.mscbook.com/us/home"
    # Param order here must stay byte-identical to what msc_commands.py's
    # three call sites have actually been sending (confirmed 2026-08-11
    # during config consolidation — this field previously had
    # tryToRetrive/bookingID swapped relative to production; fixed to
    # match the proven-working value rather than the other way around).
    msc_booking_search_url: str = (
        "https://www.mscbook.com/shop/BookingSearchView"
        "?storeId=10757&langId=-1004&marketCode=USA&catalogId=10001"
        "&fastSearchError=false&bookingID={booking_id}&tryToRetrive=true"
    )

    # ── MSC ─────────────────────────────────────────────────────
    # CLEANED UP 2026-08-13: this section used to include
    # `msc_confirm_wait_timeout_s` (how long check_booking() would wait
    # for a human to manually click "CONFIRM AND PROCEED" — that click
    # was blocked from automated use by Claude Code's own safety
    # classifier, confirmed 2026-08-10). RESOLVED 2026-08-11: Neon
    # added a narrowly-scoped autoMode.allow permission specifically for
    # that click, so msc_commands.py now clicks it directly — no more
    # human-click wait, and the setting had zero remaining references
    # anywhere in the code (confirmed via grep). This logic lives in the
    # root-level msc_commands.py/msc_session_controller.py — a
    # standalone scraper/msc.py was never built.
    # Windows Credential Manager service name used by
    # msc_save_credentials.py / msc_clear_credentials.py — must match.
    msc_credential_service: str = "msc_book_login"

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

"""Base scraper with Playwright browser management, retry, and proxy support.

All cruise line scrapers inherit from BaseScraper.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings
from core.models import BookingResult, CruiseLine
from utils.logging import get_logger

logger = get_logger(__name__)


class BaseScraper(ABC):
    """
    Abstract base for all cruise line scrapers.

    Manages a Playwright browser instance with:
    - Headless/headed mode
    - User data dir for authenticated sessions
    - Proxy support (design-ready)
    - Automatic cleanup
    """

    cruise_line: CruiseLine

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> None:
        """Launch the browser and create a page."""
        self._playwright = await async_playwright().start()

        launch_args: dict = {
            "headless": settings.browser_headless,
        }

        # Proxy support (design-ready)
        if settings.proxy_url:
            launch_args["proxy"] = {
                "server": settings.proxy_url,
            }
            if settings.proxy_username:
                launch_args["proxy"]["username"] = settings.proxy_username
                launch_args["proxy"]["password"] = settings.proxy_password

        # Use persistent context if user data dir is set (for auth sessions)
        if settings.browser_user_data_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=settings.browser_user_data_dir,
                **launch_args,
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        else:
            self._browser = await self._playwright.chromium.launch(**launch_args)
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()

        self._page.set_default_timeout(settings.scraper_timeout_ms)
        logger.info("browser.started", cruise_line=self.cruise_line.value, headless=settings.browser_headless)

    async def stop(self) -> None:
        """Close browser and cleanup resources."""
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("browser.cleanup_error", error=str(e))
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            logger.info("browser.stopped", cruise_line=self.cruise_line.value)

    @property
    def page(self) -> Page:
        """Get the active page, raising if not started."""
        if self._page is None:
            raise RuntimeError("Scraper not started — call start() first")
        return self._page

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL and wait for load."""
        logger.debug("navigate", url=url)
        await self.page.goto(url, wait_until=wait_until)

    async def wait_for(self, selector: str, timeout: int | None = None) -> None:
        """Wait for an element to appear on page."""
        t = timeout or settings.scraper_timeout_ms
        await self.page.wait_for_selector(selector, timeout=t)

    async def evaluate(self, expression: str):
        """Run JavaScript in the page context."""
        return await self.page.evaluate(expression)

    async def fill_and_submit(self, selector: str, value: str, submit_selector: str) -> None:
        """Fill an input and click submit."""
        await self.page.fill(selector, value)
        await self.page.click(submit_selector)

    @abstractmethod
    async def check_booking(self, booking_id: str) -> BookingResult:
        """Check a single booking for optimization opportunities."""
        ...

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

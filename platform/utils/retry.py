"""Retry with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


async def retry_async(
    fn: Callable[..., T],
    *args,
    attempts: int = 3,
    delay_s: float = 3.0,
    backoff: float = 1.5,
    label: str = "",
) -> T:
    """
    Retry an async function with exponential backoff.

    Args:
        fn: Async callable to retry.
        attempts: Maximum number of attempts.
        delay_s: Initial delay between retries in seconds.
        backoff: Multiplier for delay after each failure.
        label: Label for log messages.

    Returns:
        The result of fn(*args).

    Raises:
        The last exception if all attempts fail.
    """
    last_error: Exception | None = None
    current_delay = delay_s

    for attempt in range(attempts):
        try:
            return await fn(*args)
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                logger.warning(
                    "[%s] Attempt %d/%d failed: %s — retrying in %.1fs",
                    label, attempt + 1, attempts, str(e), current_delay,
                )
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(
                    "[%s] All %d attempts failed. Last error: %s",
                    label, attempts, str(e),
                )

    raise last_error  # type: ignore[misc]

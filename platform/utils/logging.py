"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """
    Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional file path to write logs to.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Console output: human-readable
    console_processor = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Root logger
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=console_processor,
        foreign_pre_chain=processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Optional file handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=processors,
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


def track_background_task(task_set: set, task) -> None:
    """Retain a strong reference to a fire-and-forget `asyncio.Task`
    until it completes, and log (rather than silently lose) any
    exception it raises.

    CONFIRMED REAL RISK, fixed 2026-08-13: `asyncio.create_task(...)`
    calls whose return value is discarded rely on the event loop's own
    reference to keep the task alive — but per the asyncio docs, that
    reference is effectively weak from the caller's perspective; a task
    with no OTHER strong reference is a real candidate for the "Task
    was destroyed but it is pending!" failure mode, and even when it
    does run to completion, any exception it raised is reported
    nowhere. Several `asyncio.create_task(...)` call sites across this
    project (the background scan runner, MSC/ESPRESSO network-response
    capture listeners) had no reference retained at all.

    Usage: keep one `set()` per owner (e.g. `self._background_tasks`),
    call this right after `create_task(...)`, and nothing else — the
    task removes itself from the set automatically on completion via
    its own done-callback. Not a global registry (each owner keeps its
    own set, scoped to its own lifetime) — deliberately not "store every
    task forever," just "don't let THIS task disappear while it's still
    doing real work."""
    task_set.add(task)

    def _on_done(t):
        task_set.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            get_logger("background_task").error(
                "background_task.unhandled_exception", task=str(t), error=str(exc), exc_info=exc,
            )

    task.add_done_callback(_on_done)

"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import pathlib
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
        # ROTATING, not plain FileHandler. Added 2026-09-21: nothing was
        # ever written to disk (every caller passed no log_file), so a
        # failure mid-scan scrolled past in the GUI and was gone - which is
        # why the recurring ESPRESSO login/logout faults kept having to be
        # re-diagnosed from scratch. A plain FileHandler would have swapped
        # that for a different problem: this project's data directory is
        # already 3.1 GB, and an unbounded JSON log of every scan would just
        # add to it. 10 MB x 5 keeps roughly the last few full runs.
        from logging.handlers import RotatingFileHandler

        pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=processors,
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


# ── crashes ──────────────────────────────────────────────────────────────
#
# THE INCIDENT, 2026-09-22/23. A RuntimeError killed a running 721-booking
# ESPRESSO scan overnight:
#
#     RuntimeError: Cannot enter into task Task-17 <_on_start at :961>
#     while another task Task-546 <_on_start at :898> is being executed
#     Task was destroyed but it is pending!
#
# Counted afterwards:
#
#     "Cannot enter into task" in terminal stdout   : 4
#     "Cannot enter into task" in data/cruiseintel.log: 0
#
# gui.main._handle_async_exception did `print(...)` plus
# traceback.print_exception(...), both to stdout. Nothing reached the log,
# so scan_watchdog - which parses JSON lines - could not see the one error
# that mattered, and the dead scan sat unnoticed from 01:19 until 14:14.
#
# Everything downstream depends on this: a monitor cannot alert on an error
# that was never recorded. track_background_task above already got this
# right (`exc_info=exc`); these are the paths that did not.

CRASH_EVENT = "crash.unhandled"


def log_crash(source: str, exc: BaseException | None, **context) -> None:
    """Record an unhandled exception as a structured, greppable log line.

    `exc_info` is what carries the traceback - structlog's format_exc_info
    processor (see setup_logging) renders it into the JSON record, so the
    stack survives to disk instead of scrolling past in a terminal.

    NEVER RAISES. This runs from excepthooks and asyncio error handlers,
    where raising would replace a diagnosable failure with an undiagnosable
    one.
    """
    try:
        get_logger("crash").error(
            CRASH_EVENT,
            source=source,
            error=str(exc) if exc is not None else None,
            error_type=type(exc).__name__ if exc is not None else None,
            exc_info=exc,
            **context,
        )
    except Exception:  # noqa: BLE001 - a crash logger must not crash
        try:
            print(f"CRASH ({source}): {exc!r}", file=sys.stderr)
        except Exception:
            pass


def install_crash_handlers(source_prefix: str = "app") -> None:
    """Send otherwise-lost exceptions to the log.

    Covers the two hooks that are silent by default:
      - sys.excepthook        uncaught exception on the main thread
      - threading.excepthook  uncaught exception in any other thread

    The asyncio path is separate because it needs a loop - callers pass
    :func:`asyncio_exception_handler` to ``loop.set_exception_handler``.

    Chains to the previous hook so nothing that already worked stops
    working (the interpreter still prints to stderr as well).
    """
    import threading

    previous_excepthook = sys.excepthook

    def _hook(exc_type, exc, tb) -> None:
        log_crash(f"{source_prefix}.main_thread", exc)
        previous_excepthook(exc_type, exc, tb)

    sys.excepthook = _hook

    previous_thread_hook = threading.excepthook

    def _thread_hook(args) -> None:
        # A thread dying silently is how a keepalive or a watchdog timer
        # stops running without anything appearing to be wrong.
        log_crash(f"{source_prefix}.thread", args.exc_value,
                  thread=getattr(args.thread, "name", None))
        previous_thread_hook(args)

    threading.excepthook = _thread_hook


def asyncio_exception_handler(loop, context: dict) -> None:
    """For ``loop.set_exception_handler``. Logs, then falls back to default.

    The context dict carries more than the exception - the failing handle,
    task, or future - and that is what identified the re-entrant _on_start
    above, so it is recorded rather than dropped.
    """
    exc = context.get("exception")
    log_crash(
        "asyncio",
        exc,
        message=str(context.get("message") or ""),
        task=str(context.get("task") or context.get("handle") or "") or None,
    )
    try:
        loop.default_exception_handler(context)
    except Exception:  # noqa: BLE001
        pass


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

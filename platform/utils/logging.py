"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import pathlib
import sys
import time as _time

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

    _make_logging_non_blocking(root)


#: The background listener that owns the real handlers. Module-level so a
#: second setup_logging() call replaces it instead of leaking a thread.
_log_listener = None


def _make_logging_non_blocking(root: logging.Logger) -> None:
    """Move all log I/O off the calling thread.

    THE HANG, 2026-09-30. py-spy on a frozen GUI:

        emit (logging\\__init__.py:1154)
        _proxy_to_logger (structlog\\_base.py:224)
        _attempt (scraper\\espresso.py:1676)
        _run_batch -> timerEvent (qasync)

    The UI thread was blocked inside `Handler.emit`, 0% CPU, window
    reported HUNG by Windows.

    The cause is the same one that froze it on 2026-09-28 through a
    `print()`: the app is launched from a .bat, so it owns a CONSOLE, and
    Windows QuickEdit PAUSES console output the moment anyone clicks or
    selects in that window. A paused console blocks whoever writes to it.
    Removing the eight prints did not fix it, because EVERY `logger.info()`
    also writes to the console handler - hundreds of times per scan instead
    of eight times per session. The earlier fix treated the symptom.

    A QueueHandler makes the caller's job a queue append, which cannot
    block; a QueueListener thread owns the real handlers and does the I/O.
    If the console is paused, that background thread waits - and nothing
    else notices.

    Also covers the FILE handler: a rotating write on a slow or locked disk
    would stall the UI thread just as effectively.
    """
    global _log_listener
    import queue as _queue
    from logging.handlers import QueueHandler, QueueListener

    class _PassthroughQueueHandler(QueueHandler):
        """Enqueue the record UNCHANGED.

        The stdlib QueueHandler.prepare() formats the record and replaces
        `record.msg` with the resulting STRING (so the record can cross a
        process boundary). That is wrong here: structlog puts a DICT in
        `record.msg`, and the listener's ProcessorFormatter then does
        `record.msg.copy()` and raises

            AttributeError: 'str' object has no attribute 'copy'

        ...on every single record, silently, on a background thread. The
        first version of this fix produced a ZERO-BYTE log file - trading a
        frozen window for no evidence at all, which is the worse bargain.

        The queue is in-process, so there is nothing to serialise for and
        the raw record is exactly what the real handlers want.
        """

        def prepare(self, record):
            return record

    if _log_listener is not None:
        try:
            _log_listener.stop()
        except Exception:
            pass
        _log_listener = None

    real_handlers = list(root.handlers)
    if not real_handlers:
        return
    # Unbounded: dropping log records to protect the UI would lose exactly
    # the evidence needed after an incident, and the listener drains far
    # faster than any scan produces records.
    record_queue: _queue.Queue = _queue.Queue(-1)
    root.handlers.clear()
    root.addHandler(_PassthroughQueueHandler(record_queue))
    _log_listener = QueueListener(record_queue, *real_handlers,
                                  respect_handler_level=True)
    _log_listener.daemon = True
    _log_listener.start()

    import atexit
    atexit.register(_stop_log_listener)


def flush_logs(timeout: float = 5.0) -> bool:
    """Wait until everything logged so far has actually been written.

    Logging is asynchronous now (see _make_logging_non_blocking), so the
    file lags the call by a few milliseconds. That is invisible in normal
    use and matters in exactly two places: a test that logs then reads the
    file, and any code that wants the evidence on disk before doing
    something drastic.

    Returns True if the queue drained within the timeout. Never raises -
    a flush that cannot complete must not become the failure.
    """
    listener = _log_listener
    if listener is None:
        return True
    try:
        queue = listener.queue
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if queue.empty():
                # The listener may still be inside emit() for the last
                # record; give it a moment to finish rather than racing it.
                _time.sleep(0.02)
                if queue.empty():
                    return True
            _time.sleep(0.01)
        return False
    except Exception:
        return False


def _stop_log_listener() -> None:
    """Flush and stop the listener. Safe to call twice."""
    global _log_listener
    listener, _log_listener = _log_listener, None
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass


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

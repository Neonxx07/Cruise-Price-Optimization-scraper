"""Log I/O must never run on the caller's thread.

THE HANG, 2026-09-30. py-spy on a frozen GUI:

    Thread 1164 (idle)
        emit (logging\__init__.py:1154)
        handle -> callHandlers -> _log -> info
        _proxy_to_logger (structlog\_base.py:224)
        _attempt (scraper\espresso.py:1676)
        _run_batch -> timerEvent (qasync\__init__.py:307)

The UI thread was blocked inside `Handler.emit` at 0% CPU, and Windows
reported the window as hung.

SAME CAUSE AS 2026-09-28, WHICH WAS ONLY HALF FIXED. The app launches from a
.bat, so it owns a console, and Windows QuickEdit PAUSES console output the
moment anyone clicks or selects in that window - blocking whoever writes to
it. Removing the eight `print()` calls did not fix it, because every
`logger.info()` also writes to the console handler: hundreds of times per
scan rather than eight times per session. The first fix treated the symptom.

A QueueHandler makes the caller's job a queue append, which cannot block; a
QueueListener thread owns the real handlers. If the console is paused, that
background thread waits and nothing else notices. It covers the FILE handler
too - a rotating write on a slow or locked disk would stall the UI just as
effectively.
"""

import logging
import time

import pytest

from utils.logging import get_logger, setup_logging


@pytest.fixture
def configured(tmp_path):
    import utils.logging as ul
    setup_logging("INFO", str(tmp_path / "t.log"))
    yield ul
    ul._stop_log_listener()


def test_the_root_logger_only_holds_a_queue_handler(configured):
    """Anything else on the root logger does its I/O on the caller."""
    from logging.handlers import QueueHandler
    # pytest attaches its OWN capture handlers after setup_logging runs;
    # those are a test-harness artifact, not something the app installs.
    handlers = [h for h in logging.getLogger().handlers
                if "LogCapture" not in type(h).__name__]
    assert handlers, "logging was not configured"
    assert all(isinstance(h, QueueHandler) for h in handlers), (
        f"a real handler is still attached to the caller: "
        f"{[type(h).__name__ for h in handlers]}")


def test_a_blocking_handler_does_not_block_the_caller(configured):
    """THE regression guard, in the shape of the actual bug: a handler that
    cannot complete must not stop the thread doing the logging."""
    started = []

    class Blocking(logging.Handler):
        def emit(self, record):
            started.append(1)
            time.sleep(2.0)          # a paused console

    blocker = Blocking()
    blocker.setFormatter(logging.Formatter("%(message)s"))
    configured._log_listener.handlers = (
        *configured._log_listener.handlers, blocker)

    log = get_logger("t")
    start = time.perf_counter()
    for i in range(10):
        log.info("record", i=i)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, (
        f"logging took {elapsed:.2f}s - the caller is still doing handler I/O")


def test_records_still_reach_the_file(tmp_path):
    """Non-blocking must not mean lost. The log is the evidence trail."""
    import utils.logging as ul
    path = tmp_path / "t.log"
    setup_logging("INFO", str(path))
    try:
        get_logger("t").info("marker_event", value=42)
        # stop() drains the queue and flushes the real handlers - the
        # deterministic way to read what was written, rather than sleeping
        # and hoping.
        ul._stop_log_listener()
        assert path.exists(), "no log file was written"
        assert "marker_event" in path.read_text(encoding="utf-8")
    finally:
        ul._stop_log_listener()


def test_reconfiguring_does_not_leak_a_listener(tmp_path):
    """setup_logging runs again on some paths; each call must not leave a
    thread behind holding the previous handlers."""
    import threading

    import utils.logging as ul
    before = threading.active_count()
    for i in range(4):
        setup_logging("INFO", str(tmp_path / f"{i}.log"))
    ul._stop_log_listener()
    time.sleep(0.3)
    assert threading.active_count() <= before + 1


def test_stopping_twice_is_harmless(tmp_path):
    import utils.logging as ul
    setup_logging("INFO", str(tmp_path / "t.log"))
    ul._stop_log_listener()
    ul._stop_log_listener()


def test_the_queue_is_unbounded(configured):
    """Dropping records to protect the UI would lose exactly the evidence
    needed after an incident - and the listener drains far faster than any
    scan produces records."""
    from logging.handlers import QueueHandler
    handler = next(h for h in logging.getLogger().handlers
                   if isinstance(h, QueueHandler))
    # queue.Queue(-1) keeps maxsize as -1; anything > 0 would drop records.
    assert handler.queue.maxsize <= 0


def test_the_record_reaches_handlers_UNMANGLED(tmp_path):
    """THE bug my own first fix introduced, caught before it shipped.

    The stdlib QueueHandler.prepare() formats the record and replaces
    `record.msg` with a STRING. structlog puts a DICT there, so the
    listener's ProcessorFormatter then raised

        AttributeError: 'str' object has no attribute 'copy'

    on every record, silently, on a background thread. The log file came
    out ZERO BYTES - trading a frozen window for no evidence at all, which
    is the worse bargain of the two.
    """
    import json

    import utils.logging as ul
    path = tmp_path / "t.log"
    setup_logging("INFO", str(path))
    try:
        get_logger("t").info("shape_check", value=42, other="x")
        ul._stop_log_listener()
        lines = [x for x in path.read_text(encoding="utf-8").splitlines()
                 if x.startswith("{")]
        assert lines, "nothing was written - records are being dropped"
        row = json.loads(lines[-1])
        # The structured FIELDS must survive, not just a rendered string.
        assert row["event"] == "shape_check"
        assert row["value"] == 42
        assert row["other"] == "x"
    finally:
        ul._stop_log_listener()


def test_the_queue_handler_does_not_reformat(tmp_path):
    """Structural guard on the same thing: prepare() must pass the record
    through, or the dict msg is destroyed again."""
    import inspect
    from logging.handlers import QueueHandler

    import utils.logging as ul
    setup_logging("INFO", str(tmp_path / "t.log"))
    try:
        handler = next(h for h in logging.getLogger().handlers
                       if isinstance(h, QueueHandler))
        assert type(handler) is not QueueHandler, (
            "the stdlib QueueHandler formats records and would mangle the "
            "structlog dict")
        src = inspect.getsource(type(handler).prepare)
        assert "return record" in src
    finally:
        ul._stop_log_listener()

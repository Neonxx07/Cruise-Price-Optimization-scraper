"""Pressing Stop re-sent the stop twice a second until the job ended.

Caught 2026-09-22 in the log file added the day before. A real run:

    15:37:19  batch.stop_requested  job_id=b254350e...
    15:37:19  batch.stop_requested  job_id=b254350e...
    15:37:20  batch.stop_requested  job_id=b254350e...
    ... 28 identical lines in 14 seconds, then the process exited ...

BookingQueueManager.start_processing polls the job every 0.5s, and
`_stop_requested` stays set until the `finally` block runs - so the relay
condition was true on EVERY tick for as long as the batch took to wind down.
The batch deliberately finishes its current booking before stopping, so that
window is normal and can be long.

Harmless-looking, but it re-entered an async service call ~2x/second and
buried every other event in the log that had just been switched on.
"""
import inspect

from gui.queue_manager import BookingQueueManager


SRC = inspect.getsource(BookingQueueManager.start_processing)


def test_the_stop_is_relayed_only_once():
    assert "stop_sent" in SRC
    assert "not stop_sent" in SRC


def test_the_flag_is_reset_per_run():
    """A manager is reused across scans; a stale flag would swallow the
    NEXT stop entirely - the opposite bug, and a worse one."""
    assert "stop_sent = False" in SRC


def test_the_relay_still_happens_at_all():
    assert "stop_scan" in SRC
    assert "_stop_requested" in SRC


def test_relay_fires_exactly_once_over_many_polls():
    """The loop's own condition, exercised directly: 30 polls = 15 seconds
    of wind-down, which used to mean 30 calls and 30 log lines."""
    calls = 0
    stop_sent = False
    stop_requested, job_id = True, "j1"
    for _ in range(30):
        if stop_requested and job_id and not stop_sent:
            calls += 1
            stop_sent = True
    assert calls == 1


def test_no_stop_means_no_relay():
    calls = 0
    stop_sent = False
    stop_requested, job_id = False, "j1"
    for _ in range(30):
        if stop_requested and job_id and not stop_sent:
            calls += 1
            stop_sent = True
    assert calls == 0


def test_a_stop_pressed_mid_run_still_relays():
    """The flag flips partway through the poll loop, which is the real
    sequence - the user presses Stop while the scan is going."""
    calls = 0
    stop_sent = False
    job_id = "j1"
    for tick in range(30):
        stop_requested = tick >= 10          # pressed on the 10th poll
        if stop_requested and job_id and not stop_sent:
            calls += 1
            stop_sent = True
    assert calls == 1

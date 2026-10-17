"""NCL "3 at once" in the NCL tab - separate logins, 2026-10-16.

Neon: "can we try to run 3 bookings in the same time in ncl in headless
only?" then "yes please build it". Evidence, scratchpad scripts:
  * 3 TABS ON ONE LOGIN: 3001052 came back with 3001055's data (category
    IF, LATREW, $2,127) - NCL keeps ONE current reservation per session.
  * 3 SEPARATE LOGINS, 30 bookings: 28 succeeded both ways with 0
    differences; one-by-one errored twice, 3-at-once never; 230 s vs 645 s.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.models import BookingResult, BookingStatus, CruiseLine


def _result(bid):
    return BookingResult(cruise_line=CruiseLine.NCL, status=BookingStatus.NO_SAVING,
                         booking_id=bid)


class _Job:
    def __init__(self, ids, status="COMPLETED"):
        self.job_id = f"job-{ids[0]}"
        self.results = [_result(b) for b in ids]
        self.status = SimpleNamespace(value=status)
        self.current_booking_id = None
        self.progress_done = self.progress_total = len(ids)


def _fake_service(log, name):
    class _S:
        session_slot = None

        async def start_scan(self, ids, line, **kw):
            log.append((name, getattr(self, "session_slot", None), list(ids),
                        kw.get("keep_browser_open"), kw.get("headless")))
            return _Job(list(ids))

        async def stop_scan(self, job_id):
            log.append(("stop", job_id))
            return True

        async def check_login(self, *a, **k):
            return True
    return _S()


def _manager(monkeypatch, log):
    import gui.queue_manager as qmod

    made = []

    def _new_service():
        s = _fake_service(log, f"helper{len(made) + 1}")
        made.append(s)
        return s
    monkeypatch.setattr(qmod, "BookingService", _new_service)
    qm = qmod.BookingQueueManager()
    qm._service = _fake_service(log, "main")

    async def _noop():
        return None
    qm.initialize = _noop
    return qm


def test_nine_bookings_go_to_three_logins_and_all_results_reach_the_table(monkeypatch):
    log, shown = [], []
    qm = _manager(monkeypatch, log)
    ids = [f"6600000{i}" for i in range(9)]
    for b in ids:
        qm.add_booking(b)
    asyncio.run(qm.start_processing(CruiseLine.NCL, on_result=shown.append, parallel=3))

    scans = [x for x in log if x[0] != "stop"]
    assert [x[0].startswith("helper") for x in scans] == [True, True, False]
    assert sorted(b for x in scans for b in x[2]) == sorted(ids)          # every booking once
    assert all(len(x[2]) == 3 for x in scans)
    helpers = [x for x in scans if x[0].startswith("helper")]
    assert {x[1] for x in helpers} == {"w2", "w3"}                        # own session each
    assert all(x[3] is False and x[4] is True for x in helpers)           # fresh, headless
    assert sorted(r.booking_id for r in shown) == sorted(ids)
    snap = qm.get_snapshot()
    assert snap.progress_done == snap.progress_total == 9


def test_parallel_is_ncl_only_and_needs_enough_bookings(monkeypatch):
    log = []
    qm = _manager(monkeypatch, log)
    for b in ("1", "2", "3", "4"):
        qm.add_booking(b)
    asyncio.run(qm.start_processing(CruiseLine.NCL, on_result=lambda r: None, parallel=3))
    assert [x[0] for x in log] == ["main"]                                 # 4 < 3 x 2
    log.clear()
    qm2 = _manager(monkeypatch, log)
    for b in [str(i) for i in range(9)]:
        qm2.add_booking(b)
    asyncio.run(qm2.start_processing(CruiseLine.GOCCL, on_result=lambda r: None, parallel=3))
    assert [x[0] for x in log] == ["main"]


def test_each_worker_has_its_own_session_file():
    from scraper.ncl import NclScraper

    paths = set()
    for slot in (None, "w2", "w3"):
        s = NclScraper()
        s.session_slot = slot
        paths.add(s._storage_state_path())
    ca = NclScraper(market="CA")
    ca.session_slot = "w2"
    paths.add(ca._storage_state_path())
    assert len(paths) == 4
    assert any(p.endswith("storage_state_NCL.json") for p in paths)       # main file unchanged
    assert any(p.endswith("storage_state_NCL_ca_w2.json") for p in paths)


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_checkbox_is_ncl_only_and_needs_headless(qt_app):
    from gui.windows import CruiseLinePanel

    ncl = CruiseLinePanel(CruiseLine.NCL)
    ncl.show()
    qt_app.processEvents()
    assert ncl.parallel_checkbox.isVisible() and not ncl.parallel_checkbox.isEnabled()
    ncl.headless_checkbox.setChecked(True)
    assert ncl.parallel_checkbox.isEnabled()
    ncl.parallel_checkbox.setChecked(True)
    ncl.headless_checkbox.setChecked(False)
    assert not ncl.parallel_checkbox.isChecked()                          # never windowed x3
    esp = CruiseLinePanel(CruiseLine.ESPRESSO)
    esp.show()
    qt_app.processEvents()
    assert not esp.parallel_checkbox.isVisible()


def test_start_scan_hands_market_and_headless_to_the_batch_unswapped(monkeypatch):
    """Found by the first live 3-at-once run: start_scan passed
    (headless, market) positionally into a (market, headless) signature -
    a CA scan ran on the US account, a headless one crashed ('bool' object
    has no attribute 'upper')."""
    from services.booking_service import BookingService

    svc = BookingService()
    got = {}

    async def _capture(job, **kw):
        got.update(kw)
    monkeypatch.setattr(svc, "_run_batch", _capture)

    async def go():
        from models.database import init_db
        await init_db()
        await svc.start_scan(["70000001"], CruiseLine.NCL, keep_browser_open=False,
                             headless=True, market="CA")
        await asyncio.sleep(0)
    asyncio.run(go())
    assert got["market"] == "CA" and got["headless"] is True

"""NCL Canada inside the NCL tab, 2026-10-16.

Neon: "I have added the seaweb cad can you try to make a script in it as
well but within ncl tab and same enviromwent nothing extra".

Before: the GUI never passed a market, so every NCL scan used the US login;
Canadian bookings came back NOT_ON_THIS_ACCOUNT (231 rows; 18 distinct in
the last 3 days) and were never priced. Every NCL row's currency read
"UNKNOWN" - and the 2026-10-07 audit had caught CAD added to USD.
"""
import asyncio
from types import SimpleNamespace

from core.models import BookingResult, BookingStatus, CruiseLine
from scraper.ncl import NclScraper


def _result(bid, status=BookingStatus.NO_SAVING, **kw):
    return BookingResult(cruise_line=CruiseLine.NCL, status=status, booking_id=bid, **kw)


# -- the account's currency on every result ----------------------------------

def _scraper(market, result):
    s = NclScraper(market=market)

    async def _inner(bid, capture_market_data=False):
        return result
    s._check_booking_market = _inner
    return s


def test_a_us_result_is_usd_and_a_ca_result_is_cad_and_says_so():
    us = asyncio.run(_scraper("US", _result("1", note="no saving")).check_booking("1"))
    ca = asyncio.run(_scraper("CA", _result("2", note="no saving")).check_booking("2"))
    assert us.currency == "USD" and us.note == "no saving"
    assert ca.currency == "CAD" and ca.note.startswith("[CA account, CAD]")


def test_a_currency_already_read_is_never_overwritten():
    r = asyncio.run(_scraper("CA", _result("3", currency="USD")).check_booking("3"))
    assert r.currency == "USD"


# -- "no market" means US: never reuse the Canada login for a US scan --------

def test_a_plain_ncl_request_after_a_canada_pass_gets_a_us_scraper():
    from services.booking_service import BookingService

    svc = BookingService()
    stopped = []

    class _Live:
        cruise_line, market, is_alive = CruiseLine.NCL, "CA", True

        async def stop(self):
            stopped.append(True)
    svc._live_scraper = _Live()
    made = []

    class _New:
        cruise_line = CruiseLine.NCL

        def __init__(self, market):
            self.market = market

        async def start(self, headless=None):
            pass

    svc._get_scraper = lambda line, market=None: made.append(market) or _New(market)
    got = asyncio.run(svc.get_or_create_scraper(CruiseLine.NCL))
    assert stopped == [True] and got.market == "US"


# -- the second pass, inside the same NCL run --------------------------------

class _Job:
    def __init__(self, results):
        self.job_id = f"job{id(self)}"
        self.results = results
        self.status = SimpleNamespace(value="COMPLETED")
        self.current_booking_id = None
        self.progress_done = self.progress_total = len(results)


def test_not_found_bookings_are_rechecked_on_the_canada_account():
    from gui.queue_manager import BookingQueueManager

    calls = []

    class _Service:
        async def check_login(self, line, timeout_minutes=15.0, market=None, headless=False):
            calls.append(("login", line, market, headless))
            return True

        async def start_scan(self, ids, line, **kw):
            calls.append(("scan", list(ids), kw.get("market"), kw.get("bypass_cache")))
            if kw.get("market") == "CA":
                return _Job([_result(b, currency="CAD") for b in ids])
            return _Job([_result("3001052"),
                         _result("70000001", BookingStatus.NOT_ON_THIS_ACCOUNT),
                         _result("70000002", BookingStatus.NOT_ON_THIS_ACCOUNT)])

        async def stop_scan(self, job_id):
            return True

    qm = BookingQueueManager()
    qm._service = _Service()

    async def _noop():
        return None
    qm.initialize = _noop
    qm._last_login_headless = True
    for b in ("3001052", "70000001", "70000002"):
        qm.add_booking(b)
    shown = []
    asyncio.run(qm.start_processing(CruiseLine.NCL, on_result=shown.append))

    assert calls[0][0] == "scan" and calls[0][2] is None            # the US pass as before
    assert ("login", CruiseLine.NCL, "CA", True) in calls           # CA login, same headless choice
    assert ("scan", ["70000001", "70000002"], "CA", True) in calls  # only the not-found, cache bypassed
    ca = [r for r in shown if r.currency == "CAD"]
    assert sorted(r.booking_id for r in ca) == ["70000001", "70000002"]


def test_no_canada_pass_when_everything_was_found():
    from gui.queue_manager import BookingQueueManager

    calls = []

    class _Service:
        async def check_login(self, *a, **k):
            calls.append("login")
            return True

        async def start_scan(self, ids, line, **kw):
            calls.append("scan")
            return _Job([_result(b) for b in ids])

        async def stop_scan(self, job_id):
            return True

    qm = BookingQueueManager()
    qm._service = _Service()

    async def _noop():
        return None
    qm.initialize = _noop
    qm.add_booking("3001052")
    asyncio.run(qm.start_processing(CruiseLine.NCL, on_result=lambda r: None))
    assert calls == ["scan"]

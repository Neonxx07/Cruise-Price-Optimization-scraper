"""Every capture is stored as what it IS, not as ESPRESSO's shape.

Neon 2026-09-16, on the NCL run: "it is just seems that it is seenig the
same prices it doe not go up or it does not go down".

Investigating that surfaced a separate, real defect. `_save_market_data_to_db`
was written around ESPRESSO's capture - a category table under "rows" - and
forced that shape on every cruise line. NCL's capture has NO "rows" key at
all; it carries the payment state. So all 135 NCL rows written in that run
were EMPTY, and mislabelled "ncl_category_table" when the scraper had
already reported "ncl_booking_details".

The dropped fields are the expensive part. final_payment_date, amount_due
and commission_rate all sit in NCL's "derived" block, and they are exactly
what is needed to rank a booking by urgency or warn that a finding is about
to expire - the gap that let $3,945 of found savings lapse during an 18-day
scan gap.

(The price reading itself was NOT broken: of 20 NCL bookings compared that
day, 13 showed today's price genuinely HIGHER by $40-$1,620 and 7 were
unchanged. NCL prices had risen, so there were no savings to find.)
"""
import json

import pytest

from core.models import BookingResult, BookingStatus, CruiseLine


def _result(line=CruiseLine.NCL):
    return BookingResult(booking_id="3000056", cruise_line=line,
                         status=BookingStatus.NO_SAVING, net_saving=0.0,
                         confidence=3, price_category="BX")


NCL_CAPTURE = {
    "capture_type": "ncl_booking_details",
    "payment": {"grossDue": 43.0, "netDue": 0.0, "commissEarned": 521.28},
    "derived": {
        "amount_due": 43.0,
        "net_due": 0.0,
        "commission_rate": 0.1317,
        "final_payment_date": "2026-10-01",
        "balance_is_all_commission": True,
        "cruise_line_fully_paid": True,
    },
}


@pytest.mark.asyncio
async def test_an_ncl_capture_keeps_its_payment_state():
    """THE DEFECT. Everything below used to be discarded because the writer
    only looked for "rows"."""
    from models.database import MarketDataRecord, async_session, init_db
    from services.booking_service import BookingService

    await init_db()
    await BookingService()._save_market_data_to_db(_result(), NCL_CAPTURE)

    from sqlalchemy import select

    async with async_session() as s:
        rec = (await s.execute(
            select(MarketDataRecord)
            .where(MarketDataRecord.booking_id == "3000056")
            .order_by(MarketDataRecord.id.desc()))).scalars().first()

    assert rec is not None
    payload = json.loads(rec.payload_json)
    assert payload["derived"]["final_payment_date"] == "2026-10-01"
    assert payload["derived"]["amount_due"] == 43.0
    assert payload["derived"]["commission_rate"] == 0.1317


@pytest.mark.asyncio
async def test_the_capture_is_labelled_what_the_scraper_called_it():
    """It was hardcoded to "ncl_category_table" - a category table it never
    was - so the stored history lied about its own contents."""
    from models.database import MarketDataRecord, async_session, init_db
    from services.booking_service import BookingService

    await init_db()
    await BookingService()._save_market_data_to_db(_result(), NCL_CAPTURE)

    from sqlalchemy import select

    async with async_session() as s:
        rec = (await s.execute(
            select(MarketDataRecord).order_by(MarketDataRecord.id.desc()))
        ).scalars().first()
    assert rec.capture_type == "ncl_booking_details"


@pytest.mark.asyncio
async def test_an_espresso_category_table_still_works_unchanged():
    """The ESPRESSO shape is the one this table was built for and must not
    regress - `rows` still populates category_table_json."""
    from models.database import MarketDataRecord, async_session, init_db
    from services.booking_service import BookingService

    capture = {
        "currentCategory": "BX",
        "executionToken": "tok",
        "selectionJSON": "{}",
        "rows": [{"category": "BX", "total": 2100.0}],
    }
    await init_db()
    await BookingService()._save_market_data_to_db(
        _result(CruiseLine.ESPRESSO), capture)

    from sqlalchemy import select

    async with async_session() as s:
        rec = (await s.execute(
            select(MarketDataRecord).order_by(MarketDataRecord.id.desc()))
        ).scalars().first()
    assert rec.capture_type == "espresso_category_table"
    assert json.loads(rec.category_table_json) == capture["rows"]
    assert rec.current_category == "BX"


@pytest.mark.asyncio
async def test_an_unserialisable_value_does_not_lose_the_whole_capture():
    """A datetime or Decimal in a payload must not make the write fail and
    throw the capture away - default=str keeps it."""
    from datetime import datetime

    from models.database import async_session, init_db  # noqa: F401
    from services.booking_service import BookingService

    await init_db()
    await BookingService()._save_market_data_to_db(
        _result(), {"capture_type": "ncl_booking_details",
                    "derived": {"final_payment_date": datetime(2026, 10, 1)}})


def test_the_payload_column_exists_on_the_model():
    from models.database import MarketDataRecord

    assert hasattr(MarketDataRecord, "payload_json")


# -- capture everything, on by default ------------------------------


def test_capture_everything_is_on_by_default():
    """Neon 2026-09-16: "Capture everything can you make this own by
    defult?" - consistent with his standing rule that captured data is the
    corpus this project mines. Leaving it off meant the richest diagnostic
    source was absent exactly when something went wrong: the NCL run that
    day wrote 135 empty market_data rows and there was no page capture to
    explain why."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from gui.windows import CruiseLinePanel

    QApplication.instance() or QApplication([])
    for line in CruiseLine:
        panel = CruiseLinePanel(line)
        assert panel.capture_everything_checkbox.isChecked(), line.value


def test_it_stays_a_checkbox_not_a_constant():
    """The cost is real - one 576-booking ESPRESSO run with this on
    produced 452 MB - so a fast run must still be able to turn it off."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from gui.windows import CruiseLinePanel

    QApplication.instance() or QApplication([])
    panel = CruiseLinePanel(CruiseLine.NCL)
    panel.capture_everything_checkbox.setChecked(False)
    assert panel.capture_everything_checkbox.isChecked() is False


def test_capture_everything_starts_a_playwright_trace():
    """The capture mechanism is Playwright's OWN tracing API - a DOM
    snapshot per action plus network and console, viewable in
    trace.playwright.dev. Adopted rather than hand-rolled: it is
    Microsoft's canonical approach, already proven in this repo by
    record_msc_session.py, and one compressed trace replaces thousands of
    loose HTML files."""
    import inspect

    import scraper.base

    src = inspect.getsource(scraper.base)
    assert "tracing.start(screenshots=True, snapshots=True" in src
    assert "tracing.stop(" in src

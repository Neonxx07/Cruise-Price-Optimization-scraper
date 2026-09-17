"""A headless choice for NCL, and for NCL only.

Neon 2026-09-16: "add an option from the gui and to check headless and non
headless to choose between of them in the gui in NCL only for now".

NCL EARNED THIS, it was not assumed. Neon's challenge was the right one -
"it is not only about opening the booking it is also about pressing on switch
to edit mode and the categories" - so it was measured before being offered:
the same three bookings run headless and headed returned identical totals AND
identical category counts (30 / 23 / 31, all from _form_12), including a real
+$1,620 increase on 3000069. Switch to Edit Mode, the SlickGrid read, the
price comparison and cancel-and-release all work with no display.

NOT offered for the others. ESPRESSO can NEVER be headless - Akamai bot
detection breaks it and scraper/base.py enforces that regardless of any
argument passed. MSC and GoCCL have simply not been tested this way, and
offering an untested toggle would be inviting the next silent failure.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import CruiseLine  # noqa: E402
from gui.windows import CruiseLinePanel  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_the_toggle_is_visible_on_ncl(qt_app):
    panel = CruiseLinePanel(CruiseLine.NCL)
    panel.show()
    qt_app.processEvents()
    assert panel.headless_checkbox.isVisible()


@pytest.mark.parametrize("line", [c for c in CruiseLine if c is not CruiseLine.NCL])
def test_it_is_hidden_everywhere_else(qt_app, line):
    """ESPRESSO especially: offering a headless choice there would be
    offering something the browser layer will refuse."""
    panel = CruiseLinePanel(line)
    panel.show()
    qt_app.processEvents()
    assert not panel.headless_checkbox.isVisible(), line.value


def test_it_is_unchecked_by_default(qt_app):
    """Behaviour is unchanged until it is asked for - NCL has always run
    visibly in the GUI because check_login opened the browser with
    headless=False."""
    assert CruiseLinePanel(CruiseLine.NCL).headless_checkbox.isChecked() is False


def test_it_governs_the_scan_not_only_the_login():
    """The GUI passes keep_browser_open=True, so start_scan's own `headless`
    argument never applies - every booking is checked in the browser that
    check_login opened. Wiring this to the wrong call would produce a toggle
    that visibly does nothing."""
    import inspect

    from gui.windows import CruiseLinePanel as Panel

    src = inspect.getsource(Panel)
    call = src[src.index("queue_manager.check_login("):][:400]
    assert "headless=" in call
    assert "headless_checkbox.isChecked()" in call


def test_a_non_ncl_panel_can_never_request_headless():
    """Belt and braces: the call site also checks the cruise line, so a
    stale checked box on another tab cannot leak through."""
    import inspect

    from gui.windows import CruiseLinePanel as Panel

    src = inspect.getsource(Panel)
    call = src[src.index("queue_manager.check_login("):][:400]
    assert "CruiseLine.NCL" in call


def test_the_service_default_is_still_visible():
    """check_login hardcoded headless=False for months. The default must not
    change, or every existing caller silently loses its window."""
    import inspect

    from services.booking_service import BookingService

    assert (inspect.signature(BookingService.check_login)
            .parameters["headless"].default is False)


def test_the_queue_manager_passes_it_through():
    import inspect

    from gui.queue_manager import BookingQueueManager

    assert "headless" in inspect.signature(BookingQueueManager.check_login).parameters
    src = inspect.getsource(BookingQueueManager.check_login)
    assert "headless=headless" in src

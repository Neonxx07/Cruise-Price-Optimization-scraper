"""GREEN IS FOR AN AVAILABLE LOWER PRICE ONLY - project-wide.

Neon 2026-10-15: "change msc coloring from green for not green things if
there is a add discount opprtuinity satatus make it orange instead of green
PLEASE NOTE THAT THROUGH THE WHOLE PROJECT GREEN IS FOR AVAIABLE LOWER
RPICES ONLY AND PRICE DIFFERNCES".

Before: every MSC row with ANY opportunity was the same green as a
confirmed saving - including an unpriced "add the club 5%" lead - and the
scan-status panel painted a RUNNING line pure green.
"""
import io
import re
import tokenize
from pathlib import Path

from core.calculator_msc import msc_row_status
from core.models import MscBookingResult, MscCheck, MscCheckStatus, MscOpportunityType

GREEN = "C6EFCE"
ROOT = Path(__file__).resolve().parents[1]


def _result(*checks):
    return MscBookingResult(booking_id="X", checks=list(checks), has_any_opportunity=True)


def _check(kind, status=MscCheckStatus.OPPORTUNITY, note="", value=None):
    return MscCheck(type=kind, status=status, note=note, estimated_value=value)


def test_a_discount_lead_without_a_lower_price_is_orange_not_green():
    """3001065: club 5% on an undiscounted booking, today's quote unverified."""
    r = _result(_check(MscOpportunityType.DISCOUNT_ADD,
                       note="no discount currently applied — Voyagers Club 5% ... — not priced (...)"))
    assert msc_row_status(r) == "DISCOUNT_OPPORTUNITY"


def test_a_price_match_with_dollars_is_green():
    r = _result(_check(MscOpportunityType.PRICE_MATCH, value=81.98))
    assert msc_row_status(r) == "OPPORTUNITY"


def test_a_discount_that_reprices_below_the_fare_is_green():
    r = _result(_check(MscOpportunityType.VOYAGERS_SELECTION,
                       note="MSVG10W ... — repricing today at 2,100.00 less 10% gives "
                            "1,890.00, below the current 2,000.00"))
    assert msc_row_status(r) == "OPPORTUNITY"


def test_nothing_found_and_no_result():
    r = _result(_check(MscOpportunityType.DISCOUNT_ADD, status=MscCheckStatus.NO_OPPORTUNITY))
    assert msc_row_status(r) == "NO_OPPORTUNITY"
    assert msc_row_status(None) == "NO_OPPORTUNITY"


def test_only_lower_price_statuses_are_ever_tinted_green():
    from gui.windows import CruiseLinePanel
    from services.excel_export import _FILLS

    gui_green = {k for k, v in CruiseLinePanel._STATUS_TINTS.items()
                 if v.upper().lstrip("#") == GREEN}
    assert gui_green == {"OPTIMIZATION", "OPPORTUNITY"}
    xls_green = {k for k, v in _FILLS.items() if (v.fgColor.rgb or "").upper().endswith(GREEN)}
    assert xls_green == {"OPTIMIZATION"}
    assert CruiseLinePanel._STATUS_TINTS["DISCOUNT_OPPORTUNITY"].upper() != "#" + GREEN


def _code_without_comments(path: Path) -> str:
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            out.append(tok.string)
    return " ".join(out)


def test_no_saturated_green_anywhere_in_the_gui_or_services():
    """The status panel's RUNNING row was Qt.GlobalColor.green."""
    for folder in ("gui", "services"):
        for path in (ROOT / folder).glob("*.py"):
            code = _code_without_comments(path)
            assert not re.search(r"GlobalColor \. green\b|Qt \. green\b", code), path.name

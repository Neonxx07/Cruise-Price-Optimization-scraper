"""Regression tests for the 2026-08-26 production-readiness pass.

Each test below corresponds to a REAL defect found by auditing this
codebase (several corroborated against the live cruise_intel.db), and
would have failed before its fix. Grouped by the file that was fixed.
"""
import os
import tempfile


from core.models import BookingResult, BookingStatus, CruiseLine


# ── main.py: watchlist atomicity + audit trail ───────────────────────


def _result(booking_id: str, status: BookingStatus) -> BookingResult:
    return BookingResult(
        cruise_line=CruiseLine.ESPRESSO, status=status, booking_id=booking_id,
    )


def test_watchlist_rewrite_is_atomic_and_leaves_no_temp_files():
    """The rewrite used to truncate the real file in place with open(...,"w"),
    so any interruption between truncate and write destroyed the entire
    client watchlist. Now written to a temp file + os.replace()."""
    from main import remove_paid_in_full_from_watchlist

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("111\n222\n333\n")

        removed = remove_paid_in_full_from_watchlist(
            path, [_result("222", BookingStatus.PAID_IN_FULL)]
        )

        assert removed == ["222"]
        with open(path, encoding="utf-8") as f:
            assert f.read() == "111\n333\n"
        # No stray temp files left behind in the directory.
        leftovers = [n for n in os.listdir(d) if n.startswith(".watchlist_")]
        assert leftovers == [], f"temp files leaked: {leftovers}"


def test_watchlist_removal_writes_an_audit_log():
    """Nothing used to record WHICH bookings were dropped or when, so a
    wrong PAID_IN_FULL call silently removed a real client booking from all
    future scans with no way to find out."""
    from main import remove_paid_in_full_from_watchlist

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("111\n222\n")
        remove_paid_in_full_from_watchlist(path, [_result("111", BookingStatus.PAID_IN_FULL)])

        with open(path + ".removals.log", encoding="utf-8") as f:
            logged = f.read()
        assert "111" in logged
        assert "PAID_IN_FULL" in logged


def test_watchlist_keeps_a_backup_before_rewriting():
    from main import remove_paid_in_full_from_watchlist

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.txt")
        original = "111\n222\n333\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)
        remove_paid_in_full_from_watchlist(path, [_result("222", BookingStatus.PAID_IN_FULL)])
        with open(path + ".bak", encoding="utf-8") as f:
            assert f.read() == original, "backup must hold the PRE-removal content"


def test_watchlist_untouched_when_nothing_is_paid_in_full():
    """An ERROR or NO_SAVING result must never remove anything, and the
    file must not even be rewritten."""
    from main import remove_paid_in_full_from_watchlist

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watchlist.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("111\n# a comment\n\n222\n")
        removed = remove_paid_in_full_from_watchlist(path, [
            _result("111", BookingStatus.ERROR),
            _result("222", BookingStatus.NO_SAVING),
        ])
        assert removed == []
        with open(path, encoding="utf-8") as f:
            assert f.read() == "111\n# a comment\n\n222\n"
        assert not os.path.exists(path + ".bak"), "must not even rewrite when nothing to remove"


# ── msc_commands.py: hyphenated discount names ───────────────────────


def test_discount_regex_matches_hyphenated_names():
    """CONFIRMED REAL BUG: the label/type groups excluded hyphens, so a
    real disclosed 'MIL-CIV-IL-DSCNT-10%' could NEVER match — making
    current_discounts empty and producing a FALSE 'no discount applied,
    add one' DISCOUNT_ADD opportunity."""
    from msc_commands import _DISCOUNT_RE

    text = (
        "Discount Description: MIL-CIV-IL-DSCNT-10% - "
        "Discount Type: Military - Discount Rate: 10.0%"
    )
    m = _DISCOUNT_RE.search(text)
    assert m is not None, "hyphenated discount name still does not match"
    assert m.group(2) == "MIL-CIV-IL-DSCNT-10%"
    assert m.group(4) == "10.0"


def test_discount_regex_still_matches_plain_names():
    """The widened groups must not break the non-hyphenated case."""
    from msc_commands import _DISCOUNT_RE

    m = _DISCOUNT_RE.search(
        "MSC Club Discount: MSCCLUB5 - Discount Type: Club - Discount Rate: 5.0%"
    )
    assert m is not None
    assert m.group(2) == "MSCCLUB5"
    assert m.group(4) == "5.0"


# ── calculator_msc.py: VOYAGERS EXCLUSIVES ───────────────────────────


def test_voyagers_selection_not_recommended_when_exclusives_already_applied():
    """CONFIRMED REAL GAP: the already-applied guard only tested for
    'SPECIAL OFFER', so a disclosed 'VOYAGERS EXCLUSIVES' discount cleared
    it and this check confidently recommended stacking a Voyagers
    Selection discount on top of the same program."""
    from core.calculator_msc import _check_voyagers_selection
    from core.models import MscCheckStatus

    check = _check_voyagers_selection(
        current_discounts=[{
            "kind": "named",
            "label": "VOYAGERS EXCLUSIVES",
            "rate_pct": 9.75,
        }],
        today_discount_catalog=[{"program_name": "VOYAGERS SELECTION", "label": "MSVG10W", "rate_pct": 10.0}],
        has_voyagers=True,
        senior_count=0,
        is_group_rate=False,
    )
    assert check.status != MscCheckStatus.OPPORTUNITY, (
        "must not recommend Voyagers Selection when Exclusives is already applied"
    )


def test_voyagers_selection_still_offered_when_nothing_applied():
    """The widened guard must not suppress a genuine opportunity."""
    from core.calculator_msc import _check_voyagers_selection
    from core.models import MscCheckStatus

    check = _check_voyagers_selection(
        current_discounts=[],
        today_discount_catalog=[{"program_name": "VOYAGERS SELECTION", "label": "MSVG10W", "rate_pct": 10.0}],
        has_voyagers=True,
        senior_count=0,
        is_group_rate=False,
    )
    assert check.status == MscCheckStatus.OPPORTUNITY

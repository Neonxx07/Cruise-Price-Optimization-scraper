"""Exports must never overwrite each other.

Neon, 2026-09-16, urgent: "the export rersults extracts file name one only
scan_results so i exported for esspresso and ncl and it got replaced".

Every panel wrote to the SAME two files - reports/scan_results.csv and
reports/scan_results.xlsx - with no cruise line and no date in the name.
Exporting ESPRESSO and then NCL silently replaced the ESPRESSO export. No
warning, and afterwards no way to tell from the file which line it even
held: the surviving scan_results.csv turned out to be 143 NCL rows, and the
576 ESPRESSO rows were gone.

Recoverable, as it happens - every result is persisted to the bookings
table as it is produced, and the export is only a view of it (see
rebuild_export.py, which regenerated both). But an export that destroys the
previous one is a data-loss bug regardless of whether a backup exists.
"""
import re

import pytest

from core.models import CruiseLine


def _names_for(line: str, stamp: str) -> tuple[str, str]:
    """The naming the GUI now uses, mirrored so the pattern itself is
    asserted rather than just the code that builds it."""
    return (f"{line}_scan_results_{stamp}.csv",
            f"{line}_scan_results_{stamp}.xlsx")


def test_two_cruise_lines_cannot_collide():
    """THE BUG. Same second, different line - the names must still differ,
    because a user exporting one tab then the next does exactly this."""
    esp = _names_for("ESPRESSO", "20260916_090114")
    ncl = _names_for("NCL", "20260916_090114")
    assert set(esp).isdisjoint(ncl)


def test_two_runs_of_the_same_line_cannot_collide():
    """A re-scan on the same day must not overwrite the morning's export."""
    a = _names_for("ESPRESSO", "20260916_090114")
    b = _names_for("ESPRESSO", "20260916_154502")
    assert set(a).isdisjoint(b)


def test_the_name_carries_both_the_line_and_the_date():
    """The surviving file told you nothing about what it held. Both facts
    have to be in the filename to be useful a week later."""
    csv_name, _ = _names_for("NCL", "20260915_090248")
    assert csv_name.startswith("NCL_")
    assert re.search(r"_\d{8}_\d{6}\.csv$", csv_name), csv_name


def test_files_sort_chronologically_within_a_line():
    """YYYYMMDD_HHMMSS sorts lexicographically as well as chronologically,
    so a directory listing is already in run order."""
    stamps = ["20260916_154502", "20260915_090248", "20260916_090114"]
    names = [_names_for("ESPRESSO", s)[0] for s in stamps]
    assert sorted(names) == [
        "ESPRESSO_scan_results_20260915_090248.csv",
        "ESPRESSO_scan_results_20260916_090114.csv",
        "ESPRESSO_scan_results_20260916_154502.csv",
    ]


@pytest.mark.parametrize("line", [c.value for c in CruiseLine])
def test_every_cruise_line_produces_a_distinct_prefix(line):
    assert _names_for(line, "20260916_090114")[0].startswith(f"{line}_")


# -- the shipped code, not just the pattern -------------------------


def test_the_gui_no_longer_uses_a_fixed_export_filename():
    """Pins the actual export handler. A pattern test alone would still
    pass if someone reverted the code."""
    import inspect

    from gui.windows import CruiseLinePanel

    src = inspect.getsource(CruiseLinePanel._on_export)
    assert '"scan_results.csv"' not in src, "fixed filename is back"
    assert '"scan_results.xlsx"' not in src, "fixed filename is back"
    assert '"msc_scan_results.csv"' not in src, "fixed MSC filename is back"
    assert "strftime" in src and "self.cruise_line.value" in src, (
        "the export name no longer carries a timestamp and a cruise line"
    )


def test_the_msc_export_is_stamped_too():
    """MSC exports from its own tab and had the same fixed-name problem."""
    import inspect

    from gui.windows import CruiseLinePanel

    src = inspect.getsource(CruiseLinePanel._on_export)
    assert "MSC_scan_results_{stamp}" in src


def test_the_rebuild_tool_refuses_to_overwrite():
    """The recovery tool must not repeat the very mistake it exists to
    repair."""
    import inspect

    import rebuild_export

    src = inspect.getsource(rebuild_export.rebuild)
    assert "refusing to overwrite" in src
    assert "Path(path).exists()" in src


def test_the_rebuild_tool_keeps_only_the_latest_row_per_booking():
    """A booking re-scanned during a run has several rows. Exporting all of
    them would double count the money - the same defect the GUI reload
    had."""
    import inspect

    import rebuild_export

    src = inspect.getsource(rebuild_export._rows)
    assert "MAX(id)" in src

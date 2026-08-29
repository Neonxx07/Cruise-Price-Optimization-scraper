"""CSV export service.

Generates CSV reports from booking results, matching the original
extension's autoSaveCSV format.
"""

from __future__ import annotations

import csv
import io

from core.models import BookingResult, MscBookingResult, MscOpportunityType


def export_results_csv(results: list[BookingResult]) -> str:
    """
    Export booking results to CSV string.

    Returns:
        CSV content as a string.
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)

    # Header — Price Drop/OBC Change/Lost Fares/Re-addable Fares/Gained
    # Fares appended at the end (existing columns kept in their original
    # order/position for anyone already relying on it): the Excel export
    # already had these, the CSV export didn't, and there was no reason
    # for the two to disagree on what's available.
    writer.writerow([
        "Booking ID", "Cruise Line", "Status", "Net Saving",
        "Old Total", "New Total", "Category", "New Category",
        "Note", "Lost Packages", "Confidence", "Checked At",
        "Price Drop", "OBC Change", "Lost Fares", "Re-addable Fares", "Gained Fares",
        "Lost Travel Protection", "Promos Before", "Promos After",
    ])

    # Data rows
    for r in results:
        writer.writerow([
            r.booking_id,
            r.cruise_line.value,
            r.status.value,
            f"{r.net_saving:.2f}",
            f"{r.old_total:.2f}",
            f"{r.new_total:.2f}",
            r.price_category or "",
            r.new_price_category or "",
            r.note,
            "|".join(r.lost_pkg_names),
            r.confidence,
            r.checked_at.isoformat() if r.checked_at else "",
            f"{r.price_drop:.2f}",
            f"{r.obc_change:.2f}",
            "|".join(r.lost_fares),
            "|".join(r.re_addable_fares),
            "|".join(r.gained_fares),
            "|".join(r.lost_travel_protection),
            r.old_promos,
            r.new_promos,
        ])

    return output.getvalue()


def export_msc_results_csv(results: list[MscBookingResult]) -> str:
    """Export MSC booking results to CSV string.

    MSC doesn't fit export_results_csv's single old_total/new_total/
    net_saving shape (see core/models.py's MscBookingResult docstring) — a
    booking is evaluated for up to four independent, non-exclusive
    opportunity types instead of one status. Same column layout
    msc_run_calculator.py's offline CSV export already uses for the
    four-check columns, minus that script's senior-blind-spot caveat
    column (derived from extra passenger-parsing data a live GUI
    MscBookingResult doesn't carry) — kept as a single source of truth for
    anyone reviewing MSC opportunities from the GUI instead of the CLI.
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)

    check_types = [t.value for t in MscOpportunityType]
    header = ["Booking ID", "Category", "Has Opportunity", "Note", "Checked At"]
    for check_type in check_types:
        header += [check_type, f"{check_type} Note"]
    writer.writerow(header)

    for r in results:
        by_type = {c.type.value: c for c in r.checks}
        row = [
            r.booking_id, r.category or "", r.has_any_opportunity, r.note,
            r.checked_at.isoformat() if r.checked_at else "",
        ]
        for check_type in check_types:
            c = by_type.get(check_type)
            row.append(c.status.value if c else "")
            row.append(c.note if c else "")
        writer.writerow(row)

    return output.getvalue()

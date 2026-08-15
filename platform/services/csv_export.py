"""CSV export service.

Generates CSV reports from booking results, matching the original
extension's autoSaveCSV format.
"""

from __future__ import annotations

import csv
import io

from core.models import BookingResult


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
        ])

    return output.getvalue()

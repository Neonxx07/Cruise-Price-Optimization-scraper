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

    # Header
    writer.writerow([
        "Booking ID", "Cruise Line", "Status", "Net Saving",
        "Old Total", "New Total", "Category", "New Category",
        "Note", "Lost Packages", "Confidence", "Checked At",
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
        ])

    return output.getvalue()

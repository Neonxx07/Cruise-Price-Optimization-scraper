"""MSC status-match eligibility: no Voyagers membership, no applied discount.

Added 2026-10-08, Neon: "detect that this customer doesnt have a msc
voyagers membership and no discount applied".

Reads one MSC booking record exactly as msc_commands._lookup_one_booking
captures it (and data/msc_control/booking_data.jsonl stores it). Three
answers, never two:

  ELIGIBLE        no passenger has a Voyagers Club number, and "no
                  discount" is PROVEN - the SRN-vs-standard-fare check ran
  NOT_ELIGIBLE    a Voyagers member is on the booking, a discount is
                  applied (a MILITARY discount named as such - rule
                  MSC-D9: no Voyagers discount on top of military), or the
                  booking is cancelled / not found
  CHECK_MANUALLY  required data is missing, malformed, stale, or unverified

Why "no discount printed" is not enough on its own: senior and Voyagers
Exclusive discounts never print a disclosure line (bookings 3000021,
3000024). An empty discount list only proves "no discount" when the SRN
check could run, which today means a 3-, 4- or 7-night cruise
(msc_commands.STANDARD_NCF_BY_NIGHTS). Otherwise it is CHECK_MANUALLY -
reporting it ELIGIBLE would repeat the 2026-08-24 false positives on
3000031, 3000018 and 3000017.

Measured over the 133 bookings captured by 2026-10-08: 3 ELIGIBLE, 2 with
no printed discount but unprovable, 93 with a Voyagers member aboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.calculator_msc import military_discounts, military_rule_note

ELIGIBLE = "ELIGIBLE"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
CHECK_MANUALLY = "CHECK_MANUALLY"


@dataclass(frozen=True)
class StatusMatchEligibility:
    verdict: str
    reason: str

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason}


def assess_status_match(record: dict | None) -> StatusMatchEligibility:
    """Classify one captured MSC booking record. Never raises."""
    # Imported here, not at module level: msc_commands pulls in Playwright,
    # and core/ stays importable without it.
    from msc_commands import _extract_srn_value, _srn_reference_available

    if not isinstance(record, dict):
        return StatusMatchEligibility(CHECK_MANUALLY, "booking not read from MSC")

    if record.get("session_expired"):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "MSC session expired; booking details are unverified"
        )

    summary = record.get("summary_text")
    if not isinstance(summary, str):
        return StatusMatchEligibility(CHECK_MANUALLY, "booking summary not read")
    if "no bookings found" in summary.casefold():
        return StatusMatchEligibility(NOT_ELIGIBLE, "booking not found on MSC")
    if record.get("explicitly_cancelled") or record.get("cancelled_or_postponed_placeholder"):
        return StatusMatchEligibility(NOT_ELIGIBLE, "booking is cancelled or postponed")

    passengers = record.get("passengers")
    if not isinstance(passengers, list) or not passengers:
        return StatusMatchEligibility(CHECK_MANUALLY, "passenger list not read")
    if any(not isinstance(passenger, dict) for passenger in passengers):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "passenger data is incomplete or malformed"
        )

    members = [p for p in passengers if p.get("voyagers_number")]
    has_voyagers = record.get("has_voyagers")
    if has_voyagers is not None and not isinstance(has_voyagers, bool):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "Voyagers membership status is malformed"
        )
    if members:
        who = ", ".join(f"{p.get('name')} ({p.get('voyagers_tier') or 'tier unknown'})"
                        for p in members)
        return StatusMatchEligibility(NOT_ELIGIBLE, f"already a Voyagers member: {who}")
    if has_voyagers is True:
        return StatusMatchEligibility(
            NOT_ELIGIBLE, "already a Voyagers member on the booking"
        )
    if has_voyagers is None and any(
        "voyagers_number" not in passenger for passenger in passengers
    ):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "Voyagers membership was not captured for every passenger"
        )

    discounts = record.get("current_discounts")
    if discounts is None:
        return StatusMatchEligibility(
            CHECK_MANUALLY, "no Voyagers member, but the price breakdown was not "
                            "captured, so discounts are unknown")
    if not isinstance(discounts, list):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "captured discount data is malformed"
        )
    if any(not isinstance(discount, dict) for discount in discounts):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "captured discount data is incomplete or malformed"
        )
    # RULE MSC-D9 (Neon 2026-10-08): a Voyagers discount cannot go on top
    # of military, so matching an external loyalty tier gains a military booking
    # nothing. Booking 3001062 (MIL-CIV-IL-DSCNT-05%) is the case.
    military = military_discounts(discounts)
    if military:
        return StatusMatchEligibility(
            NOT_ELIGIBLE, "no status match: " + military_rule_note(military))
    if discounts:
        labels = ", ".join(f"{d.get('label')} {d.get('rate_pct')}%" for d in discounts)
        return StatusMatchEligibility(NOT_ELIGIBLE, f"discount applied: {labels}")

    breakdown = record.get("breakdown_text")
    if not isinstance(breakdown, str):
        return StatusMatchEligibility(
            CHECK_MANUALLY, "price breakdown not captured; discounts are unknown"
        )
    proven = (_srn_reference_available(summary)
              and _extract_srn_value(breakdown) is not None)
    if not proven:
        return StatusMatchEligibility(
            CHECK_MANUALLY, "no Voyagers member and no discount printed, but senior/"
                            "Voyagers Exclusive discounts never print and the fare "
                            "check only covers 3, 4 and 7-night cruises - check the "
                            "price breakdown")
    return StatusMatchEligibility(ELIGIBLE, "no Voyagers member on the booking and "
                                            "no discount applied")

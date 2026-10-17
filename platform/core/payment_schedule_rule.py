"""THE PAYMENT SCHEDULE RULE - one rule for every cruise line.

Neon, 2026-10-15 (HARD RULE): "we can never add deposit or payment dates
changes unless regarding the dates only situation we can save if the final
payment is further not nearer from the original final payment date".

So a repricing is refused - NO_SAVING, whatever it saves - when it would:

  1. ADD A DEPOSIT, i.e. any payment falling due before the final payment
     that the booking does not already have;
  2. MOVE ANY PAYMENT NEARER - the final payment date earlier, or an
     existing pending payment (deposit) to an earlier date or a new date;

and the ONE change allowed is:

  3. THE FINAL PAYMENT DATE MOVING LATER (further away) - more time to pay
     is never a loss. It is still named in the note.

A schedule that cannot be read is never assumed unchanged: the result is
UNKNOWN (None), which the caller must treat as unconfirmed, never green.

Evidence: DEMO16 (GoCCL, 2026-10-15) showed as an $868 "saving" (1,396 ->
528 on FSA Players Club FunMatch) whose review added "Deposit $200.00 Due
Date Jul28, 2026" to a booking with no pending deposit (its next payment
was the 2027-07-12 final payment). The existing deposit check never ran
because the invoice-sum check came first and returned "unconfirmed" on the
TFPA FunPlay line. This rule is now checked BEFORE any money figure. Earlier:
DEMO18 (2026-10-12) PB4 would have put a $300 deposit due Aug 07 2026 -
a date already past - on a booking with $100 paid; Neon refused it.

Princess runs the same function but only WRITES the result into its note
(Neon 2026-10-15: "a thing we keep an eye on but it is not as madnatory as
carnival") - see calculator_princess.decide.

Dates are ISO strings ("2027-07-12") or datetime.date - anything comparable
with < and of the same kind on both sides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The reason prefix every refusal note carries, so the GUI, the export and
#: tests can find a payment-schedule refusal by text.
REFUSAL_TAG = "PAYMENT SCHEDULE"


@dataclass(frozen=True)
class ScheduleVerdict:
    #: True = allowed, False = refused, None = could not be compared.
    allowed: bool | None
    reason: str = ""
    #: Set when the final payment moves later - allowed, but reported.
    final_moved_later: bool = False


def check_payment_schedule(
    orig_final: Any, new_final: Any,
    orig_next: Any = None, new_next: Any = None,
    deposit_on_new: bool | None = None,
    deposit_on_orig: bool | None = None,
) -> ScheduleVerdict:
    """Judge a repricing's payment schedule against the booking's own.

    orig_final / new_final  final payment due date, booking vs new fare
    orig_next / new_next    the next payment of any kind (GoCCL
                            closestPaymentDueBy). Equal to the final date
                            when no deposit is pending.
    deposit_on_new          a "Deposit" line was SEEN on the new fare's
                            schedule (None = not looked at)
    deposit_on_orig         the booking itself still has a deposit pending
    """
    if orig_final is None or new_final is None:
        return ScheduleVerdict(None, "the final payment due date was not read on "
                                     "both sides, so the payment schedule could not be compared")

    # 2. The final payment may never come nearer.
    if new_final < orig_final:
        return ScheduleVerdict(False, f"{REFUSAL_TAG}: the final payment would fall due "
                                      f"EARLIER, {orig_final} -> {new_final}")

    # 1. A deposit may never be added. A pending payment before the final one
    #    on the new fare is a deposit, whether or not the line was read.
    new_has_early = new_next is not None and new_next < new_final
    orig_has_early = (orig_next is not None and orig_next < orig_final) or bool(deposit_on_orig)
    if deposit_on_new and not orig_has_early:
        return ScheduleVerdict(False, f"{REFUSAL_TAG}: a DEPOSIT would be added "
                                      f"(the booking has none pending)")
    if new_has_early and not orig_has_early:
        return ScheduleVerdict(False, f"{REFUSAL_TAG}: a DEPOSIT would be added - a payment "
                                      f"falls due {new_next}, before the final payment "
                                      f"({new_final}); the booking's next payment is "
                                      f"{orig_next or orig_final}")
    # 2b. An existing pending payment may not move at all - nearer is a loss,
    #     and only the FINAL date is allowed to move later.
    if new_has_early and orig_has_early and orig_next is not None and new_next != orig_next:
        return ScheduleVerdict(False, f"{REFUSAL_TAG}: the pending deposit would move, "
                                      f"{orig_next} -> {new_next}")
    if new_next is not None and orig_next is not None and new_next < orig_next:
        return ScheduleVerdict(False, f"{REFUSAL_TAG}: the next payment would fall due "
                                      f"EARLIER, {orig_next} -> {new_next}")

    # 3. The only allowed change: the final payment moving later.
    if new_final > orig_final:
        return ScheduleVerdict(True, f"final payment moves LATER, {orig_final} -> "
                                     f"{new_final} (allowed - more time to pay)",
                               final_moved_later=True)
    return ScheduleVerdict(True, "")

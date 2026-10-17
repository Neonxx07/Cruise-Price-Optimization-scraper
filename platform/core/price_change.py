"""Did the price move since the last time we looked?

Neon 2026-09-29: *"When a stale booking is rescanned, compare the new
prices against the previous successful scan and flag changes (price dropped
/ increased / unchanged). A price drop is the repricing opportunity, so it
should stand out."*

`price_history` has kept a row per scan all along - 6,606 of them - and
nothing ever compared two consecutive rows. The whole product exists to
catch a price going down, and the data to see it was already on disk.

DELIBERATELY SEPARATE from the repricing calculators. Those answer "is
there a better fare available right now", which needs the live category
table. This answers the much simpler "is this booking's own total different
from last time", which needs only two numbers and no portal at all.
"""

from __future__ import annotations

from dataclasses import dataclass

# Movements smaller than this are noise, not news: rounding between
# currencies, a cent of tax reconciliation. Matches the tolerance the
# paid-in-full test already uses for "near enough to zero".
NOISE_FLOOR = 1.00

DROPPED = "DROPPED"
INCREASED = "INCREASED"
UNCHANGED = "UNCHANGED"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PriceChange:
    """How this booking's total compares with the previous scan."""

    direction: str            # DROPPED | INCREASED | UNCHANGED | UNKNOWN
    delta: float | None       # new - previous; negative means cheaper
    previous: float | None
    current: float | None

    @property
    def is_drop(self) -> bool:
        """The one that matters - a repricing opportunity."""
        return self.direction == DROPPED

    @property
    def summary(self) -> str:
        if self.direction == UNKNOWN or self.delta is None:
            return "no previous scan to compare"
        if self.direction == UNCHANGED:
            return "unchanged since the last scan"
        word = "DROPPED" if self.delta < 0 else "increased"
        return f"{word} {abs(self.delta):,.2f} since the last scan"


def compare(previous_total: float | None,
            current_total: float | None,
            noise_floor: float = NOISE_FLOOR) -> PriceChange:
    """Compare this scan's total against the previous one.

    UNKNOWN when either side is missing. That is not a formality: a booking
    with no prior scan and a booking whose price did not move are completely
    different facts, and reporting the first as "unchanged" would invent a
    history that does not exist. Missing is not zero - the rule that this
    project has already been bitten by twice.
    """
    if previous_total is None or current_total is None:
        return PriceChange(UNKNOWN, None, previous_total, current_total)
    delta = round(current_total - previous_total, 2)
    if abs(delta) < noise_floor:
        return PriceChange(UNCHANGED, delta, previous_total, current_total)
    return PriceChange(DROPPED if delta < 0 else INCREASED,
                       delta, previous_total, current_total)

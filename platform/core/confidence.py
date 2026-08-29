"""Confidence scoring algorithm.

Ported from calcConfidence() in the original calculator.js.
Evaluates how reliable an optimization recommendation is based on
fare changes, package losses, and OBC shifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


def _round2(value: float) -> float:
    """Same rounding convention as core.calculator.round2 (ROUND_HALF_UP
    via a Decimal(str(...)) string round-trip, not Python's banker's-
    rounding builtin `round()`) — duplicated here rather than imported to
    avoid a circular import (calculator.py imports calc_confidence from
    this module already, at module load time). ADDED 2026-08-13 (Phase 0
    correctness audit): fare_change_pct below used to be computed with
    plain `round(x, 2)`, the exact float-representation/banker's-rounding
    defect calculator.round2 was already rewritten to fix elsewhere in
    this codebase — just missed here because this field is a percentage
    display value, not a dollar amount, and was out of that earlier fix's
    scope. This is a display-only field (never summed, never fed back
    into a financial decision), so this only changes what a human sees
    for the rare exact-half-cent-equivalent tie case — never a dollar
    figure."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class ConfidenceResult:
    score: int          # 1-5 stars
    fare_change_pct: float
    old_cruise_fare: float
    new_cruise_fare: float


def calc_confidence(
    old_cruise_fare: float,
    new_cruise_fare: float,
    net_saving: float,
    old_total: float,
    lost_pkg_value: float,
    obc_change: float,
) -> ConfidenceResult:
    """
    Score an optimization from 1-5 stars.

    Positive signals: fare decrease, high net %, no lost packages, OBC stable/up.
    Negative signals: fare increase, package losses.

    Returns:
        ConfidenceResult with score (1-5) and fare analysis.
    """
    try:
        fare_change_pct = (
            (new_cruise_fare - old_cruise_fare) / old_cruise_fare
            if old_cruise_fare > 0
            else 0.0
        )
        net_pct = net_saving / old_total if old_total > 0 else 0.0

        pts = 0

        # Fare direction scoring
        if fare_change_pct < -0.02:
            pts += 2
        elif fare_change_pct < 0:
            pts += 1
        elif fare_change_pct > 0.15:
            pts -= 2
        elif fare_change_pct > 0.05:
            pts -= 1

        # Net saving impact
        if net_pct > 0.05:
            pts += 2
        elif net_pct > 0.02:
            pts += 1

        # Package and OBC stability
        if lost_pkg_value <= 0:
            pts += 1
        if obc_change >= 0:
            pts += 1

        # Points → stars lookup
        pts_to_stars = {
            -2: 1, -1: 1, 0: 2, 1: 2, 2: 2,
            3: 3, 4: 4, 5: 5, 6: 5,
        }
        clamped = max(-2, min(6, pts))
        score = pts_to_stars.get(clamped, 3)

        # Safety caps — high fare increases reduce confidence
        if fare_change_pct >= 0.05 and score > 3:
            score = 3
        if fare_change_pct > 0.10 and lost_pkg_value > 0:
            score = min(score, 2)

        return ConfidenceResult(
            score=score,
            fare_change_pct=_round2(fare_change_pct * 100),
            old_cruise_fare=old_cruise_fare,
            new_cruise_fare=new_cruise_fare,
        )

    except Exception:
        return ConfidenceResult(
            score=3,
            fare_change_pct=0.0,
            old_cruise_fare=0.0,
            new_cruise_fare=0.0,
        )

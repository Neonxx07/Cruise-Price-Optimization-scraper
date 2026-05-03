"""Confidence scoring algorithm.

Ported from calcConfidence() in the original calculator.js.
Evaluates how reliable an optimization recommendation is based on
fare changes, package losses, and OBC shifts.
"""

from __future__ import annotations

from dataclasses import dataclass


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
            fare_change_pct=round(fare_change_pct * 100, 2),
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

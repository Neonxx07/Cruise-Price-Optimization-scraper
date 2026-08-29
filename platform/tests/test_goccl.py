"""Tests for scraper/goccl.py's guest-count-candidate probing.

Added 2026-08-25 alongside the fix for a real, confirmed gap: GoCCL's
calculate_goccl estimates a candidate's gross total as
`price_per_person * guests_count`, but no real GoCCL capture has ever
confirmed what field (if any) holds a real per-booking guest count inside
window.initialData -- every real call site today falls back to a global
default (see core/calculator.py's guests_count_verified handling).

_probe_guest_count_candidates is a purely diagnostic, non-guessing helper:
it tries several plausible (UNCONFIRMED) candidate paths and reports only
the ones that actually resolve, so a human can check a real capture and
confirm whether any of them is real -- it is never wired into
guests_count/guests_count_verified itself. These tests only cover that
this probing is safe (never raises, never invents a value) and correctly
reports which candidates resolved -- they do NOT claim any candidate path
is confirmed real.
"""
from scraper.goccl import _probe_guest_count_candidates


def test_no_candidates_found_on_confirmed_real_shape():
    """The ONLY real, confirmed shape of window.initialData (per
    read_current_price_and_selection's own docstring) has no guest-count
    field at all -- this must return an empty dict, not guess one."""
    data = {
        "invoiceSummary": {"grossAmount": {"amount": 1234.56}},
        "rate": {"code": "ABC123"},
        "category": {"code": "8A", "stateroomType": {"name": "BALCONY"}},
    }
    assert _probe_guest_count_candidates(data) == {}


def test_finds_a_candidate_when_one_plausible_path_exists():
    data = {"guestCount": 4}
    found = _probe_guest_count_candidates(data)
    assert found == {"guestCount": 4}


def test_finds_multiple_candidates_independently():
    data = {"numGuests": 2, "occupancy": {"total": 2}, "guests": ["a", "b"]}
    found = _probe_guest_count_candidates(data)
    assert found["numGuests"] == 2
    assert found["occupancy.total"] == 2
    assert found["guests (count)"] == 2


def test_never_raises_on_malformed_or_missing_nested_shapes():
    """A candidate path partially existing with the wrong shape (e.g.
    occupancy is a string, not a dict) must be skipped, not crash the
    whole probe."""
    data = {"occupancy": "not-a-dict", "passengers": None}
    found = _probe_guest_count_candidates(data)
    assert isinstance(found, dict)  # did not raise
    assert "occupancy.total" not in found


def test_empty_data_returns_empty_dict():
    assert _probe_guest_count_candidates({}) == {}

"""Parity mechanism between core/calculator.py (Python) and
extension/calculator.js (JavaScript) -- two independently-maintained
implementations of the same business logic, confirmed to agree exactly
as of 2026-08-13, with NO shared source of truth and no automated check
before this file existed. This is a real regression risk documented in
that audit: a future fix to one file (e.g. widening a tolerance
constant, which this project's own history shows already happened once)
is trivial to land in only one of the two, and nothing would catch the
divergence -- the extension and the desktop GUI would then quietly
disagree on whether the same booking is a TRAP, an OPTIMIZATION, or
PAID_IN_FULL.

No JavaScript runtime (Node) is available in this environment, so this
does not execute calculator.js -- it extracts each business-critical
constant's literal value directly from the JS source text via targeted
regex and compares it against the real Python value (imported directly,
never hand-copied), and fails loudly if either side's declaration can't
be found at all (protecting against a silent rename breaking the check
itself, not just a value drift).
"""
import re

import pytest

from core.calculator import (
    ESPRESSO_ROOM_TYPE_RANK,
    GOCCL_CANDIDATE_CONFIDENCE,
    NCL_ADDON_VALUES,
    OBC_LOSS_MIN_RATIO,
    PAID_IN_FULL_TOLERANCE_FLAT,
    PAID_IN_FULL_TOLERANCE_PCT,
)

CALCULATOR_JS_PATH = "../../extension/calculator.js"


@pytest.fixture(scope="module")
def js_source():
    import os
    path = os.path.join(os.path.dirname(__file__), CALCULATOR_JS_PATH)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_js_number_const(source: str, name: str) -> float:
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*([\d.]+)\s*;", source)
    assert m, f"could not find `const {name} = ...;` in calculator.js -- extraction itself is broken, not just the value"
    return float(m.group(1))


def _extract_js_object_const(source: str, name: str) -> dict:
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*\{{(.*?)\}}\s*;", source, re.DOTALL)
    assert m, f"could not find `const {name} = {{...}};` in calculator.js -- extraction itself is broken, not just the value"
    body = m.group(1)
    pairs = re.findall(r"['\"]([^'\"]+)['\"]\s*:\s*(-?[\d.]+)", body)
    assert pairs, f"found `const {name}` but extracted zero key:value pairs from it -- extraction is broken"
    return {k: float(v) for k, v in pairs}


def test_parity_obc_loss_min_ratio(js_source):
    js_value = _extract_js_number_const(js_source, "OBC_LOSS_MIN_RATIO")
    assert js_value == OBC_LOSS_MIN_RATIO, (
        f"PARITY BREAK: calculator.js OBC_LOSS_MIN_RATIO={js_value} != "
        f"core/calculator.py OBC_LOSS_MIN_RATIO={OBC_LOSS_MIN_RATIO}"
    )


def test_parity_goccl_candidate_confidence(js_source):
    js_value = _extract_js_number_const(js_source, "GOCCL_CANDIDATE_CONFIDENCE")
    assert js_value == GOCCL_CANDIDATE_CONFIDENCE


def test_parity_paid_in_full_tolerance(js_source):
    js_flat = _extract_js_number_const(js_source, "PAID_IN_FULL_TOLERANCE_FLAT")
    js_pct = _extract_js_number_const(js_source, "PAID_IN_FULL_TOLERANCE_PCT")
    assert js_flat == PAID_IN_FULL_TOLERANCE_FLAT
    assert js_pct == PAID_IN_FULL_TOLERANCE_PCT


def test_parity_ncl_addon_values(js_source):
    js_values = _extract_js_object_const(js_source, "NCL_ADDON_VALUES")
    py_values = {k: float(v) for k, v in NCL_ADDON_VALUES.items()}
    assert js_values == py_values, (
        f"PARITY BREAK in NCL_ADDON_VALUES.\n  JS:     {js_values}\n  Python: {py_values}"
    )


def test_parity_espresso_room_type_rank(js_source):
    js_values = _extract_js_object_const(js_source, "ESPRESSO_ROOM_TYPE_RANK")
    py_values = {k: float(v) for k, v in ESPRESSO_ROOM_TYPE_RANK.items()}
    assert js_values == py_values, (
        f"PARITY BREAK in ESPRESSO_ROOM_TYPE_RANK.\n  JS:     {js_values}\n  Python: {py_values}"
    )

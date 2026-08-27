"""Validates the smart_locator heuristic scoring against REAL, already-
confirmed field attributes from this project's own scrapers (ESPRESSO's
search input, MSC's Voyagers Club insert fields) — proving the ranking
logic would have correctly identified the real field among plausible
decoys BEFORE trusting it on a genuinely new, unfamiliar portal. This is
the same real-data-before-trust discipline used everywhere else in this
project (see the 2026-08-25 price_parser near-miss on ESPRESSO's
_price_from_row for why this matters)."""
import pytest

from scraper.smart_locator import FieldCandidate, find_candidate_fields, score_candidate


# ── Pure scoring logic, synthetic data mirroring real confirmed fields ──


def test_scores_espressos_real_search_input_highest_among_decoys():
    """CONFIRMED REAL FIELD (scraper/espresso.py's _SEARCH_INPUT_SELECTOR):
    id="reservationid", data-qa="secure.espresso.input.reservation.search".
    A generic "find the booking search box" query must rank this above
    plausible decoys on the same page (a name search, a date field, an
    unrelated free-text box)."""
    real_field = {"id": "reservationid", "dataQa": "secure.espresso.input.reservation.search", "name": None, "placeholder": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None}
    decoys = [
        {"id": "guestLastName", "name": "lastName", "placeholder": "Last Name", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "sailDate", "name": "sailDate", "placeholder": "MM/DD/YYYY", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "commentBox", "name": "notes", "placeholder": "Add a note...", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
    ]
    keywords = ["reservation", "booking", "confirmation number"]

    real_score = score_candidate(real_field, keywords)
    decoy_scores = [score_candidate(d, keywords) for d in decoys]

    assert real_score > max(decoy_scores), (
        f"real field scored {real_score}, best decoy scored {max(decoy_scores)} -- "
        "heuristic would have picked the wrong field"
    )


def test_scores_msc_voyagers_first_name_field_highest_among_decoys():
    """CONFIRMED REAL FIELDS (msc_project_knowledge.md's Voyagers Club
    insert flow): #club-firstname/#club-lastname/#club-dob/#club-card.
    Searching for "first name"/"given name" must rank #club-firstname
    above the adjacent last-name/DOB/card-number fields on the same
    modal."""
    real_field = {"id": "club-firstname", "name": None, "placeholder": "First Name", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None}
    decoys = [
        {"id": "club-lastname", "name": None, "placeholder": "Last Name", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "club-dob", "name": None, "placeholder": "Date of Birth", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "club-card", "name": None, "placeholder": "Card Number", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
    ]
    keywords = ["first name", "given name"]

    real_score = score_candidate(real_field, keywords)
    decoy_scores = [score_candidate(d, keywords) for d in decoys]

    assert real_score > max(decoy_scores)


def test_score_zero_for_element_with_no_signal_at_all():
    """An element with none of the tracked attributes (no id, no name, no
    placeholder, nothing) has nothing to score against -- must return 0,
    not raise, not crash the ranking of everything else on the page."""
    empty = {"id": None, "name": None, "placeholder": None, "ariaLabel": None, "title": None, "dataQa": None, "labelText": None, "visibleText": None}
    assert score_candidate(empty, ["reservation"]) == 0.0


def test_suggested_selector_prefers_id_then_data_qa_then_name():
    from scraper.smart_locator import _suggest_selector
    assert _suggest_selector({"id": "reservationid", "dataQa": "x", "name": "y"}, "textbox", 0) == "#reservationid"
    assert _suggest_selector({"id": None, "dataQa": "secure.espresso.input.reservation.search", "name": "y"}, "textbox", 0) == '[data-qa="secure.espresso.input.reservation.search"]'
    assert _suggest_selector({"id": None, "dataQa": None, "name": "lastName"}, "textbox", 0) == '[name="lastName"]'
    assert _suggest_selector({"id": None, "dataQa": None, "name": None}, "textbox", 2) == "textbox:nth-of-type(3)"


# ── find_candidate_fields, using a fake page/locator (no live browser) ──


class _FakeElementHandle:
    def __init__(self, attrs: dict):
        self._attrs = attrs

    async def evaluate(self, js):
        return self._attrs


class _FakeRoleLocator:
    def __init__(self, elements: list[dict]):
        self._elements = elements

    async def count(self):
        return len(self._elements)

    def nth(self, i):
        return _FakeElementHandle(self._elements[i])


class _FakePage:
    def __init__(self, elements: list[dict]):
        self._elements = elements

    def get_by_role(self, role):
        return _FakeRoleLocator(self._elements)


@pytest.mark.asyncio
async def test_find_candidate_fields_ranks_real_field_first_and_returns_selector():
    elements = [
        {"id": "guestLastName", "name": "lastName", "placeholder": "Last Name", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "reservationid", "name": None, "placeholder": None, "dataQa": "secure.espresso.input.reservation.search", "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
        {"id": "sailDate", "name": "sailDate", "placeholder": "MM/DD/YYYY", "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None},
    ]
    page = _FakePage(elements)

    results = await find_candidate_fields(page, ["reservation", "booking"], role="textbox")

    assert isinstance(results[0], FieldCandidate)
    # id wins over data-qa when both are present (matches _suggest_selector's
    # documented preference order, and real ESPRESSO usage: the id selector
    # is listed FIRST in _SEARCH_INPUT_SELECTOR).
    assert results[0].suggested_selector == "#reservationid"
    assert results[0].score > results[1].score


@pytest.mark.asyncio
async def test_find_candidate_fields_skips_elements_that_error_on_evaluate():
    class _BrokenElement:
        async def evaluate(self, js):
            raise Exception("detached from DOM")

    class _MixedLocator:
        async def count(self):
            return 2

        def nth(self, i):
            return _BrokenElement() if i == 0 else _FakeElementHandle(
                {"id": "reservationid", "name": None, "placeholder": None, "dataQa": None, "ariaLabel": None, "title": None, "labelText": None, "visibleText": None}
            )

    class _MixedPage:
        def get_by_role(self, role):
            return _MixedLocator()

    results = await find_candidate_fields(_MixedPage(), ["reservation"], role="textbox")

    assert len(results) == 1
    assert results[0].suggested_selector == "#reservationid"

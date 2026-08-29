"""Classical, deterministic "smart field finder" for drafting a brand-new
cruise line's scraper faster — no AI, no API key, no trained model.

Scores every element of a given ARIA role on the current page against a
list of keywords, using RapidFuzz text similarity over the element's
visible contextual signals (id, name, placeholder, aria-label, title,
data-qa, associated <label> text). This is the same category of
technique browser password managers/autofill engines already use to
guess "this is the username field" — plain heuristics over attributes
that are already there, not an LLM reading and understanding the page.

Meant for the one-time, human-supervised task of onboarding a NEW,
unfamiliar cruise line portal: point it at an unfamiliar login/search
page (in a real, visible browser you're already logged into or looking
at) and get back a ranked "this element looks like what you're after"
list with a suggested selector — then a human confirms/tests it against
real captured HTML before it becomes real BaseScraper code, same
discipline CONTRIBUTING.md already establishes for `playwright codegen`
output (never trust a generated/suggested selector blind).

Never used in the live, recurring scan pipeline — those scrapers keep
their own explicit, hand-verified selectors exactly as before. This is
onboarding tooling only.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

# Reads the handful of attributes real portals actually use to label a
# field, plus any <label for="..."> text — the same signals a sighted
# human (or a screen reader) would use to figure out what a field is for.
_CONTEXT_JS = """
(el) => {
    const label = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
    return {
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        name: el.getAttribute('name'),
        placeholder: el.getAttribute('placeholder'),
        ariaLabel: el.getAttribute('aria-label'),
        title: el.getAttribute('title'),
        dataQa: el.getAttribute('data-qa'),
        labelText: label ? label.textContent.trim() : null,
        visibleText: (el.textContent || '').trim().slice(0, 80),
    };
}
"""


@dataclass
class FieldCandidate:
    role: str
    index: int
    score: float
    context_text: str
    suggested_selector: str
    attrs: dict


def _context_string(attrs: dict) -> str:
    parts = [
        attrs.get("id"), attrs.get("name"), attrs.get("placeholder"),
        attrs.get("ariaLabel"), attrs.get("title"), attrs.get("dataQa"),
        attrs.get("labelText"), attrs.get("visibleText"),
    ]
    return " ".join(p for p in parts if p)


def score_candidate(attrs: dict, keywords: list[str]) -> float:
    """Pure, synchronously-testable scoring function — given the
    attributes already read off one element, return the best similarity
    score (0-100) against any of the given keywords. Split out from
    find_candidate_fields so the ranking logic itself can be validated
    with synthetic data mirroring REAL known fields (no live browser
    needed) before ever trusting it on a genuinely unfamiliar page —
    see tests/test_smart_locator.py, which validates this against
    ESPRESSO's and MSC's actual real field attributes."""
    context = _context_string(attrs)
    if not context:
        return 0.0
    return max(fuzz.WRatio(kw, context) for kw in keywords)


def _suggest_selector(attrs: dict, role: str, index: int) -> str:
    """Prefer the most stable handle available, in the same order a human
    hand-picking a selector would — id, then name, then data-qa (the
    project's own experience with ESPRESSO's Mantine rebuild is exactly
    why data-qa outranks a generated id there), falling back to a
    positional role selector only when nothing else is present."""
    if attrs.get("id"):
        return f"#{attrs['id']}"
    if attrs.get("dataQa"):
        return f'[data-qa="{attrs["dataQa"]}"]'
    if attrs.get("name"):
        return f'[name="{attrs["name"]}"]'
    return f"{role}:nth-of-type({index + 1})"


async def find_candidate_fields(
    page, keywords: list[str], role: str = "textbox", limit: int = 5,
) -> list[FieldCandidate]:
    """Rank every visible element of `role` on the CURRENT page by how
    well its contextual text matches `keywords`. Read-only — never
    clicks, never fills, never submits anything. Returns the top `limit`
    candidates, best score first.

    Typical roles worth trying on an unfamiliar page: "textbox" (search/
    login inputs), "button" (submit/search buttons), "table" (category/
    price grids)."""
    locator = page.get_by_role(role)
    count = await locator.count()
    scored: list[FieldCandidate] = []
    for i in range(count):
        el = locator.nth(i)
        try:
            attrs = await el.evaluate(_CONTEXT_JS)
        except Exception:
            continue
        score = score_candidate(attrs, keywords)
        scored.append(FieldCandidate(
            role=role,
            index=i,
            score=score,
            context_text=_context_string(attrs),
            suggested_selector=_suggest_selector(attrs, role, i),
            attrs=attrs,
        ))
    scored.sort(key=lambda c: -c.score)
    return scored[:limit]

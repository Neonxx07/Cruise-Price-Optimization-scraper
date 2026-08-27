"""Interactive, read-only helper for onboarding a NEW, unfamiliar cruise
line portal faster — no AI, no API key, no trained model.

Opens a real, visible browser at a URL you give it. You navigate and log
in yourself (this never touches any credentials and never clicks or
fills anything on your behalf). Whenever you're looking at a page you
want to understand, press Enter and pick what you're hunting for — it
ranks every element of the relevant kind on the CURRENT page by how well
its visible attributes (id, name, placeholder, aria-label, title,
data-qa, associated <label> text) match a set of keywords, using plain
text-similarity scoring (see scraper/smart_locator.py) — the same
category of heuristic a password manager already uses to guess "this is
the username field," not an LLM reading and understanding the page.

Purely observational — safe to run against a real, logged-in session.
Prints a ranked list with a SUGGESTED selector for each candidate.
Verify any suggestion against the real page (view-source / inspect
element) before trusting it inside an actual BaseScraper subclass — same
discipline this project already applies to `playwright codegen` output
(see CONTRIBUTING.md's GoCCL lesson: a selector "confirmed" by a
recording tool still turned out wrong against a real live booking).

Usage:
    python discover_portal_fields.py <url>
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

from scraper.smart_locator import find_candidate_fields

# (label, role, keywords) — role/keywords are None for the "custom" entry,
# filled in interactively instead.
COMMON_SEARCHES: dict[str, tuple[str, str | None, list[str] | None]] = {
    "1": ("Booking/reservation search box", "textbox", ["reservation", "booking", "confirmation number", "record locator"]),
    "2": ("Username/agent-ID login field", "textbox", ["username", "user id", "agent id", "login"]),
    "3": ("Password field", "textbox", ["password"]),
    "4": ("Search/submit button", "button", ["search", "submit", "go", "find"]),
    "5": ("Custom role + keywords...", None, None),
}


async def _run_search(page) -> None:
    print("\nWhat are you looking for?")
    for key, (label, _, _) in COMMON_SEARCHES.items():
        print(f"  {key}. {label}")
    choice = input("Type a number: ").strip()
    entry = COMMON_SEARCHES.get(choice)
    if not entry:
        print("Not a valid choice.")
        return

    label, role, keywords = entry
    if role is None:
        role = input("ARIA role to search (e.g. textbox, button, table): ").strip() or "textbox"
        keywords = [k.strip() for k in input("Keywords (comma-separated): ").split(",") if k.strip()]
    if not keywords:
        print("Need at least one keyword.")
        return

    results = await find_candidate_fields(page, keywords, role=role)
    if not results:
        print(f"No {role!r} elements found on the current page.")
        return

    print(f"\nTop matches for {label!r} (role={role}):")
    for c in results:
        print(f"  score={c.score:5.1f}  selector={c.suggested_selector!r}")
        if c.context_text:
            print(f"    context: {c.context_text[:100]!r}")
    print("\nVerify the top suggestion against the real page before trusting it —")
    print("this is a ranked guess from attributes alone, not a confirmed answer.")


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python discover_portal_fields.py <url>")
        sys.exit(1)
    url = sys.argv[1]

    print("=" * 60)
    print("   PORTAL FIELD DISCOVERY (no AI, no API key, no model)")
    print("=" * 60)
    print(f"\nOpening {url} in a visible browser.")
    print("Navigate / log in yourself — this never fills in or clicks anything for you.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(url)

        while True:
            input("\nNavigate to the page you want to inspect, then press Enter here...")
            await _run_search(page)
            again = input("\nSearch again on this page? (y/N): ").strip().lower()
            if again != "y":
                break

        input("\nDone. Press Enter to close the browser and exit...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

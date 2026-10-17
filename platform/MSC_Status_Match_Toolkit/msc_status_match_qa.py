"""Run status-match local QA or dispatch verified live preparation.

Default `--mode qa` preserves the original synthetic local-mock workflow and
all of its existing functions. `--mode prepare` adds a separate real-data
path that never submits MSC's application.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import string
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
BOOKING_DATA_PATH = PLATFORM_ROOT / "data" / "msc_control" / "booking_data.jsonl"
ADDRESS_URL = "https://www.fakepersongenerator.com/random-address"
MAIL_API_URL = "https://api.mail.tm"
STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}

MOCK_FORM = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Local MSC Status Match QA Mock</title>
<style>
body { font: 16px Arial, sans-serif; margin: 2rem auto; max-width: 760px; color: #17202a; }
fieldset { border: 1px solid #899; margin: 1rem 0; padding: 1rem; }
label { display: block; margin: .8rem 0; }
input, select { display: block; box-sizing: border-box; margin-top: .3rem; padding: .5rem; width: 100%; }
button { padding: .6rem 1rem; }
.notice { background: #fff3cd; padding: .8rem; }
.qa-warning { background: #b00020; color: #fff; font-weight: bold; padding: .8rem; }
</style></head>
<body>
<h1>Local MSC Status Match QA Mock</h1>
<p class="notice">Synthetic test only. This page is local and is not MSC's website.
No application can be submitted from this mock.</p>
<p id="qa-only-loyalty-warning" class="qa-warning" hidden>
QA ONLY - SYNTHETIC LOYALTY PLACEHOLDERS - NOT ACCOUNT PROOF - NO TIER VERIFIED
</p>
<form id="status-match">
  <fieldset><legend>Personal information</legend>
    <label>First name <input name="customer.name" required></label>
    <label>Last name <input name="customer.surname" required></label>
    <label>Date of birth <input name="customer.dateOfBirth" placeholder="MM/DD/YYYY" required></label>
    <label>Mobile number <input name="customer.mobilePhone:tel" required></label>
    <label>Email <input name="customer.email" type="email" required></label>
  </fieldset>
  <fieldset><legend>Address information</legend>
    <label>Country <select name="customer.address.countryCode"><option value="US">United States</option></select></label>
    <label>ZIP code <input name="customer.address.postCode" required></label>
    <label>City <input name="customer.address.town" required></label>
    <label>State <input name="customer.address.state" required></label>
  </fieldset>
  <fieldset><legend>Local QA loyalty placeholders (not real account proof)</legend>
    <label>Company <input name="existingMembership.company"></label>
    <label>Status <input name="existingMembership.status"></label>
    <label>Membership number <input name="existingMembership.number"></label>
    <label>Card proof <input name="attachment:file" type="file"></label>
  </fieldset>
  <fieldset><legend>Applicant-only actions (intentionally untouched)</legend>
    <label>Signature <input name="signature_skip"></label>
    <label><input name="genericMarketingConsent_skip:boolean" type="checkbox"> Marketing</label>
    <label><input name="profilingConsents" type="checkbox"> Personalization</label>
  </fieldset>
  <button type="button" id="confirm" disabled>Confirm (disabled)</button>
</form>
<script>
document.querySelector('#status-match').addEventListener('submit', event => {
  event.preventDefault();
  event.stopImmediatePropagation();
});
</script>
</body></html>"""


def load_passenger(booking_id: str, passenger_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the newest matching passenger record without modifying the corpus."""
    if not re.fullmatch(r"\d{6,9}", booking_id):
        raise ValueError("MSC booking ID must be 6-9 digits")
    if passenger_index < 1:
        raise ValueError("passenger_index must be 1 or greater")
    if not BOOKING_DATA_PATH.is_file():
        raise FileNotFoundError(f"No MSC capture corpus found at {BOOKING_DATA_PATH}")

    latest = None
    with BOOKING_DATA_PATH.open(encoding="utf-8") as captures:
        for line_number, line in enumerate(captures, 1):
            try:
                capture = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Malformed JSON in MSC capture corpus at line {line_number}"
                ) from exc
            if capture.get("booking_id") == booking_id:
                latest = capture
    if latest is None:
        raise LookupError(
            f"No existing capture for booking {booking_id}; run the read-only "
            f"MSC command scan_passengers:{booking_id} first"
        )

    passengers = latest.get("passengers")
    if not isinstance(passengers, list) or passenger_index > len(passengers):
        raise LookupError(f"Passenger {passenger_index} is not present in the latest capture")
    passenger = passengers[passenger_index - 1]
    age = passenger.get("age")
    if not isinstance(age, int) or isinstance(age, bool) or age < 18:
        raise ValueError("Selected passenger is not verified as an adult; refusing to use the record")
    first_name = passenger.get("first_name")
    last_name = passenger.get("last_name")
    dob = passenger.get("dob")
    if not all(isinstance(value, str) and value.strip() for value in (first_name, last_name, dob)):
        raise ValueError("Selected passenger is missing a verified first name, last name, or DOB")
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", dob):
        raise ValueError("Captured DOB is not in the project's MM/DD/YYYY format")
    return latest, {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "date_of_birth": dob,
        "age": age,
    }


def _api_request(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{MAIL_API_URL}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Mail.tm API {method} {path} failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Mail.tm API {method} {path} could not be reached: {exc.reason}") from exc


def _collection_items(payload: Any, description: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("hydra:member"), list):
        items = payload["hydra:member"]
    else:
        raise RuntimeError(f"Unexpected {description} response from Mail.tm")
    if any(not isinstance(item, dict) for item in items):
        raise RuntimeError(f"Malformed {description} response from Mail.tm")
    return items


async def create_test_inbox() -> tuple[str, str]:
    """Create an ephemeral Mail.tm inbox; retain its password only in memory."""
    _, domain_payload = await asyncio.to_thread(_api_request, "/domains")
    domains = [
        domain["domain"]
        for domain in _collection_items(domain_payload, "domain list")
        if domain.get("domain") and domain.get("isActive", True)
    ]
    if not domains:
        raise RuntimeError("Mail.tm returned no active domains")
    local_part = "qa" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(20)
    )
    address = f"{local_part}@{domains[0]}"
    password = secrets.token_urlsafe(24)
    await asyncio.to_thread(
        _api_request,
        "/accounts",
        "POST",
        {"address": address, "password": password},
    )
    _, token_data = await asyncio.to_thread(
        _api_request,
        "/token",
        "POST",
        {"address": address, "password": password},
    )
    if not isinstance(token_data, dict) or not isinstance(token_data.get("token"), str):
        raise RuntimeError("Mail.tm account created but token response was malformed")
    return address, token_data["token"]


async def poll_test_inbox(token: str, wait_seconds: int) -> int:
    """Poll inbox metadata only; never fetch message bodies or send mail."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        _, payload = await asyncio.to_thread(
            _api_request, "/messages", "GET", None, token
        )
        count = len(_collection_items(payload, "message list"))
        if count or asyncio.get_running_loop().time() >= deadline:
            return count
        await asyncio.sleep(min(2, max(0, deadline - asyncio.get_running_loop().time())))


async def generate_test_address(state: str) -> dict[str, str]:
    """Generate a fictional address with the public address generator."""
    state_code = STATE_CODES.get(state)
    if state_code is None:
        raise ValueError(f"Unsupported US state: {state}")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            response = await page.goto(
                ADDRESS_URL, wait_until="domcontentloaded", timeout=60_000
            )
            if response is None or response.status >= 400:
                status = response.status if response else "no response"
                raise RuntimeError(f"Address generator failed to load (HTTP {status})")
            await page.locator("select").first.select_option(state_code)
            await page.locator('input[type="submit"]').first.click()
            await page.wait_for_function(
                """() => {
                    const box = document.querySelector('textarea');
                    if (!box) return false;
                    try {
                        const address = JSON.parse(box.value);
                        return Boolean(address.city && address.postcode && address.state);
                    } catch {
                        return false;
                    }
                }""",
                timeout=20_000,
            )
            address = json.loads(await page.locator("textarea").first.input_value())
            if address.get("state") != state_code:
                raise RuntimeError("Generated address state does not match the requested state")
            required_fields = ("street", "city", "postcode", "phone", "country", "state")
            if any(not isinstance(address.get(key), str) or not address[key] for key in required_fields):
                raise RuntimeError("Address generator returned incomplete test data")
            return {key: address[key] for key in required_fields}
        finally:
            await browser.close()


async def fill_local_mock(
    passenger: dict[str, Any],
    address: dict[str, str],
    email: str,
    screenshot: Path,
    synthetic_guest_number: str | None = None,
) -> dict[str, Any]:
    """Fill and verify a local-only form and save its screenshot."""
    if synthetic_guest_number is not None and not re.fullmatch(
        r"90[0-9]{8}", synthetic_guest_number
    ):
        raise ValueError("Synthetic guest number must use the reserved 90######## format")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(MOCK_FORM, wait_until="domcontentloaded")
            await page.get_by_label("First name").fill(passenger["first_name"])
            await page.get_by_label("Last name").fill(passenger["last_name"])
            await page.get_by_label("Date of birth").fill(passenger["date_of_birth"])
            await page.get_by_label("Mobile number").fill(address["phone"])
            await page.get_by_label("Email").fill(email)
            await page.get_by_label("Country").select_option("US")
            await page.get_by_label("ZIP code").fill(address["postcode"])
            await page.get_by_label("City").fill(address["city"])
            await page.get_by_role("textbox", name="State").fill(address["state"])
            if synthetic_guest_number is not None:
                await page.locator("#qa-only-loyalty-warning").evaluate(
                    "element => { element.hidden = false; }"
                )
                await page.get_by_label("Company").fill("QA ONLY - NOT A REAL PROGRAM")
                await page.get_by_label("Status").fill("UNVERIFIED QA PLACEHOLDER")
                await page.get_by_label("Membership number").fill(synthetic_guest_number)
            result = await page.locator("#status-match").evaluate(
                """form => {
                    const value = name => form.elements.namedItem(name).value;
                    return {
                        first_name: value('customer.name'),
                        last_name: value('customer.surname'),
                        date_of_birth: value('customer.dateOfBirth'),
                        mobile_number: value('customer.mobilePhone:tel'),
                        email: value('customer.email'),
                        country_code: value('customer.address.countryCode'),
                        zip_code: value('customer.address.postCode'),
                        city: value('customer.address.town'),
                        state: value('customer.address.state'),
                        loyalty_company: value('existingMembership.company'),
                        loyalty_status: value('existingMembership.status'),
                        membership_number: value('existingMembership.number'),
                        qa_only_warning_visible: !form.ownerDocument.querySelector('#qa-only-loyalty-warning').hidden,
                        card_proof_selected: Boolean(form.elements.namedItem('attachment:file').files.length),
                        signature: value('signature_skip'),
                        marketing_checked: form.elements.namedItem('genericMarketingConsent_skip:boolean').checked,
                        personalized_checked: form.elements.namedItem('profilingConsents').checked,
                        confirm_disabled: form.querySelector('#confirm').disabled
                    };
                }"""
            )
            expected = {
                "first_name": passenger["first_name"],
                "last_name": passenger["last_name"],
                "date_of_birth": passenger["date_of_birth"],
                "mobile_number": address["phone"],
                "email": email,
                "country_code": "US",
                "zip_code": address["postcode"],
                "city": address["city"],
                "state": address["state"],
            }
            mismatched = [key for key, value in expected.items() if result.get(key) != value]
            if mismatched:
                raise RuntimeError("Local mock values did not verify: " + ", ".join(mismatched))
            expected_loyalty = (
                {
                    "loyalty_company": "QA ONLY - NOT A REAL PROGRAM",
                    "loyalty_status": "UNVERIFIED QA PLACEHOLDER",
                    "membership_number": synthetic_guest_number,
                }
                if synthetic_guest_number is not None
                else {
                    "loyalty_company": "",
                    "loyalty_status": "",
                    "membership_number": "",
                }
            )
            loyalty_mismatches = [
                key for key, value in expected_loyalty.items()
                if result.get(key) != value
            ]
            if loyalty_mismatches or result["signature"]:
                raise RuntimeError(
                    "Local mock loyalty or signature fields did not verify: "
                    + ", ".join(loyalty_mismatches or ["signature"])
                )
            if result["qa_only_warning_visible"] != (synthetic_guest_number is not None):
                raise RuntimeError("Local mock synthetic-data warning did not verify")
            if result["card_proof_selected"] or result["marketing_checked"] or result["personalized_checked"]:
                raise RuntimeError("Card proof or an optional consent was unexpectedly selected")
            if not result["confirm_disabled"]:
                raise RuntimeError("Local mock Confirm control is not disabled")
            if not page.url.startswith("about:blank"):
                raise RuntimeError("Local mock unexpectedly navigated away from its local document")
            await page.screenshot(path=str(screenshot), full_page=True)
            result["form_submitted"] = False
            result["synthetic_guest_number"] = synthetic_guest_number
            result["synthetic_loyalty_data"] = synthetic_guest_number is not None
            return result
        finally:
            await browser.close()


async def run_workflow(
    booking_id: str,
    passenger_index: int,
    state: str,
    screenshot: Path,
    inbox_wait_seconds: int,
    *,
    synthetic_guest_number: str | None = None,
    synthetic_phone_number: str | None = None,
) -> dict[str, Any]:
    capture, passenger = load_passenger(booking_id, passenger_index)
    address = await generate_test_address(state)
    if synthetic_phone_number is not None:
        if not re.fullmatch(r"\d{10}", synthetic_phone_number):
            raise ValueError("Synthetic phone number must contain exactly 10 digits")
        address["phone"] = synthetic_phone_number
    email, inbox_token = await create_test_inbox()
    form_result = await fill_local_mock(
        passenger,
        address,
        email,
        screenshot,
        synthetic_guest_number=synthetic_guest_number,
    )
    message_count = await poll_test_inbox(inbox_token, inbox_wait_seconds)
    return {
        "booking_id": booking_id,
        "capture_timestamp": capture.get("captured_at"),
        "passenger_index": passenger_index,
        "adult_verified": True,
        "address": address,
        "temporary_email": email,
        "synthetic_guest_number": synthetic_guest_number,
        "synthetic_phone_number": synthetic_phone_number,
        "inbox_messages_received": message_count,
        "mock_form_verified": form_result,
        "screenshot": str(screenshot.resolve()),
        "target": "local mock only",
        "live_msc_form_opened": False,
        "email_sent": False,
        "form_submitted": False,
        "capture_corpus_modified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise status-match QA locally or prepare a live application without submitting."
    )
    parser.add_argument(
        "--mode",
        choices=("qa", "prepare"),
        default="qa",
        help="qa keeps the existing local-mock workflow; prepare uses verified live records",
    )
    parser.add_argument("--booking", required=True, help="MSC booking ID already present in the capture corpus")
    parser.add_argument("--passenger-index", type=int, help="1-based passenger number from latest capture (qa mode)")
    parser.add_argument("--state", default="Oklahoma", help="US state for the synthetic address (default: Oklahoma)")
    parser.add_argument("--screenshot", type=Path, help="path for the local QA screenshot")
    parser.add_argument("--inbox-wait-seconds", type=int, default=10, help="Mail.tm inbox poll limit (default: 10)")
    parser.add_argument("--guest-number", help="actual customer-provided Carnival Guest Number (prepare mode)")
    parser.add_argument("--contact-data", type=Path, help="private customer/CRM contact JSON (prepare mode)")
    parser.add_argument("--loyalty-company", help="exact provider label available in MSC's form (prepare mode)")
    parser.add_argument("--output-dir", type=Path, help="private output folder outside the project (prepare mode)")
    parser.add_argument("--login-timeout-minutes", type=float, default=15)
    args = parser.parse_args()

    try:
        if args.mode == "qa":
            if args.passenger_index is None:
                parser.error("--passenger-index is required in qa mode")
            if args.screenshot is None:
                parser.error("--screenshot is required in qa mode")
            if args.inbox_wait_seconds < 0 or args.inbox_wait_seconds > 120:
                parser.error("--inbox-wait-seconds must be between 0 and 120")
            state_name = next(
                (name for name in STATE_CODES if name.casefold() == args.state.casefold()),
                None,
            )
            if state_name is None:
                parser.error(f"Unsupported US state: {args.state}")
            result = asyncio.run(run_workflow(
                args.booking,
                args.passenger_index,
                state_name,
                args.screenshot.expanduser(),
                args.inbox_wait_seconds,
            ))
        else:
            missing = [
                option for option, value in (
                    ("--guest-number", args.guest_number),
                    ("--contact-data", args.contact_data),
                    ("--loyalty-company", args.loyalty_company),
                    ("--output-dir", args.output_dir),
                )
                if value is None
            ]
            if missing:
                parser.error("prepare mode requires " + ", ".join(missing))
            if args.login_timeout_minutes < 0 or args.login_timeout_minutes > 60:
                parser.error("--login-timeout-minutes must be between 0 and 60")
            result = asyncio.run(run_live_preparation(
                args.booking,
                args.guest_number,
                args.contact_data,
                args.loyalty_company,
                args.output_dir,
                args.login_timeout_minutes,
            ))
    except (OSError, ValueError, LookupError, RuntimeError) as exc:
        parser.error(str(exc))
        return

    print(json.dumps(result, indent=2))
    if result.get("status") == "failed":
        raise SystemExit(1)


async def run_live_preparation(
    booking_id: str,
    guest_number: str,
    contact_data: Path,
    loyalty_company: str,
    output_dir: Path,
    login_timeout_minutes: float = 15,
) -> dict[str, Any]:
    """Add the verified live workflow while preserving the original QA API."""
    from MSC_Status_Match_Toolkit.msc_status_match_workflow import run_live_workflow

    return await run_live_workflow(
        booking_id,
        guest_number,
        contact_data,
        loyalty_company,
        output_dir,
        login_timeout_minutes,
    )


if __name__ == "__main__":
    main()

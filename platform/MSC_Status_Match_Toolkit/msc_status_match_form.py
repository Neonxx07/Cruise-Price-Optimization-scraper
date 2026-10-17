"""Prefill MSC's status-match form from applicant-reviewed local data.

This helper never submits the form. It does not create or invent applicant
data, and it leaves the signature and consent controls for the applicant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Route, async_playwright

FORM_URL = "https://www.msccruisesusa.com/msc-voyagers-club/status-match"
ALLOWED_COUNTRIES = {"US", "PR"}
ALLOWED_CARD_SUFFIXES = {".jpg", ".jpeg", ".png", ".doc", ".docx", ".pdf"}
MAX_CARD_BYTES = 4 * 1024 * 1024


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string in the local data file")
    return value.strip()


def validate_form_data(raw: Any) -> dict[str, Any]:
    """Validate supplied values without logging or modifying them."""
    if not isinstance(raw, dict):
        raise ValueError("The data file must contain a JSON object")

    allowed_keys = {
        "first_name",
        "last_name",
        "date_of_birth",
        "mobile_number",
        "email",
        "country_code",
        "zip_code",
        "city",
        "loyalty_company",
        "loyalty_status",
        "membership_number",
        "card_proof_path",
    }
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        raise ValueError(f"Unsupported data fields: {', '.join(sorted(unknown_keys))}")

    data = {
        "first_name": _required_text(raw, "first_name"),
        "last_name": _required_text(raw, "last_name"),
        "date_of_birth": _required_text(raw, "date_of_birth"),
        "mobile_number": _required_text(raw, "mobile_number"),
        "email": _required_text(raw, "email"),
        "country_code": _required_text(raw, "country_code").upper(),
        "zip_code": _required_text(raw, "zip_code"),
    }
    try:
        parsed_dob = datetime.strptime(data["date_of_birth"], "%m/%d/%Y")
    except ValueError as exc:
        raise ValueError("'date_of_birth' must use MM/DD/YYYY") from exc
    if parsed_dob.strftime("%m/%d/%Y") != data["date_of_birth"]:
        raise ValueError("'date_of_birth' must use zero-padded MM/DD/YYYY")
    if data["country_code"] not in ALLOWED_COUNTRIES:
        raise ValueError("'country_code' must be US or PR")
    if not re.fullmatch(r"\d{5}", data["zip_code"]):
        raise ValueError("'zip_code' must be exactly five digits")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", data["email"]):
        raise ValueError("'email' must be a valid email address")
    phone_digits = re.sub(r"\D", "", data["mobile_number"])
    if len(phone_digits) not in (10, 11) or (
        len(phone_digits) == 11 and not phone_digits.startswith("1")
    ):
        raise ValueError("'mobile_number' must contain a 10-digit US/PR number, optionally prefixed by 1")

    city = raw.get("city", "")
    if not isinstance(city, str):
        raise ValueError("'city' must be a string when supplied")
    data["city"] = city.strip()

    loyalty_keys = ("loyalty_company", "loyalty_status", "membership_number", "card_proof_path")
    loyalty_values = {key: raw.get(key) for key in loyalty_keys}
    if any(value not in (None, "") for value in loyalty_values.values()):
        for key in loyalty_keys:
            if not isinstance(loyalty_values[key], str) or not loyalty_values[key].strip():
                raise ValueError(
                    "Provide loyalty_company, loyalty_status, membership_number, and card_proof_path "
                    "together using verified applicant data"
                )
        proof_path = Path(loyalty_values["card_proof_path"]).expanduser()
        if not proof_path.is_file():
            raise ValueError("'card_proof_path' must point to an existing card image/document")
        if proof_path.suffix.lower() not in ALLOWED_CARD_SUFFIXES:
            raise ValueError("Card proof must be JPG, JPEG, PNG, DOC, DOCX, or PDF")
        if proof_path.stat().st_size > MAX_CARD_BYTES:
            raise ValueError("Card proof must not exceed 4 MB")
        data.update({key: value.strip() for key, value in loyalty_values.items()})
        data["card_proof_path"] = str(proof_path.resolve())
    else:
        data.update(dict.fromkeys(loyalty_keys, ""))
    return data


async def prefill_form(
    data: dict[str, Any],
    screenshot: Path | None = None,
    before_screenshot: Path | None = None,
) -> dict[str, str]:
    """Fill supplied fields in a headless browser and stop before submission."""
    for path in (screenshot, before_screenshot):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.add_init_script(
                """const isStatusMatchForm = form =>
                        form instanceof HTMLFormElement &&
                        form.querySelector('[name="existingMembership.company"]');
                    document.addEventListener('submit', event => {
                        if (!isStatusMatchForm(event.target)) return;
                        event.preventDefault();
                        if (event.submitter?.matches('.button-zip-code')) return;
                        event.stopImmediatePropagation();
                    }, true);
                    document.addEventListener('click', event => {
                        const button = event.target.closest('button, input[type="submit"]');
                        const form = button?.closest('form');
                        const label = (button?.innerText || button?.value || '').trim();
                        if (form?.querySelector('[name="existingMembership.company"]') &&
                            /^(confirm|submit)$/i.test(label)) {
                            event.preventDefault();
                            event.stopImmediatePropagation();
                        }
                    }, true);"""
                + """const nativeSubmit = HTMLFormElement.prototype.submit;
                    HTMLFormElement.prototype.submit = function() {
                        if (isStatusMatchForm(this)) return;
                        return nativeSubmit.apply(this, arguments);
                    };"""
            )
            initial_document = True

            async def block_application_post(route: Route) -> None:
                nonlocal initial_document
                request = route.request
                parsed_url = urlsplit(request.url)
                is_application_url = (
                    parsed_url.hostname == "www.msccruisesusa.com"
                    and parsed_url.path.rstrip("/") == "/msc-voyagers-club/status-match"
                )
                if not is_application_url:
                    await route.continue_()
                    return
                if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                    await route.abort()
                    return
                submitted_fields = {
                    "customer.name",
                    "customer.surname",
                    "customer.dateOfBirth",
                    "customer.mobilePhone:tel",
                    "customer.email",
                    "customer.address.countryCode",
                    "customer.address.postCode",
                    "customer.address.town",
                    "existingMembership.company",
                    "existingMembership.status",
                    "existingMembership.number",
                }
                if submitted_fields.intersection(parse_qs(parsed_url.query)):
                    await route.abort()
                    return
                if request.resource_type == "document":
                    if not initial_document:
                        await route.abort()
                        return
                    initial_document = False
                await route.continue_()

            await page.route("**/*", block_application_post)
            response = await page.goto(FORM_URL, wait_until="domcontentloaded", timeout=60_000)
            if response is None or response.status >= 400:
                status = response.status if response else "no response"
                raise RuntimeError(f"MSC Status Match form failed to load (HTTP {status})")

            form = page.locator("form").filter(
                has=page.locator('[name="existingMembership.company"]')
            ).first
            await form.wait_for(state="visible", timeout=20_000)
            if before_screenshot is not None:
                await page.screenshot(path=str(before_screenshot), full_page=True)

            phone_country = form.get_by_role("combobox", name="Selected country")
            if "+1" not in await phone_country.inner_text():
                raise RuntimeError(
                    "The form's phone country selector is not set to United States/Puerto Rico (+1)"
                )

            await form.locator('[name="customer.address.countryCode"]').select_option(
                data["country_code"]
            )
            await form.get_by_placeholder("Enter your first name").fill(data["first_name"])
            await form.get_by_placeholder("Enter your last name").fill(data["last_name"])
            await form.locator('input[type="text"][placeholder="mm/dd/yyyy" i]').fill(
                data["date_of_birth"]
            )
            await form.get_by_placeholder("Enter your mobile number").fill(data["mobile_number"])
            await form.get_by_placeholder("Enter your email").fill(data["email"])
            await form.get_by_placeholder("Enter your zip code").fill(data["zip_code"])
            await form.locator(".button-zip-code").click(force=True)

            await page.wait_for_function(
                """() => {
                    const form = [...document.forms].find(item =>
                        item.querySelector('[name="existingMembership.company"]')
                    );
                    const city = form?.querySelector('[name="customer.address.town"]');
                    return city && [...city.options].some(option => option.value);
                }""",
                timeout=20_000,
            )
            city_select = form.locator('[name="customer.address.town"]')
            city_options = await city_select.locator("option").evaluate_all(
                "options => options.filter(option => option.value).map(option => ({value: option.value, label: option.textContent.trim()}))"
            )
            if data["city"]:
                matches = [
                    option
                    for option in city_options
                    if option["label"].casefold() == data["city"].casefold()
                ]
                if len(matches) != 1:
                    raise ValueError("The supplied city is not an exact option for this ZIP code")
                await city_select.select_option(matches[0]["value"])
            elif len(city_options) == 1:
                await city_select.select_option(city_options[0]["value"])
            else:
                raise ValueError("ZIP lookup returned multiple cities; supply the applicant's exact city")

            if data["loyalty_company"]:
                company = form.locator('[name="existingMembership.company"]')
                await company.select_option(label=data["loyalty_company"])
                status_select = form.locator('[name="existingMembership.status"]')
                status_options = await status_select.locator("option").all_text_contents()
                if data["loyalty_status"] not in [item.strip() for item in status_options]:
                    raise ValueError("The supplied loyalty status is not available for that company")
                await status_select.select_option(label=data["loyalty_status"])
                await form.locator('[name="existingMembership.number"]').fill(
                    data["membership_number"]
                )
                await form.locator('[name="attachment:file"]').set_input_files(
                    data["card_proof_path"]
                )

            verified = await form.evaluate(
                """form => {
                    const value = selector => form.querySelector(selector)?.value ?? '';
                    const city = form.querySelector('[name="customer.address.town"]');
                    const visibleDob = form.querySelector('input[type="text"][placeholder="mm/dd/yyyy" i]');
                    return {
                        first_name: value('[name="customer.name"]'),
                        last_name: value('[name="customer.surname"]'),
                        date_of_birth: visibleDob?.value ?? '',
                        normalized_date_of_birth: value('[name="customer.dateOfBirth"]'),
                        mobile_number: value('[name="customer.mobilePhone:tel"]'),
                        email: value('[name="customer.email"]'),
                        country_code: value('[name="customer.address.countryCode"]'),
                        zip_code: value('[name="customer.address.postCode"]'),
                        city: city?.selectedOptions?.[0]?.textContent.trim() ?? '',
                        loyalty_company: value('[name="existingMembership.company"]'),
                        loyalty_company_label: form.querySelector('[name="existingMembership.company"]')?.selectedOptions?.[0]?.textContent.trim() ?? '',
                        loyalty_status: value('[name="existingMembership.status"]'),
                        loyalty_status_label: form.querySelector('[name="existingMembership.status"]')?.selectedOptions?.[0]?.textContent.trim() ?? '',
                        membership_number: value('[name="existingMembership.number"]'),
                        card_proof_file: form.querySelector('[name="attachment:file"]')?.files?.[0]?.name ?? '',
                        signature: value('[name="signature_skip"]'),
                        marketing_consent: Boolean(form.querySelector('[name="genericMarketingConsent_skip:boolean"]')?.checked),
                        personalized_consent: Boolean(form.querySelector('[name="profilingConsents"]')?.checked)
                    };
                }"""
            )
            expected = {
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "date_of_birth": data["date_of_birth"],
                "normalized_date_of_birth": datetime.strptime(
                    data["date_of_birth"], "%m/%d/%Y"
                ).strftime("%Y-%m-%d"),
                "mobile_number": data["mobile_number"],
                "email": data["email"],
                "country_code": data["country_code"],
                "zip_code": data["zip_code"],
            }
            mismatches = [
                key for key, value in expected.items() if verified.get(key) != value
            ]
            expected_phone = re.sub(r"\D", "", data["mobile_number"])
            actual_phone = re.sub(r"\D", "", verified["mobile_number"])
            if expected_phone.startswith("1") and len(expected_phone) == 11:
                expected_phone = expected_phone[1:]
            if actual_phone.startswith("1") and len(actual_phone) == 11:
                actual_phone = actual_phone[1:]
            if expected_phone != actual_phone:
                mismatches.append("mobile_number")
            if mismatches:
                raise RuntimeError(
                    "MSC form values failed verification for: " + ", ".join(mismatches)
                )
            if not verified["city"]:
                raise RuntimeError("MSC ZIP lookup did not resolve a city")
            if verified["signature"] or verified["marketing_consent"] or verified["personalized_consent"]:
                raise RuntimeError("Signature or optional consent was unexpectedly populated")
            if data["loyalty_company"]:
                loyalty_expected = {
                    "loyalty_company_label": data["loyalty_company"],
                    "loyalty_status_label": data["loyalty_status"],
                    "membership_number": data["membership_number"],
                    "card_proof_file": Path(data["card_proof_path"]).name,
                }
                loyalty_mismatches = [
                    key for key, value in loyalty_expected.items() if verified.get(key) != value
                ]
                if loyalty_mismatches:
                    raise RuntimeError(
                        "Loyalty fields failed verification for: "
                        + ", ".join(loyalty_mismatches)
                    )

            if screenshot is not None:
                await page.screenshot(path=str(screenshot), full_page=True)

            return {
                "first_name": verified["first_name"],
                "last_name": verified["last_name"],
                "date_of_birth": verified["date_of_birth"],
                "mobile_number": verified["mobile_number"],
                "email": verified["email"],
                "country_code": verified["country_code"],
                "zip_code": verified["zip_code"],
                "city": verified["city"],
                "loyalty_details_filled": "yes" if data["loyalty_company"] else "no",
                "form_submitted": "no",
            }
        finally:
            await browser.close()


async def prefill_verified_identity(
    identity: dict[str, str],
    screenshot: Path,
) -> dict[str, str]:
    """Fill only booking-verified name/DOB on MSC and block application writes."""
    if not isinstance(identity, dict) or set(identity) != {
        "first_name", "last_name", "date_of_birth"
    }:
        raise ValueError("Identity preview accepts only first_name, last_name, and date_of_birth")
    for key, value in identity.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{key}' must be a non-empty verified string")
    try:
        parsed_dob = datetime.strptime(identity["date_of_birth"], "%m/%d/%Y")
    except ValueError as exc:
        raise ValueError("'date_of_birth' must use MM/DD/YYYY") from exc
    if parsed_dob.strftime("%m/%d/%Y") != identity["date_of_birth"]:
        raise ValueError("'date_of_birth' must use zero-padded MM/DD/YYYY")

    screenshot.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            page.set_default_timeout(20_000)
            page.set_default_navigation_timeout(60_000)
            await page.add_init_script(
                """document.addEventListener('submit', event => {
                        const form = event.target;
                        if (form instanceof HTMLFormElement &&
                            form.querySelector('[name="existingMembership.company"]')) {
                            event.preventDefault();
                            event.stopImmediatePropagation();
                        }
                    }, true);
                    document.addEventListener('click', event => {
                        const button = event.target.closest('button, input[type="submit"]');
                        const label = (button?.innerText || button?.value || '').trim();
                        if (/^(confirm|submit)$/i.test(label)) {
                            event.preventDefault();
                            event.stopImmediatePropagation();
                        }
                    }, true);
                    for (const method of ['submit', 'requestSubmit']) {
                        const original = HTMLFormElement.prototype[method];
                        HTMLFormElement.prototype[method] = function() {
                            if (this.querySelector('[name="existingMembership.company"]')) return;
                            return original.apply(this, arguments);
                        };
                    }"""
            )
            initial_document = True

            async def block_application_writes(route: Route) -> None:
                nonlocal initial_document
                request = route.request
                parsed_url = urlsplit(request.url)
                is_form = (
                    parsed_url.hostname == "www.msccruisesusa.com"
                    and parsed_url.path.rstrip("/") == "/msc-voyagers-club/status-match"
                )
                if not is_form:
                    await route.continue_()
                    return
                if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                    await route.abort()
                    return
                if parse_qs(parsed_url.query).keys() & {
                    "customer.name",
                    "customer.surname",
                    "customer.dateOfBirth",
                    "customer.mobilePhone:tel",
                    "customer.email",
                    "customer.address.postCode",
                    "existingMembership.number",
                }:
                    await route.abort()
                    return
                if request.resource_type == "document":
                    if not initial_document:
                        await route.abort()
                        return
                    initial_document = False
                await route.continue_()

            await page.route("**/*", block_application_writes)
            response = await page.goto(FORM_URL, wait_until="domcontentloaded")
            if response is None or response.status >= 400:
                status = response.status if response else "no response"
                raise RuntimeError(f"MSC Status Match form failed to load (HTTP {status})")

            form = page.locator("form").filter(
                has=page.locator('[name="existingMembership.company"]')
            ).first
            await form.wait_for(state="visible")
            await form.get_by_placeholder("Enter your first name").fill(identity["first_name"])
            await form.get_by_placeholder("Enter your last name").fill(identity["last_name"])
            dob = form.locator('input[type="text"][placeholder="mm/dd/yyyy" i]')
            await dob.fill(identity["date_of_birth"])
            await dob.press("Tab")
            await page.screenshot(path=str(screenshot), full_page=True)
            expected_normalized_dob = parsed_dob.strftime("%Y-%m-%d")
            await page.wait_for_timeout(250)

            verified = await form.evaluate(
                """form => ({
                    first_name: form.querySelector('[name="customer.name"]')?.value ?? '',
                    last_name: form.querySelector('[name="customer.surname"]')?.value ?? '',
                    date_of_birth: form.querySelector('input[type="text"][placeholder="mm/dd/yyyy" i]')?.value ?? '',
                    normalized_date_of_birth: form.querySelector('[name="customer.dateOfBirth"]')?.value ?? '',
                    loyalty_company: form.querySelector('[name="existingMembership.company"]')?.value ?? '',
                    loyalty_status: form.querySelector('[name="existingMembership.status"]')?.value ?? '',
                    membership_number: form.querySelector('[name="existingMembership.number"]')?.value ?? '',
                    card_proof_selected: Boolean(form.querySelector('[name="attachment:file"]')?.files?.length),
                    signature: form.querySelector('[name="signature_skip"]')?.value ?? '',
                    consent_checked: Boolean(
                        form.querySelector('[name="genericMarketingConsent_skip:boolean"]')?.checked ||
                        form.querySelector('[name="profilingConsents"]')?.checked
                    )
                })"""
            )
            expected = {
                "first_name": identity["first_name"],
                "last_name": identity["last_name"],
                "date_of_birth": identity["date_of_birth"],
                "normalized_date_of_birth": expected_normalized_dob,
            }
            mismatch = [key for key, value in expected.items() if verified[key] != value]
            if mismatch:
                raise RuntimeError("MSC identity fields failed verification: " + ", ".join(mismatch))
            if any(verified[key] for key in (
                "loyalty_company",
                "loyalty_status",
                "membership_number",
                "card_proof_selected",
                "signature",
                "consent_checked",
            )):
                raise RuntimeError("An unprovided loyalty, proof, signature, or consent field was populated")

            return {
                "verified_fields": "first_name,last_name,date_of_birth",
                "loyalty_fields_filled": "no",
                "signature_or_consent_filled": "no",
                "submission_blocked": "yes",
                "form_submitted": "no",
                "current_url": page.url,
            }
        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prefill MSC Status Match with applicant-reviewed local data; never submits."
    )
    parser.add_argument("--data", required=True, help="local JSON file with verified applicant data")
    parser.add_argument(
        "--screenshot",
        help="optional path for a full-page screenshot containing the entered personal data",
    )
    parser.add_argument(
        "--before-screenshot",
        help="optional path for a full-page screenshot before any fields are filled",
    )
    args = parser.parse_args()

    try:
        raw = json.loads(Path(args.data).read_text(encoding="utf-8"))
        data = validate_form_data(raw)
        result = asyncio.run(
            prefill_form(
                data,
                Path(args.screenshot).expanduser() if args.screenshot else None,
                Path(args.before_screenshot).expanduser()
                if args.before_screenshot else None,
            )
        )
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
        return

    print("MSC Status Match fields filled and verified in headless Chromium.")
    print("The form was NOT submitted. Review any saved screenshot before taking further action.")
    print("Verified fields: " + ", ".join(result))
    if args.screenshot:
        print(f"Screenshot saved to: {Path(args.screenshot).expanduser()}")
    if args.before_screenshot:
        print(f"Before screenshot saved to: {Path(args.before_screenshot).expanduser()}")


if __name__ == "__main__":
    main()

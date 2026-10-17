"""Unified status-match workflow with separate live and synthetic-QA paths.

The live path reads an MSC booking, looks up the supplied Carnival Guest
Number in GoCCL, captures the unedited account view, and pre-fills MSC using
customer/CRM contact data. It never clicks Submit or Confirm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLATFORM_ROOT.parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from core.vifp_guest import (  # noqa: E402
    CarnivalGuestInfo,
    match_msc_passenger,
    normalise_guest_number,
    parse_date,
)
from scraper.goccl import GoCCLScraper  # noqa: E402
from services.msc_live_service import MscLiveService  # noqa: E402
from MSC_Status_Match_Toolkit.msc_status_match_form import (  # noqa: E402
    prefill_form,
    validate_form_data,
)
from utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)
CONTACT_FIELDS = {"mobile_number", "email", "country_code", "zip_code", "city"}
SYNTHETIC_GUEST_NUMBER = re.compile(r"90\d{8}")

# The DOB used by the original fixed QA example. Kept only so the local QA
# report can flag when a validated capture disagrees with it; the capture is
# always the source of truth and this value is never applied to a preview.
LEGACY_QA_EXAMPLE_DOB = "January 15, 1990"


def _load_goccl_filler(path: Path):
    """Import the local GoCCL template filler by path so its existing
    template-fill, screenshot, and synthetic-number functions are reused."""
    import importlib.util

    if not path.is_file():
        raise FileNotFoundError(f"The saved local GoCCL filler is unavailable: {path}")
    spec = importlib.util.spec_from_file_location("goccl_template_fill_local_qa", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the local GoCCL filler: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    platform = PLATFORM_ROOT.resolve()
    if resolved == platform or platform in resolved.parents:
        raise ValueError("Output must be outside the project to keep customer screenshots private")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _load_contact_data(path: Path) -> dict[str, str]:
    raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Contact data must be a JSON object")
    unknown = set(raw) - CONTACT_FIELDS
    missing = CONTACT_FIELDS - set(raw)
    if unknown:
        raise ValueError(f"Unsupported contact fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"Missing customer/CRM contact fields: {', '.join(sorted(missing))}")
    if any(not isinstance(raw[key], str) or not raw[key].strip() for key in CONTACT_FIELDS):
        raise ValueError("Every customer/CRM contact field must be a non-empty string")
    return {key: raw[key].strip() for key in CONTACT_FIELDS}


def _passenger_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    passengers = record.get("passengers")
    if not isinstance(passengers, list):
        return []
    rows = []
    for passenger in passengers:
        if not isinstance(passenger, dict):
            continue
        name = passenger.get("name")
        if not isinstance(name, str) or not name.strip():
            name = " ".join(
                str(passenger.get(key, "")).strip()
                for key in ("first_name", "middle_name", "last_name")
                if passenger.get(key)
            )
        rows.append({**passenger, "name": name})
    return rows


def _matched_identity(record: dict[str, Any], matched: dict[str, Any]) -> dict[str, str]:
    passenger = next(
        (
            row for row in _passenger_rows(record)
            if row.get("name") == matched.get("name")
            and row.get("dob") == matched.get("dob")
        ),
        None,
    )
    if passenger is None:
        raise RuntimeError("The matched MSC passenger is missing from the capture")
    age = passenger.get("age")
    if not isinstance(age, int) or isinstance(age, bool) or age < 18:
        raise ValueError("Selected status-match applicant is not verified as an adult")
    first = passenger.get("first_name")
    last = passenger.get("last_name")
    dob = parse_date(passenger.get("dob"))
    if not all(isinstance(value, str) and value.strip() for value in (first, last)) or dob is None:
        raise ValueError("Matched MSC passenger lacks separately verified name fields or a readable DOB")
    return {
        "first_name": first.strip(),
        "last_name": last.strip(),
        "date_of_birth": dob.strftime("%m/%d/%Y"),
    }


def _safe_error(exc: Exception, sensitive: list[str]) -> str:
    message = str(exc)
    for value in sorted((item for item in sensitive if item), key=len, reverse=True):
        message = re.sub(re.escape(value), "[REDACTED]", message, flags=re.IGNORECASE)
    return f"{type(exc).__name__}: {message[:400]}"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def run_live_workflow(
    booking_id: str,
    guest_number: str,
    contact_data_path: Path,
    loyalty_company: str,
    output_dir: Path,
    login_timeout_minutes: float = 15,
    *,
    msc_service_factory: Callable[[], Any] = MscLiveService,
    goccl_scraper_factory: Callable[[], Any] = GoCCLScraper,
    form_prefill: Callable[..., Any] = prefill_form,
) -> dict[str, Any]:
    """Run live read-only capture and form preparation; never submit."""
    booking_id = str(booking_id).strip()
    normalized_guest_number = normalise_guest_number(guest_number)
    if not re.fullmatch(r"\d{6,9}", booking_id):
        raise ValueError("MSC booking ID must be 6-9 digits")
    if not normalized_guest_number:
        raise ValueError("Provide the customer's actual Carnival Guest Number")
    if SYNTHETIC_GUEST_NUMBER.fullmatch(normalized_guest_number):
        raise ValueError(
            "This Guest Number uses the reserved synthetic-test pattern; "
            "the live workflow will not send it to GoCCL or MSC"
        )
    if not isinstance(loyalty_company, str) or not loyalty_company.strip():
        raise ValueError("Select the Carnival loyalty provider label used by the MSC form")

    output = _private_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    proof_path = output / f"vifp_account_{booking_id}_{stamp}.png"
    before_path = output / f"msc_form_before_{booking_id}_{stamp}.png"
    after_path = output / f"msc_form_after_{booking_id}_{stamp}.png"
    report_path = output / f"status_match_{booking_id}_{stamp}.json"
    report: dict[str, Any] = {
        "workflow": "msc_status_match",
        "booking_id": booking_id,
        "started_at": datetime.now().isoformat(),
        "status": "running",
        "steps": {
            "msc_login": "pending",
            "msc_passenger_capture": "pending",
            "vifp_lookup_and_proof": "pending",
            "passenger_identity_match": "pending",
            "contact_data_validation": "pending",
            "msc_form_prefill_and_verification": "pending",
            "human_review": "required",
        },
        "artifacts": {
            "vifp_account_proof": str(proof_path),
            "msc_form_before": str(before_path),
            "msc_form_after": str(after_path),
            "execution_report": str(report_path),
        },
        "submission": "NOT SUBMITTED; no Submit or Confirm action was called.",
        "error": None,
    }
    sensitive: list[str] = [normalized_guest_number]
    msc = None
    goccl = None
    error: Exception | None = None

    try:
        msc = msc_service_factory()
        if not await msc.check_login(timeout_minutes=login_timeout_minutes):
            raise RuntimeError("MSC login was not completed")
        report["steps"]["msc_login"] = "complete"

        record = await msc.capture_passengers(booking_id)
        report["steps"]["msc_passenger_capture"] = "complete"
        report["msc_capture_timestamp"] = record.get("captured_at")
        sensitive.extend(
            str(value)
            for passenger in record.get("passengers", [])
            if isinstance(passenger, dict)
            for value in (
                passenger.get("first_name"),
                passenger.get("last_name"),
                passenger.get("name"),
            )
            if value
        )

        goccl = goccl_scraper_factory()
        await goccl.start()
        login_result = await goccl.auto_login()
        if login_result not in {"OK", "ALREADY_LOGGED_IN"}:
            raise RuntimeError(f"GoCCL login did not complete: {login_result}")
        await goccl.open_vifp_lookup()
        guest: CarnivalGuestInfo = await goccl.lookup_vifp_guest(
            normalized_guest_number, screenshot_path=str(proof_path)
        )
        if guest.guest_number != normalized_guest_number:
            raise RuntimeError("GoCCL's account view did not verify the supplied guest number")
        if not proof_path.is_file() or proof_path.stat().st_size == 0:
            raise RuntimeError("GoCCL account-proof screenshot was not saved")
        report["steps"]["vifp_lookup_and_proof"] = "complete"
        report["verified_vifp_tier"] = guest.tier

        match = match_msc_passenger(guest, _passenger_rows(record))
        if match.passenger is None:
            raise RuntimeError(f"VIFP record did not match one MSC passenger: {match.reason}")
        identity = _matched_identity(record, match.passenger)
        sensitive.extend(identity.values())
        report["steps"]["passenger_identity_match"] = "complete"
        report["matched_passenger_index"] = next(
            index
            for index, row in enumerate(_passenger_rows(record), 1)
            if row.get("name") == match.passenger.get("name")
            and row.get("dob") == match.passenger.get("dob")
        )

        contact = _load_contact_data(contact_data_path)
        sensitive.extend(contact.values())
        report["steps"]["contact_data_validation"] = "complete"
        form_data = validate_form_data({
            **identity,
            **contact,
            "loyalty_company": loyalty_company.strip(),
            "loyalty_status": guest.tier.title(),
            "membership_number": guest.guest_number,
            "card_proof_path": str(proof_path),
        })

        result = await form_prefill(
            form_data,
            screenshot=after_path,
            before_screenshot=before_path,
        )
        report["steps"]["msc_form_prefill_and_verification"] = "complete"
        report["verified_form_fields"] = sorted(result)
        report["status"] = "ready_for_human_review"
    except Exception as exc:
        error = exc
        report["status"] = "failed"
        report["error"] = _safe_error(exc, sensitive)
        failed_step = next(
            (key for key, value in report["steps"].items()
             if value == "pending" and key != "human_review"),
            "human_review",
        )
        report["steps"][failed_step] = "failed"
        logger.error("status_match.workflow_failed", booking_id=booking_id,
                     step=failed_step, error=type(exc).__name__)
    finally:
        if goccl is not None:
            try:
                await goccl.stop()
            except Exception as exc:
                report["cleanup_error"] = _safe_error(exc, sensitive)
                report["status"] = "failed"
                if error is None:
                    error = exc
        if msc is not None:
            try:
                await msc.stop()
            except Exception as exc:
                report["cleanup_error"] = _safe_error(exc, sensitive)
                report["status"] = "failed"
                if error is None:
                    error = exc
        report["finished_at"] = datetime.now().isoformat()
        _write_report(report_path, report)

    if error is not None:
        logger.error("status_match.workflow_report_saved", report=str(report_path))
    else:
        logger.info("status_match.workflow_ready_for_review", report=str(report_path))
    return report


async def run_identity_preview(
    booking_id: str,
    passenger_index: int,
    output_dir: Path,
    *,
    identity_prefill: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Prefill only captured name/DOB on MSC; leave contact and proof blank."""
    from MSC_Status_Match_Toolkit import msc_status_match_qa as qa

    output = _private_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    screenshot = output / f"msc_identity_preview_{booking_id}_{stamp}.png"
    report_path = output / f"msc_identity_preview_{booking_id}_{stamp}.json"
    report: dict[str, Any] = {
        "workflow": "msc_status_match_live_identity_preview",
        "booking_id": str(booking_id),
        "passenger_index": passenger_index,
        "started_at": datetime.now().isoformat(),
        "status": "running",
        "steps": {
            "existing_booking_capture_read": "pending",
            "adult_and_identity_validation": "pending",
            "live_msc_form_load": "pending",
            "verified_identity_fields_filled": "pending",
            "contact_loyalty_proof_signature_and_consent": "left_blank",
            "submission": "blocked; not submitted",
        },
        "artifacts": {
            "filled_live_page_screenshot": str(screenshot),
            "execution_report": str(report_path),
        },
        "error": None,
    }
    try:
        capture, passenger = qa.load_passenger(booking_id, passenger_index)
        report["steps"]["existing_booking_capture_read"] = "complete"
        identity = {
            "first_name": passenger["first_name"],
            "last_name": passenger["last_name"],
            "date_of_birth": passenger["date_of_birth"],
        }
        if identity["first_name"].strip().upper() != "JEREMY":
            raise ValueError("Selected passenger is not RILEY DEMOGUEST")
        if identity["last_name"].strip().upper() != "WEBB":
            raise ValueError("Selected passenger is not RILEY DEMOGUEST")
        report["steps"]["adult_and_identity_validation"] = "complete"
        report["capture_timestamp"] = capture.get("captured_at")
        report["identity_source"] = "existing MSC booking capture"
        report["filled_fields"] = ["first_name", "last_name", "date_of_birth"]
        report["contact_fields_filled"] = []
        report["loyalty_fields_filled"] = []
        report["consent_action"] = "No cookie, marketing, profiling, or terms consent was clicked."
        prefill = identity_prefill
        if prefill is None:
            from MSC_Status_Match_Toolkit.msc_status_match_form import (
                prefill_verified_identity,
            )
            prefill = prefill_verified_identity
        result = await prefill(identity, screenshot)
        report["steps"]["live_msc_form_load"] = "complete"
        report["steps"]["verified_identity_fields_filled"] = "complete"
        report["form_result"] = result
        report["status"] = "identity_preview_complete_not_application_ready"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = _safe_error(exc, [])
        failed_step = next(
            (step for step, status in report["steps"].items()
             if status == "pending"),
            None,
        )
        if failed_step:
            report["steps"][failed_step] = "failed"
        logger.error(
            "status_match.identity_preview_failed",
            booking_id=str(booking_id),
            error=type(exc).__name__,
        )
    finally:
        report["finished_at"] = datetime.now().isoformat()
        _write_report(report_path, report)
    return report


async def run_local_qa(
    booking_id: str,
    passenger_index: int,
    state: str,
    output_dir: Path,
    inbox_wait_seconds: int = 10,
    *,
    filler_module: Any | None = None,
    mock_workflow: Callable[..., Any] | None = None,
    template_path: Path | None = None,
    capture_path: Path | None = None,
    guest_number_history_path: Path | None = None,
    scan_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the local mock and a Carnival-template preview from the latest
    valid MSC passenger capture. Opens neither live application, submits
    nothing, and never sends the synthetic preview to Carnival or MSC.

    Identity comes from the validated GoCCL MSC-capture adapter. A synthetic
    guest number and contact data are recorded separately and sent only to
    the local mock. No loyalty tier is asserted, and the local template's
    pre-existing GOLD badge is visibly labeled unverified.
    """
    output = _private_output_dir(output_dir)
    from MSC_Status_Match_Toolkit import msc_status_match_qa as qa

    if mock_workflow is None:
        mock_workflow = qa.run_workflow

    filler_path = WORKSPACE_ROOT / "GoCCL_Guest_Info" / "goccl_template_fill.py"
    template = template_path or (WORKSPACE_ROOT / "GoCCL_Guest_Info" / "GoCCL_Template.htm")
    if filler_module is None:
        filler_module = _load_goccl_filler(filler_path)
    if not template.is_file():
        raise FileNotFoundError(f"The saved local GoCCL template is unavailable: {template}")

    corpus = capture_path or Path(getattr(qa, "BOOKING_DATA_PATH"))
    passengers = filler_module.load_msc_passengers(corpus, booking_id, passenger_index)
    if len(passengers) != 1:
        raise RuntimeError("Expected exactly one validated passenger for the QA preview")
    identity = passengers[0]
    fresh_scan = scan_metadata is not None
    if fresh_scan:
        scanned_timestamp = str(scan_metadata.get("capture_timestamp") or "")
        adapted_timestamp = str(identity.get("_source_captured_at") or "")
        if not scanned_timestamp or scanned_timestamp != adapted_timestamp:
            raise RuntimeError(
                "The validated passenger adapter did not select the fresh MSC scan."
            )

    history_path = guest_number_history_path or Path(
        filler_module.GENERATED_GUEST_NUMBERS_PATH
    )
    generated_numbers = filler_module.generate_synthetic_guest_numbers(
        1, history_path=history_path
    )
    if len(generated_numbers) != 1 or not filler_module.SYNTHETIC_GUEST_NUMBER_PATTERN.fullmatch(
        generated_numbers[0]
    ):
        raise RuntimeError("Synthetic guest-number generator returned an invalid value.")
    synthetic_guest_number = generated_numbers[0]
    try:
        number_history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Generated guest number history was not saved correctly: {history_path}"
        ) from exc
    if synthetic_guest_number not in number_history:
        raise RuntimeError("Generated guest number was not persisted to its history file.")
    synthetic_phone = filler_module.randomize_phone_number()

    guest = {
        "title": identity["title"],
        "first_name": identity["first_name"],
        "last_name": identity["last_name"],
        "dob": identity["dob"],
        "state": identity["state"],
        "guest_number": synthetic_guest_number,
    }

    template_html = template.read_text(encoding="utf-8", errors="replace")
    document = filler_module.fill_guest(template_html, guest, None, qa_only=True)
    if (
        'id="qa-only-watermark"' not in document
        or "NOT CARNIVAL ACCOUNT EVIDENCE" not in document
    ):
        raise RuntimeError("QA-only template watermark was not added.")
    preview = output / f"qa_goccl_preview_{booking_id}_{passenger_index}.html"
    preview.write_text(document, encoding="utf-8")
    # The filler's create_screenshot uses Playwright's sync API, so it must run
    # outside this event loop.
    preview_screenshot = Path(
        await asyncio.to_thread(filler_module.create_screenshot, preview, guest, None)
    )

    mock_screenshot = output / "qa_local_form.png"
    mock_result = await mock_workflow(
        booking_id,
        passenger_index,
        state,
        mock_screenshot,
        inbox_wait_seconds,
        synthetic_guest_number=synthetic_guest_number,
        synthetic_phone_number=synthetic_phone,
    )
    if (
        mock_result.get("booking_id") != booking_id
        or mock_result.get("passenger_index") != passenger_index
        or mock_result.get("adult_verified") is not True
        or str(mock_result.get("capture_timestamp") or "")
        != str(identity.get("_source_captured_at") or "")
    ):
        raise RuntimeError(
            "The local mock did not use the same validated passenger capture."
        )
    if mock_result.get("form_submitted") is not False:
        raise RuntimeError("Local QA mock reported a form submission.")
    mock_form = mock_result.get("mock_form_verified")
    if not isinstance(mock_form, dict):
        raise RuntimeError("Local QA did not return verified mock-form values.")
    expected_dob = datetime.strptime(identity["dob"], "%B %d, %Y").strftime("%m/%d/%Y")
    expected_mock_values = {
        "first_name": identity["first_name"],
        "last_name": identity["last_name"],
        "date_of_birth": expected_dob,
        "mobile_number": synthetic_phone,
        "email": mock_result.get("temporary_email"),
        "loyalty_company": "QA ONLY - NOT A REAL PROGRAM",
        "loyalty_status": "UNVERIFIED QA PLACEHOLDER",
        "membership_number": synthetic_guest_number,
        "qa_only_warning_visible": True,
        "card_proof_selected": False,
        "signature": "",
        "marketing_checked": False,
        "personalized_checked": False,
        "confirm_disabled": True,
        "form_submitted": False,
    }
    mismatched_mock_values = [
        key for key, value in expected_mock_values.items()
        if mock_form.get(key) != value
    ]
    if mismatched_mock_values:
        raise RuntimeError(
            "Local mock did not verify all expected fields/safety controls: "
            + ", ".join(mismatched_mock_values)
        )
    address = mock_result.get("address")
    email = mock_result.get("temporary_email")
    if not isinstance(address, dict) or not isinstance(email, str) or not email.strip():
        raise RuntimeError("Synthetic contact data is incomplete.")
    required_address_fields = ("street", "city", "postcode", "phone", "country", "state")
    if any(
        not isinstance(address.get(key), str) or not address[key].strip()
        for key in required_address_fields
    ):
        raise RuntimeError("Synthetic contact address is incomplete.")
    if address["phone"] != synthetic_phone:
        raise RuntimeError("Synthetic phone number was not used in the local mock.")
    if not Path(mock_screenshot).is_file() or Path(mock_screenshot).stat().st_size == 0:
        raise RuntimeError("Local status-match mock screenshot was not saved.")

    synthetic_input_path = output / "qa_synthetic_inputs.json"
    synthetic_inputs = {
        "classification": "QA ONLY - SYNTHETIC INPUTS; NOT CUSTOMER ACCOUNT DATA",
        "intended_use": "Local status-match mock only. Never submit to live MSC or GoCCL.",
        "verified_booking_data": {
            "booking_id": booking_id,
            "passenger_index": passenger_index,
            "capture_timestamp": identity.get("_source_captured_at") or None,
            "origin": "MSC booking capture validated by load_msc_passengers",
            "adult_verified": True,
            "fields": {
                key: identity[key]
                for key in ("title", "first_name", "last_name", "dob", "state")
            },
        },
        "synthetic_values": {
            "guest_number": {
                "value": synthetic_guest_number,
                "format": "90########",
                "stored_in_history": str(history_path.resolve()),
                "verified_account_number": False,
            },
            "contact": {
                "email": email,
                "address": address,
                "phone": synthetic_phone,
                "source": "synthetic local-QA contact generation",
            },
        },
        "template_badge": {
            "displayed_value": "GOLD",
            "source": "unchanged local GoCCL template",
            "verified_for_passenger": False,
        },
    }
    _write_report(synthetic_input_path, synthetic_inputs)

    dob_discrepancy = None
    if identity["dob"] != LEGACY_QA_EXAMPLE_DOB:
        dob_discrepancy = {
            "example_dob": LEGACY_QA_EXAMPLE_DOB,
            "capture_dob": identity["dob"],
            "resolution": "validated capture used as the source; example value not applied",
        }

    report = {
        "workflow": (
            "msc_status_match_end_to_end_local_qa"
            if fresh_scan
            else "msc_status_match_local_qa"
        ),
        "status": "completed_local_qa_not_submitted",
        "target": "local mock and local Carnival-template preview only",
        "qa_preview_is_not_carnival_account_evidence": True,
        "synthetic_data_used_only_for_testing": True,
        "live_msc_form_opened": False,
        "live_msc_booking_scan_performed": fresh_scan,
        "form_submitted": False,
        "capture_corpus_modified": fresh_scan,
        "steps": {
            "msc_passenger_scan": "complete" if fresh_scan else "existing capture reused",
            "adapter_mapping_and_validation": "complete",
            "synthetic_contact_and_number_generation": "complete",
            "local_mock_form_fill_and_validation": "complete",
            "local_template_fill_and_watermark": "complete",
            "screenshots": "complete",
            "human_review": "required; nothing submitted",
        },
        "source": {
            "booking_id": booking_id,
            "passenger_index": passenger_index,
            "capture_timestamp": identity.get("_source_captured_at") or None,
            "origin": (
                "fresh read-only MSC scan"
                if fresh_scan
                else "existing MSC booking capture"
            ),
        },
        "validation": {
            "adult_verified": True,
            "title": identity["title"],
            "first_name": identity["first_name"],
            "last_name": identity["last_name"],
            "date_of_birth": identity["dob"],
            "state_of_residency": identity["state"],
            "mapped_fields": [
                "title",
                "first_name",
                "last_name",
                "date_of_birth",
                "state_of_residency",
            ],
        },
        "unverified_loyalty_fields": {
            "guest_number": {
                "value": synthetic_guest_number,
                "synthetic": True,
                "verified": False,
                "history_path": str(history_path.resolve()),
            },
            "loyalty_tier": {
                "value": None,
                "confirmed": False,
                "template_badge_displayed": "GOLD",
                "template_badge_verified_for_passenger": False,
            },
            "note": (
                "Synthetic local QA value; never looked up or verified against Carnival. "
                "The displayed GOLD badge is unchanged template content, not this passenger's status."
            ),
        },
        "synthetic_phone_number": synthetic_phone,
        "synthetic_contact": {
            "email": email,
            "address": address,
            "phone": synthetic_phone,
            "source": "synthetic local-QA contact generation",
        },
        "dob_discrepancy": dob_discrepancy,
        "mock_form_result": mock_result,
        "artifacts": {
            "goccl_template_preview_html": str(preview.resolve()),
            "goccl_template_preview_screenshot": str(preview_screenshot.resolve()),
            "local_mock_screenshot": str(mock_screenshot.resolve()),
            "synthetic_inputs": str(synthetic_input_path.resolve()),
            "generated_guest_number_history": str(history_path.resolve()),
        },
    }
    if scan_metadata is not None:
        report["msc_scan"] = scan_metadata
    report_path = output / "status_match_local_qa_report.json"
    report["execution_report"] = str(report_path.resolve())
    _write_report(report_path, report)
    return report


async def run_end_to_end_local_qa(
    booking_id: str,
    passenger_index: int,
    state: str,
    output_dir: Path,
    inbox_wait_seconds: int = 10,
    login_timeout_minutes: float = 15,
    *,
    msc_service_factory: Callable[[], Any] | None = None,
    filler_module: Any | None = None,
    mock_workflow: Callable[..., Any] | None = None,
    template_path: Path | None = None,
    capture_path: Path | None = None,
    guest_number_history_path: Path | None = None,
) -> dict[str, Any]:
    """Freshly scan MSC read-only, then run the synthetic local-only QA flow."""
    booking_id = str(booking_id).strip()
    if not re.fullmatch(r"\d{6,9}", booking_id):
        raise ValueError("MSC booking ID must be 6-9 digits")
    if passenger_index < 1:
        raise ValueError("passenger_index must be 1 or greater")
    if inbox_wait_seconds < 0 or inbox_wait_seconds > 120:
        raise ValueError("inbox_wait_seconds must be between 0 and 120")
    if login_timeout_minutes < 0 or login_timeout_minutes > 60:
        raise ValueError("login_timeout_minutes must be between 0 and 60")

    from MSC_Status_Match_Toolkit import msc_status_match_qa as qa

    normalized_state = next(
        (name for name in qa.STATE_CODES if name.casefold() == state.strip().casefold()),
        None,
    )
    if normalized_state is None:
        raise ValueError(f"Unsupported US state for synthetic contact generation: {state!r}")

    output_root = _private_output_dir(output_dir)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    run_output = output_root / f"msc_status_match_qa_{booking_id}_{stamp}"
    run_output.mkdir(parents=True, exist_ok=False)
    report_path = run_output / "status_match_local_qa_report.json"
    report: dict[str, Any] = {
        "workflow": "msc_status_match_end_to_end_local_qa",
        "booking_id": booking_id,
        "passenger_index": passenger_index,
        "started_at": datetime.now().isoformat(),
        "status": "running",
        "steps": {
            "msc_login": "pending",
            "msc_read_only_passenger_scan": "pending",
            "validated_adapter_and_local_qa": "pending",
            "human_review": "required; submission blocked",
        },
        "live_msc_form_opened": False,
        "form_submitted": False,
        "artifacts": {"execution_report": str(report_path.resolve())},
    }
    msc = None
    scan_record: dict[str, Any] | None = None
    error: Exception | None = None
    failed_step = "msc_login"
    sensitive: list[str] = []
    service_factory = msc_service_factory or MscLiveService
    try:
        msc = service_factory()
        if not await msc.check_login(timeout_minutes=login_timeout_minutes):
            raise RuntimeError("MSC login was not completed")
        report["steps"]["msc_login"] = "complete"

        failed_step = "msc_read_only_passenger_scan"
        scan_record = await msc.capture_passengers(booking_id)
        if not isinstance(scan_record, dict):
            raise RuntimeError("MSC passenger scan returned an invalid record")
        if str(scan_record.get("booking_id", "")).strip() != booking_id:
            raise RuntimeError("MSC passenger scan returned a different booking")
        passengers = scan_record.get("passengers")
        if not isinstance(passengers, list) or not passengers:
            raise RuntimeError("MSC passenger scan returned no passenger records")
        capture_timestamp = scan_record.get("captured_at")
        if not isinstance(capture_timestamp, str) or not capture_timestamp.strip():
            raise RuntimeError("MSC passenger scan record has no capture timestamp")
        sensitive.extend(
            str(value)
            for passenger in passengers
            if isinstance(passenger, dict)
            for value in (
                passenger.get("first_name"),
                passenger.get("last_name"),
                passenger.get("name"),
                passenger.get("dob"),
            )
            if value
        )
        report["steps"]["msc_read_only_passenger_scan"] = "complete"
        report["msc_scan"] = {
            "booking_id": booking_id,
            "capture_timestamp": capture_timestamp,
            "passenger_count": len(passengers),
            "command": f"scan_passengers:{booking_id}",
            "read_only": True,
        }
    except Exception as exc:
        error = exc
        report["steps"][failed_step] = "failed"
        report["error"] = _safe_error(exc, sensitive)
    finally:
        if msc is not None:
            try:
                await msc.stop()
            except Exception as exc:
                report["cleanup_error"] = _safe_error(exc, sensitive)
                if error is None:
                    error = exc
                    failed_step = "msc_read_only_passenger_scan"
                    report["steps"][failed_step] = "failed"

    if error is not None:
        report["status"] = "failed"
        report["finished_at"] = datetime.now().isoformat()
        _write_report(report_path, report)
        return report

    if scan_record is None:
        raise RuntimeError("MSC scan completed without a capture record.")
    scan_metadata = {
        "capture_timestamp": scan_record["captured_at"],
        "passenger_count": len(scan_record["passengers"]),
        "command": f"scan_passengers:{booking_id}",
        "read_only": True,
    }
    failed_step = "validated_adapter_and_local_qa"
    try:
        result = await run_local_qa(
            booking_id,
            passenger_index,
            normalized_state,
            run_output,
            inbox_wait_seconds,
            filler_module=filler_module,
            mock_workflow=mock_workflow,
            template_path=template_path,
            capture_path=capture_path,
            guest_number_history_path=guest_number_history_path,
            scan_metadata=scan_metadata,
        )
    except Exception as exc:
        report["steps"][failed_step] = "failed"
        report["status"] = "failed"
        report["error"] = _safe_error(exc, sensitive)
        report["finished_at"] = datetime.now().isoformat()
        report["artifacts"]["partial_files"] = [
            str(path.resolve())
            for path in run_output.iterdir()
            if path.is_file()
        ]
        _write_report(report_path, report)
        return report

    result["workflow"] = "msc_status_match_end_to_end_local_qa"
    result["status"] = "completed_local_qa_not_submitted"
    result["steps"]["msc_login"] = "complete"
    result["steps"]["msc_read_only_passenger_scan"] = "complete"
    result["steps"]["validated_adapter_and_local_qa"] = "complete"
    result["steps"]["human_review"] = "required; submission blocked"
    result["msc_scan"] = report["msc_scan"]
    result["artifacts"]["execution_report"] = str(report_path.resolve())
    result["finished_at"] = datetime.now().isoformat()
    _write_report(report_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or locally test MSC Status Match; never submits."
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    live = modes.add_parser("prepare", help="capture genuine records and prefill the live form")
    live.add_argument("--booking", required=True)
    live.add_argument("--guest-number", required=True, help="customer's actual Carnival Guest Number")
    live.add_argument("--contact-data", required=True, type=Path, help="customer/CRM JSON; contact fields only")
    live.add_argument("--loyalty-company", required=True, help="exact Carnival provider label shown by MSC")
    live.add_argument("--output-dir", required=True, type=Path, help="private folder outside the project")
    live.add_argument("--login-timeout-minutes", type=float, default=15)

    qa = modes.add_parser("qa", help="run synthetic QA only against local mocks")
    qa.add_argument("--booking", required=True)
    qa.add_argument("--passenger-index", required=True, type=int)
    qa.add_argument("--state", default="Oklahoma")
    qa.add_argument("--output-dir", required=True, type=Path, help="private folder outside the project")
    qa.add_argument("--inbox-wait-seconds", type=int, default=10)

    full_qa = modes.add_parser(
        "end-to-end-qa",
        help="freshly scan MSC, then run the synthetic QA flow only against local mocks",
    )
    full_qa.add_argument("--booking", required=True)
    full_qa.add_argument("--passenger-index", required=True, type=int)
    full_qa.add_argument("--state", default="Oklahoma")
    full_qa.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="private folder outside the project; each run gets a timestamped subfolder",
    )
    full_qa.add_argument("--inbox-wait-seconds", type=int, default=10)
    full_qa.add_argument("--login-timeout-minutes", type=float, default=15)

    preview = modes.add_parser(
        "identity-preview",
        help="fill only booking-verified name/DOB on MSC; leave other fields blank",
    )
    preview.add_argument("--booking", required=True)
    preview.add_argument("--passenger-index", required=True, type=int)
    preview.add_argument("--output-dir", required=True, type=Path, help="private folder outside the project")
    args = parser.parse_args()

    try:
        if args.mode == "prepare":
            if args.login_timeout_minutes < 0 or args.login_timeout_minutes > 60:
                parser.error("--login-timeout-minutes must be between 0 and 60")
            result = asyncio.run(run_live_workflow(
                args.booking,
                args.guest_number,
                args.contact_data,
                args.loyalty_company,
                args.output_dir,
                args.login_timeout_minutes,
            ))
        elif args.mode == "qa":
            if args.inbox_wait_seconds < 0 or args.inbox_wait_seconds > 120:
                parser.error("--inbox-wait-seconds must be between 0 and 120")
            result = asyncio.run(run_local_qa(
                args.booking,
                args.passenger_index,
                args.state,
                args.output_dir,
                args.inbox_wait_seconds,
            ))
        elif args.mode == "end-to-end-qa":
            result = asyncio.run(run_end_to_end_local_qa(
                args.booking,
                args.passenger_index,
                args.state,
                args.output_dir,
                args.inbox_wait_seconds,
                args.login_timeout_minutes,
            ))
        else:
            result = asyncio.run(run_identity_preview(
                args.booking,
                args.passenger_index,
                args.output_dir,
            ))
    except (OSError, ValueError, LookupError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
        return

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

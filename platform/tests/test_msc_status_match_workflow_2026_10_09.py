"""End-to-end orchestration checks using fake portal sessions.

These tests confirm that the live workflow uses the read-only MSC capture and
GoCCL lookup, requires the matched adult and contact input, saves proof and
before/after artifacts, and never invokes GoCCL Add Number or form submission.
"""

import json
from pathlib import Path

import pytest

from core.vifp_guest import guest_info_from_fields
from MSC_Status_Match_Toolkit import msc_status_match_workflow as workflow


def _guest(first="KIMBERLY", last="WILLIAMS"):
    return guest_info_from_fields({
        "Title": "MS",
        "First Name": first,
        "Last Name": last,
        "Date of Birth": "August 04, 1968",
        "State of Residency": "Georgia",
        "Carnival Guest Number": "1234567890",
        "Carnival Rewards Status": "GOLD",
        "Carnival Rewards Points Available": "No",
    })


def _contact_file(path):
    path.write_text(json.dumps({
        "mobile_number": "202-555-0100",
        "email": "customer@example.test",
        "country_code": "US",
        "zip_code": "74875",
        "city": "Tryon",
    }), encoding="utf-8")
    return path


class FakeMsc:
    def __init__(self):
        self.stopped = False

    async def check_login(self, timeout_minutes):
        return True

    async def capture_passengers(self, booking_id):
        assert booking_id == "3001060"
        return {
            "booking_id": booking_id,
            "captured_at": "2026-10-09T10:40:11",
            "passengers": [
                {
                    "name": "KIMBERLY K WILLIAMS",
                    "first_name": "KIMBERLY",
                    "last_name": "K WILLIAMS",
                    "dob": "08/04/1994",
                    "age": 58,
                }
            ],
        }

    async def stop(self):
        self.stopped = True


class FakeGoCcl:
    def __init__(self, guest=None):
        self.guest = guest or _guest()
        self.stopped = False
        self.lookup_number = None

    async def start(self):
        pass

    async def auto_login(self):
        return "OK"

    async def open_vifp_lookup(self):
        pass

    async def lookup_vifp_guest(self, number, screenshot_path):
        self.lookup_number = number
        from pathlib import Path
        Path(screenshot_path).write_bytes(b"official-page-screenshot")
        return self.guest

    async def add_vifp_guest(self, _guest):
        raise AssertionError("The status-match workflow must not click Add Number")

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_live_workflow_verifies_real_record_and_pauses_for_review(tmp_path):
    contact_path = _contact_file(tmp_path / "contact.json")
    output = tmp_path / "private"
    msc = FakeMsc()
    goccl = FakeGoCcl()
    captured = {}

    async def fake_prefill(data, screenshot, before_screenshot):
        captured["data"] = data
        screenshot.write_bytes(b"after")
        before_screenshot.write_bytes(b"before")
        return {"first_name": data["first_name"], "loyalty_status": data["loyalty_status"]}

    result = await workflow.run_live_workflow(
        "3001060",
        "1234567890",
        contact_path,
        "Carnival Cruise Line",
        output,
        msc_service_factory=lambda: msc,
        goccl_scraper_factory=lambda: goccl,
        form_prefill=fake_prefill,
    )

    assert result["status"] == "ready_for_human_review"
    assert result["submission"].startswith("NOT SUBMITTED")
    assert result["steps"]["human_review"] == "required"
    assert result["verified_vifp_tier"] == "GOLD"
    assert captured["data"]["first_name"] == "KIMBERLY"
    assert captured["data"]["last_name"] == "K WILLIAMS"
    assert captured["data"]["loyalty_status"] == "Gold"
    assert captured["data"]["membership_number"] == "1234567890"
    assert captured["data"]["card_proof_path"] == result["artifacts"]["vifp_account_proof"]
    assert goccl.lookup_number == "1234567890"
    assert msc.stopped and goccl.stopped
    assert all(__import__("pathlib").Path(path).is_file()
               for key, path in result["artifacts"].items()
               if key != "execution_report")
    saved_report = json.loads(
        __import__("pathlib").Path(result["artifacts"]["execution_report"]).read_text(
            encoding="utf-8"
        )
    )
    assert saved_report["status"] == "ready_for_human_review"
    assert "customer@example.test" not in json.dumps(saved_report)


@pytest.mark.asyncio
async def test_reserved_synthetic_guest_number_is_refused_before_portal_sessions(tmp_path):
    called = False

    def forbidden_service():
        nonlocal called
        called = True
        raise AssertionError("synthetic number must be refused before opening a portal")

    with pytest.raises(ValueError, match="reserved synthetic-test pattern"):
        await workflow.run_live_workflow(
            "3001061",
            "9023883679",
            tmp_path / "not-read.json",
            "Carnival Cruise Line",
            tmp_path / "private",
            msc_service_factory=forbidden_service,
        )
    assert not called


@pytest.mark.asyncio
async def test_identity_preview_fills_only_booking_identity_and_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_private_output_dir",
        lambda path: (Path(path).mkdir(parents=True, exist_ok=True) or Path(path)),
    )
    capture = {"captured_at": "2026-10-09T11:59:12"}
    passenger = {
        "first_name": "JEREMY",
        "last_name": "WEBB",
        "date_of_birth": "01/18/1997",
    }
    monkeypatch.setattr(
        "MSC_Status_Match_Toolkit.msc_status_match_qa.load_passenger",
        lambda booking_id, index: (capture, passenger),
    )
    submitted_identity = {}

    async def fake_prefill(identity, screenshot):
        submitted_identity.update(identity)
        screenshot.write_bytes(b"real-page-screenshot-placeholder")
        return {
            "verified_fields": "first_name,last_name,date_of_birth",
            "form_submitted": "no",
        }

    result = await workflow.run_identity_preview(
        "3001061", 1, tmp_path / "private", identity_prefill=fake_prefill
    )

    assert result["status"] == "identity_preview_complete_not_application_ready"
    assert submitted_identity == passenger
    assert result["contact_fields_filled"] == []
    assert result["loyalty_fields_filled"] == []
    assert result["steps"]["submission"] == "blocked; not submitted"
    assert Path(result["artifacts"]["filled_live_page_screenshot"]).is_file()
    persisted = json.loads(
        Path(result["artifacts"]["execution_report"]).read_text(encoding="utf-8")
    )
    assert persisted["status"] == "identity_preview_complete_not_application_ready"


@pytest.mark.asyncio
async def test_nonmatching_vifp_record_is_retained_but_never_prefills(tmp_path):
    contact_path = _contact_file(tmp_path / "contact.json")
    msc = FakeMsc()
    goccl = FakeGoCcl(_guest(first="SOMEONE", last="ELSE"))
    prefill_called = False

    async def forbidden_prefill(*_args, **_kwargs):
        nonlocal prefill_called
        prefill_called = True

    result = await workflow.run_live_workflow(
        "3001060",
        "1234567890",
        contact_path,
        "Carnival Cruise Line",
        tmp_path / "private",
        msc_service_factory=lambda: msc,
        goccl_scraper_factory=lambda: goccl,
        form_prefill=forbidden_prefill,
    )

    assert result["status"] == "failed"
    assert result["steps"]["passenger_identity_match"] == "failed"
    assert not prefill_called
    assert __import__("pathlib").Path(result["artifacts"]["vifp_account_proof"]).is_file()
    assert msc.stopped and goccl.stopped


@pytest.mark.asyncio
async def test_invalid_contact_data_fails_before_form_or_submission(tmp_path):
    contact_path = _contact_file(tmp_path / "contact.json")
    contact_path.write_text(
        json.dumps({
            "mobile_number": "202-555-0100",
            "email": "customer@example.test",
            "country_code": "US",
            "zip_code": "74875",
            "city": "Tryon",
            "first_name": "unverified override",
        }),
        encoding="utf-8",
    )
    prefill_called = False

    async def forbidden_prefill(*_args, **_kwargs):
        nonlocal prefill_called
        prefill_called = True

    result = await workflow.run_live_workflow(
        "3001060",
        "1234567890",
        contact_path,
        "Carnival Cruise Line",
        tmp_path / "private",
        msc_service_factory=FakeMsc,
        goccl_scraper_factory=FakeGoCcl,
        form_prefill=forbidden_prefill,
    )

    assert result["status"] == "failed"
    assert result["steps"]["contact_data_validation"] == "failed"
    assert not prefill_called
    assert "NOT SUBMITTED" in result["submission"]


def test_customer_artifacts_cannot_be_written_inside_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "PLATFORM_ROOT", tmp_path / "project")
    with pytest.raises(ValueError, match="outside the project"):
        workflow._private_output_dir(tmp_path / "project" / "evidence")


def test_contact_file_accepts_only_customer_contact_fields(tmp_path):
    path = _contact_file(tmp_path / "contact.json")
    contact = workflow._load_contact_data(path)
    assert set(contact) == workflow.CONTACT_FIELDS

    path.write_text('{"email":"customer@example.test","guest_number":"synthetic"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported contact fields"):
        workflow._load_contact_data(path)

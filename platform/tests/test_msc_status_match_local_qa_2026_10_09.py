"""Local MSC Status Match QA preserves data provenance and labels synthetic output.

Evidence (data/msc_control/booking_data.jsonl, booking 3001061, passenger 1):
    RILEY DEMOGUEST, DOB 01/18/1997, MR, Utah (UT), Voyagers 7240627 Gold.
The original fixed QA example used DOB "January 15, 1990", so a real build
must use the validated capture and report the mismatch instead of overriding
it. Synthetic loyalty data and the template's unverified GOLD badge must be
visibly marked as QA-only.

These tests never open Chrome, the network, or a live portal: the filler's
screenshot is stubbed and the mock workflow is injected.
"""

import json
from pathlib import Path

import pytest

from MSC_Status_Match_Toolkit import msc_status_match_workflow as workflow

FILLER_PATH = workflow.WORKSPACE_ROOT / "GoCCL_Guest_Info" / "goccl_template_fill.py"
BANNER_TEXT = "QA ONLY - LOCAL TEST DATA - GUEST NUMBER AND TEMPLATE TIER UNVERIFIED"
BANNER_RGB = (176, 0, 32)  # #b00020

CAPTURE = {
    "booking_id": "3001061",
    "captured_at": "2026-10-09T11:59:12.471923",
    "passengers": [
        {
            "name": "RILEY DEMOGUEST",
            "dob": "01/18/1997",
            "age": 47,
            "first_name": "JEREMY",
            "last_name": "WEBB",
            "title": "MR",
            "gender": "M",
            "state_of_residency": "Utah",
            "state_code": "UT",
        }
    ],
}

MINIMAL_TEMPLATE = """<!doctype html><html><head><title>t</title></head><body>
<div class=vifp-guest-info__tier data-component=program-GOLD-tier-icon>GOLD</div>
<div class=vifp-guest-info__title><span class=vifp-guest-info__label>Title</span><span class=vifp-guest-info__txt data-pii=true>MR</span></div>
<div class=vifp-guest-info__first-name><span class=vifp-guest-info__label>First Name</span><span class=vifp-guest-info__txt data-pii=true>ALEX</span></div>
<div class=vifp-guest-info__last-name><span class=vifp-guest-info__label>Last Name</span><span class=vifp-guest-info__txt data-pii=true>SMITH</span></div>
<div class=vifp-guest-info__dob><span class=vifp-guest-info__label>Date of Birth</span><span class=vifp-guest-info__txt data-pii=true>01/01/1990</span></div>
<div class=vifp-guest-info__sor><span class=vifp-guest-info__label>State of Residency</span><span class=vifp-guest-info__txt data-pii=true>FL</span></div>
<div class=vifp-guest-info__vifp-number><span class=vifp-guest-info__label>Carnival Guest Number</span><span class=vifp-guest-info__txt data-pii=true></span></div>
</body></html>"""


def _write_corpus(path: Path, records) -> Path:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


@pytest.fixture
def filler(monkeypatch, tmp_path):
    """The real GoCCL filler module, with the browser screenshot stubbed."""
    module = workflow._load_goccl_filler(FILLER_PATH)

    def fake_screenshot(html_path, _guest, _tier=None):
        assert 'id="qa-only-watermark"' in Path(html_path).read_text(
            encoding="utf-8"
        )
        png = Path(html_path).with_suffix(".png")
        png.write_bytes(b"stub-png")
        return png

    monkeypatch.setattr(module, "create_screenshot", fake_screenshot)
    return module


def _patch_output(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_private_output_dir",
        lambda path: (Path(path).mkdir(parents=True, exist_ok=True) or Path(path)),
    )


def test_load_msc_passengers_maps_validated_identity(filler, tmp_path):
    corpus = _write_corpus(tmp_path / "capture.jsonl", [CAPTURE])

    passengers = filler.load_msc_passengers(corpus, "3001061", 1)

    assert len(passengers) == 1
    identity = passengers[0]
    assert identity["title"] == "MR"
    assert identity["first_name"] == "JEREMY"
    assert identity["last_name"] == "WEBB"
    assert identity["dob"] == "January 18, 1979"
    assert identity["state"] == "Utah"
    assert identity["_source_captured_at"] == "2026-10-09T11:59:12.471923"


def test_missing_record_is_rejected(filler, tmp_path):
    corpus = _write_corpus(tmp_path / "capture.jsonl", [])

    with pytest.raises(LookupError, match="No MSC capture found"):
        filler.load_msc_passengers(corpus, "3001061", 1)


def test_invalid_dob_format_is_rejected(filler, tmp_path):
    bad = json.loads(json.dumps(CAPTURE))
    bad["passengers"][0]["dob"] = "1979-01-18"
    corpus = _write_corpus(tmp_path / "capture.jsonl", [bad])

    with pytest.raises(ValueError, match="MM/DD/YYYY"):
        filler.load_msc_passengers(corpus, "3001061", 1)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "", "verified title"),
        ("gender", "", "verified title"),
        ("state_of_residency", "", "verified title"),
    ],
)
def test_missing_verified_fields_are_rejected(filler, tmp_path, field, value, message):
    bad = json.loads(json.dumps(CAPTURE))
    bad["passengers"][0][field] = value
    corpus = _write_corpus(tmp_path / "capture.jsonl", [bad])

    with pytest.raises(ValueError, match=message):
        filler.load_msc_passengers(corpus, "3001061", 1)


def test_qa_watermark_is_opt_in_and_preserves_badge(filler):
    guest = {
        "title": "MR",
        "first_name": "JEREMY",
        "last_name": "WEBB",
        "dob": "January 18, 1979",
        "state": "Utah",
        "guest_number": "9012345678",
    }
    badge = filler.tier_markup(MINIMAL_TEMPLATE)
    unmarked = filler.fill_guest(MINIMAL_TEMPLATE, guest)
    marked = filler.fill_guest(MINIMAL_TEMPLATE, guest, qa_only=True)

    assert 'id="qa-only-watermark"' not in unmarked
    assert BANNER_TEXT in marked
    assert "NOT CARNIVAL ACCOUNT EVIDENCE" in marked
    assert filler.tier_markup(marked) == badge
    assert filler.tier_markup(unmarked) == badge


def test_local_mock_has_a_synthetic_loyalty_warning():
    from MSC_Status_Match_Toolkit import msc_status_match_qa as qa

    assert "QA ONLY - SYNTHETIC LOYALTY PLACEHOLDERS" in qa.MOCK_FORM
    assert 'id="qa-only-loyalty-warning"' in qa.MOCK_FORM


def test_generated_screenshot_contains_qa_banner_pixels(tmp_path):
    """Verify the rendered PNG contains the visible #b00020 QA banner.

    Uses the real, unstubbed screenshot path (headless Chrome) and skips when
    Chrome/Edge or Playwright/Pillow is unavailable on this machine.
    """
    pytest.importorskip("PIL")
    pytest.importorskip("playwright.sync_api")
    from PIL import Image

    filler = workflow._load_goccl_filler(FILLER_PATH)
    template = workflow.WORKSPACE_ROOT / "GoCCL_Guest_Info" / "GoCCL_Template.htm"
    if not template.is_file():
        pytest.skip("Saved GoCCL template not present on this machine")
    html_path = tmp_path / "preview.html"
    guest = {
        "title": "MR",
        "first_name": "JEREMY",
        "last_name": "WEBB",
        "dob": "January 18, 1979",
        "state": "Utah",
        "guest_number": "9012345678",
    }
    html_path.write_text(
        filler.fill_guest(
            template.read_text(encoding="utf-8", errors="replace"),
            guest,
            qa_only=True,
        ),
        encoding="utf-8",
    )
    try:
        png = filler.create_screenshot(html_path, guest, None)
    except FileNotFoundError as exc:
        pytest.skip(f"Chrome/Edge not installed: {exc}")
    image = Image.open(png).convert("RGB")
    pixels = image.load()
    width, height = image.size
    hits = sum(
        1
        for y in range(height)
        for x in range(width)
        if pixels[x, y] == BANNER_RGB
    )
    assert hits > 0


def test_randomize_phone_number_uses_reserved_fictional_exchange():
    import re

    numbers = {workflow._load_goccl_filler(FILLER_PATH).randomize_phone_number() for _ in range(20)}
    assert all(re.fullmatch(r"\d{3}55501\d{2}", number) for number in numbers)
    assert len(numbers) > 1


def test_generated_guest_numbers_are_unique_and_persisted(filler, tmp_path):
    history = tmp_path / "generated.json"

    first = filler.generate_synthetic_guest_numbers(1, history_path=history)[0]
    second = filler.generate_synthetic_guest_numbers(1, history_path=history)[0]

    assert first != second
    assert filler.SYNTHETIC_GUEST_NUMBER_PATTERN.fullmatch(first)
    assert json.loads(history.read_text(encoding="utf-8")) == [first, second]


def _mock_result(screenshot, synthetic_guest_number, synthetic_phone_number):
    email = "qa@example.test"
    address = {
        "street": "100 QA Lane",
        "city": "Test City",
        "postcode": "74875",
        "state": "OK",
        "country": "United States",
        "phone": synthetic_phone_number,
    }
    form = {
        "first_name": "JEREMY",
        "last_name": "WEBB",
        "date_of_birth": "01/18/1997",
        "mobile_number": synthetic_phone_number,
        "email": email,
        "country_code": "US",
        "zip_code": "74875",
        "city": "Test City",
        "state": "OK",
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
    Path(screenshot).write_bytes(b"mock")
    return {
        "booking_id": CAPTURE["booking_id"],
        "capture_timestamp": CAPTURE["captured_at"],
        "passenger_index": 1,
        "adult_verified": True,
        "address": address,
        "temporary_email": email,
        "mock_form_verified": form,
        "form_submitted": False,
        "live_msc_form_opened": False,
    }


@pytest.mark.asyncio
async def test_run_local_qa_builds_capture_driven_watermarked_preview(
    filler, tmp_path, monkeypatch
):
    _patch_output(monkeypatch)
    corpus = _write_corpus(tmp_path / "capture.jsonl", [CAPTURE])
    template = tmp_path / "template.htm"
    template.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
    history = tmp_path / "generated_guest_numbers.json"
    monkeypatch.setattr(filler, "GENERATED_GUEST_NUMBERS_PATH", history)
    mock_calls = {}

    async def fake_mock(
        booking_id,
        passenger_index,
        state,
        screenshot,
        wait,
        *,
        synthetic_guest_number,
        synthetic_phone_number,
    ):
        mock_calls.update(
            booking_id=booking_id,
            passenger_index=passenger_index,
            state=state,
            wait=wait,
            synthetic_guest_number=synthetic_guest_number,
            synthetic_phone_number=synthetic_phone_number,
        )
        return _mock_result(screenshot, synthetic_guest_number, synthetic_phone_number)

    report = await workflow.run_local_qa(
        "3001061",
        1,
        "Oklahoma",
        tmp_path / "private",
        3,
        filler_module=filler,
        mock_workflow=fake_mock,
        template_path=template,
        capture_path=corpus,
    )

    preview = Path(report["artifacts"]["goccl_template_preview_html"])
    html = preview.read_text(encoding="utf-8")
    assert BANNER_TEXT in html
    assert 'id="qa-only-watermark"' in html
    assert "JEREMY" in html and "January 18, 1979" in html and "Utah" in html
    assert Path(report["artifacts"]["goccl_template_preview_screenshot"]).is_file()

    assert report["source"]["booking_id"] == "3001061"
    assert report["source"]["passenger_index"] == 1
    assert report["source"]["capture_timestamp"] == "2026-10-09T11:59:12.471923"
    assert report["validation"]["first_name"] == "JEREMY"
    assert report["validation"]["date_of_birth"] == "January 18, 1979"
    synthetic_number = report["unverified_loyalty_fields"]["guest_number"]["value"]
    assert filler.SYNTHETIC_GUEST_NUMBER_PATTERN.fullmatch(synthetic_number)
    assert report["unverified_loyalty_fields"]["guest_number"]["synthetic"] is True
    assert report["unverified_loyalty_fields"]["guest_number"]["verified"] is False
    assert report["unverified_loyalty_fields"]["loyalty_tier"]["confirmed"] is False
    assert report["unverified_loyalty_fields"]["loyalty_tier"][
        "template_badge_displayed"
    ] == "GOLD"
    assert report["unverified_loyalty_fields"]["loyalty_tier"][
        "template_badge_verified_for_passenger"
    ] is False
    assert report["qa_preview_is_not_carnival_account_evidence"] is True
    assert report["live_msc_form_opened"] is False
    assert report["form_submitted"] is False
    assert mock_calls["synthetic_guest_number"] == synthetic_number
    assert mock_calls["synthetic_phone_number"] == report["synthetic_phone_number"]
    assert json.loads(history.read_text(encoding="utf-8")) == [synthetic_number]

    assert preview.is_file()
    assert Path(report["execution_report"]).is_file()
    saved = json.loads(Path(report["execution_report"]).read_text(encoding="utf-8"))
    assert saved["execution_report"] == report["execution_report"]
    assert saved["status"] == "completed_local_qa_not_submitted"
    assert "QA ONLY" in json.dumps(saved, ensure_ascii=False)
    synthetic_inputs = json.loads(
        Path(report["artifacts"]["synthetic_inputs"]).read_text(encoding="utf-8")
    )
    assert synthetic_inputs["classification"].startswith("QA ONLY")
    assert synthetic_inputs["verified_booking_data"]["fields"]["first_name"] == "JEREMY"
    assert synthetic_inputs["synthetic_values"]["guest_number"]["value"] == synthetic_number
    assert synthetic_inputs["synthetic_values"]["guest_number"]["verified_account_number"] is False
    assert synthetic_inputs["synthetic_values"]["contact"]["phone"] == report["synthetic_phone_number"]
    assert synthetic_inputs["template_badge"]["verified_for_passenger"] is False
    assert mock_calls == {
        "booking_id": "3001061",
        "passenger_index": 1,
        "state": "Oklahoma",
        "wait": 3,
        "synthetic_guest_number": synthetic_number,
        "synthetic_phone_number": report["synthetic_phone_number"],
    }


@pytest.mark.asyncio
async def test_dob_discrepancy_is_reported_not_overridden(filler, tmp_path, monkeypatch):
    _patch_output(monkeypatch)
    corpus = _write_corpus(tmp_path / "capture.jsonl", [CAPTURE])
    template = tmp_path / "template.htm"
    template.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
    history = tmp_path / "generated_guest_numbers.json"
    monkeypatch.setattr(filler, "GENERATED_GUEST_NUMBERS_PATH", history)

    async def fake_mock(
        _booking_id,
        _passenger_index,
        _state,
        screenshot,
        _wait,
        *,
        synthetic_guest_number,
        synthetic_phone_number,
    ):
        return _mock_result(screenshot, synthetic_guest_number, synthetic_phone_number)

    report = await workflow.run_local_qa(
        "3001061",
        1,
        "Oklahoma",
        tmp_path / "private",
        0,
        filler_module=filler,
        mock_workflow=fake_mock,
        template_path=template,
        capture_path=corpus,
    )

    discrepancy = report["dob_discrepancy"]
    assert discrepancy["example_dob"] == "January 15, 1990"
    assert discrepancy["capture_dob"] == "January 18, 1979"
    assert "capture" in discrepancy["resolution"]
    assert report["validation"]["date_of_birth"] == "January 18, 1979"
    html = Path(report["artifacts"]["goccl_template_preview_html"]).read_text(
        encoding="utf-8"
    )
    assert "January 18, 1979" in html
    assert "January 15, 1990" not in html


@pytest.mark.asyncio
async def test_end_to_end_qa_freshly_scans_then_runs_only_local_pipeline(
    filler, tmp_path
):
    corpus = tmp_path / "booking_data.jsonl"
    template = tmp_path / "template.htm"
    template.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
    history = tmp_path / "generated_guest_numbers.json"
    calls = []

    class FakeMscService:
        async def check_login(self, timeout_minutes):
            calls.append(("login", timeout_minutes))
            return True

        async def capture_passengers(self, booking_id):
            calls.append(("scan", booking_id))
            _write_corpus(corpus, [CAPTURE])
            return json.loads(json.dumps(CAPTURE))

        async def stop(self):
            calls.append(("stop",))

    async def fake_mock(
        _booking_id,
        _passenger_index,
        _state,
        screenshot,
        _wait,
        *,
        synthetic_guest_number,
        synthetic_phone_number,
    ):
        return _mock_result(screenshot, synthetic_guest_number, synthetic_phone_number)

    report = await workflow.run_end_to_end_local_qa(
        "3001061",
        1,
        "Oklahoma",
        tmp_path / "private",
        0,
        0,
        msc_service_factory=FakeMscService,
        filler_module=filler,
        mock_workflow=fake_mock,
        template_path=template,
        capture_path=corpus,
        guest_number_history_path=history,
    )

    assert calls == [("login", 0), ("scan", "3001061"), ("stop",)]
    assert report["status"] == "completed_local_qa_not_submitted"
    assert report["live_msc_booking_scan_performed"] is True
    assert report["live_msc_form_opened"] is False
    assert report["form_submitted"] is False
    assert report["capture_corpus_modified"] is True
    assert report["source"]["capture_timestamp"] == CAPTURE["captured_at"]
    assert report["steps"]["msc_read_only_passenger_scan"] == "complete"
    assert report["steps"]["validated_adapter_and_local_qa"] == "complete"
    assert Path(report["artifacts"]["execution_report"]).is_file()
    assert Path(report["artifacts"]["goccl_template_preview_html"]).is_file()
    assert Path(report["artifacts"]["goccl_template_preview_screenshot"]).is_file()
    assert Path(report["artifacts"]["local_mock_screenshot"]).is_file()
    assert Path(report["artifacts"]["synthetic_inputs"]).is_file()
    assert len(json.loads(history.read_text(encoding="utf-8"))) == 1


@pytest.mark.asyncio
async def test_end_to_end_login_failure_saves_failure_report_without_qa_outputs(tmp_path):
    calls = []

    class FailedLoginMscService:
        async def check_login(self, timeout_minutes):
            calls.append(("login", timeout_minutes))
            return False

        async def stop(self):
            calls.append(("stop",))

    report = await workflow.run_end_to_end_local_qa(
        "3001061",
        1,
        "Oklahoma",
        tmp_path / "private",
        0,
        0,
        msc_service_factory=FailedLoginMscService,
    )

    assert calls == [("login", 0), ("stop",)]
    assert report["status"] == "failed"
    assert report["steps"]["msc_login"] == "failed"
    assert report["steps"]["msc_read_only_passenger_scan"] == "pending"
    assert report["form_submitted"] is False
    assert Path(report["artifacts"]["execution_report"]).is_file()
    assert "login was not completed" in report["error"]

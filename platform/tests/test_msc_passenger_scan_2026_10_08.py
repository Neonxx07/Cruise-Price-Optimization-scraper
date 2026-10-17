"""MSC passenger scan command stays read-only and preserves every capture.

The command must validate booking IDs, append successful captures to the
existing booking corpus, and save a screenshot of the real booking page.
"""

import json

import pytest

import msc_commands


def test_form_fields_are_attached_only_after_name_and_dob_match():
    passengers = [{"name": "MORGAN DEMOGUEST", "dob": "06/04/1991"}]
    msc_commands._attach_verified_passenger_form_details(passengers, [{
        "first_name": "BRIAN",
        "last_name": "WILLIAMS",
        "title": "MR",
        "gender": "M",
        "dob": "06/04/1991",
        "state_code": "OK",
        "state_label": "OKLAHOMA",
    }])

    assert passengers[0]["title"] == "MR"
    assert passengers[0]["first_name"] == "BRIAN"
    assert passengers[0]["last_name"] == "WILLIAMS"
    assert passengers[0]["gender"] == "M"
    assert passengers[0]["state_of_residency"] == "Oklahoma"
    assert passengers[0]["state_code"] == "OK"


def test_form_fields_are_not_attached_if_name_or_dob_disagrees():
    passengers = [{"name": "MORGAN DEMOGUEST", "dob": "06/04/1991"}]
    msc_commands._attach_verified_passenger_form_details(passengers, [{
        "first_name": "BRIAN",
        "last_name": "WILLIAMS",
        "title": "MR",
        "gender": "M",
        "dob": "06/05/1992",
        "state_code": "OK",
        "state_label": "OKLAHOMA",
    }])

    assert "state_of_residency" not in passengers[0]
    assert "title" not in passengers[0]


@pytest.mark.asyncio
async def test_scan_passengers_appends_capture_and_saves_booking_screenshot(
    tmp_path, monkeypatch
):
    screenshot_dir = tmp_path / "screenshots"
    booking_data = tmp_path / "booking_data.jsonl"
    monkeypatch.setattr(msc_commands, "SCREENSHOT_DIR", str(screenshot_dir))
    monkeypatch.setattr(msc_commands, "BOOKING_DATA_PATH", str(booking_data))

    async def lookup(_page, booking_id, screenshot_path=None):
        assert booking_id == "12345678"
        assert screenshot_path
        with open(screenshot_path, "wb") as image:
            image.write(b"captured page")
        return {
            "booking_id": booking_id,
            "passengers": [{
                "name": "JANE DOE",
                "dob": "01/18/1997",
                "voyagers_number": None,
                "voyagers_tier": None,
            }],
        }

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", lookup)
    result = await msc_commands.run_command(
        {"page": object()}, "scan_passengers:12345678"
    )

    record = json.loads(booking_data.read_text(encoding="utf-8").splitlines()[0])
    screenshots = list(screenshot_dir.glob("passengers_12345678_*.png"))
    assert record["passengers"][0]["name"] == "JANE DOE"
    assert len(screenshots) == 1
    assert screenshots[0].read_bytes() == b"captured page"
    assert "1 passenger(s)" in result
    assert str(screenshots[0]) in result


@pytest.mark.asyncio
async def test_expired_session_does_not_append_a_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(msc_commands, "SCREENSHOT_DIR", str(tmp_path / "shots"))
    booking_data = tmp_path / "booking_data.jsonl"
    monkeypatch.setattr(msc_commands, "BOOKING_DATA_PATH", str(booking_data))

    async def lookup(_page, _booking_id, screenshot_path=None):
        assert screenshot_path
        return {"booking_id": "12345678", "session_expired": True}

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", lookup)
    result = await msc_commands.run_command(
        {"page": object()}, "scan_passengers:12345678"
    )

    assert "session expired" in result
    assert not booking_data.exists()
    assert not list((tmp_path / "shots").glob("*.png"))


@pytest.mark.asyncio
async def test_scan_passengers_rejects_invalid_booking_id(tmp_path, monkeypatch):
    monkeypatch.setattr(msc_commands, "SCREENSHOT_DIR", str(tmp_path / "shots"))

    async def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("invalid booking ID reached the page lookup")

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", unexpected_lookup)
    with pytest.raises(ValueError, match="6-9 digits"):
        await msc_commands.run_command({"page": object()}, "scan_passengers:/*")

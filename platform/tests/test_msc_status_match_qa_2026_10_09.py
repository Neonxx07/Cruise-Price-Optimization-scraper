"""Local MSC Status Match QA never edits captures or enables submission."""

import json

import pytest

from MSC_Status_Match_Toolkit import msc_status_match_qa as qa


def test_load_passenger_reads_latest_capture_without_mutating_corpus(tmp_path, monkeypatch):
    corpus = tmp_path / "booking_data.jsonl"
    records = [
        {
            "booking_id": "12345678",
            "captured_at": "2026-10-08T10:00:00",
            "passengers": [
                {
                    "first_name": "KIMBERLY",
                    "last_name": "K WILLIAMS",
                    "dob": "08/04/1994",
                    "age": 58,
                }
            ],
        },
        {
            "booking_id": "12345678",
            "captured_at": "2026-10-09T10:00:00",
            "passengers": [
                {
                    "first_name": "KIMBERLY",
                    "last_name": "K WILLIAMS",
                    "dob": "08/04/1994",
                    "age": 58,
                }
            ],
        },
    ]
    original = "".join(json.dumps(item) + "\n" for item in records)
    corpus.write_text(original, encoding="utf-8")
    monkeypatch.setattr(qa, "BOOKING_DATA_PATH", corpus)

    capture, passenger = qa.load_passenger("12345678", 1)

    assert capture["captured_at"] == "2026-10-09T10:00:00"
    assert passenger == {
        "first_name": "KIMBERLY",
        "last_name": "K WILLIAMS",
        "date_of_birth": "08/04/1994",
        "age": 58,
    }
    assert corpus.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("booking_id", "index", "passengers", "message"),
    [
        ("12345678", 0, [], "passenger_index"),
        ("bad-id", 1, [], "6-9 digits"),
        (
            "12345678",
            1,
            [{"first_name": "MINOR", "last_name": "GUEST", "dob": "01/01/2015", "age": 11}],
            "not verified as an adult",
        ),
    ],
)
def test_load_passenger_rejects_bad_selection(
    tmp_path, monkeypatch, booking_id, index, passengers, message
):
    corpus = tmp_path / "booking_data.jsonl"
    corpus.write_text(
        json.dumps({"booking_id": "12345678", "passengers": passengers}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(qa, "BOOKING_DATA_PATH", corpus)

    with pytest.raises((ValueError, LookupError), match=message):
        qa.load_passenger(booking_id, index)


@pytest.mark.asyncio
async def test_local_mock_fills_synthetic_contact_and_keeps_submit_controls_safe(tmp_path):
    screenshot = tmp_path / "review.png"
    passenger = {
        "first_name": "TEST",
        "last_name": "APPLICANT",
        "date_of_birth": "01/01/1990",
    }
    address = {
        "street": "100 QA Lane",
        "city": "Test City",
        "state": "OK",
        "postcode": "74875",
        "phone": "4055550100",
        "country": "United States",
    }

    result = await qa.fill_local_mock(passenger, address, "qa@example.test", screenshot)

    assert result["first_name"] == "TEST"
    assert result["last_name"] == "APPLICANT"
    assert result["date_of_birth"] == "01/01/1990"
    assert result["email"] == "qa@example.test"
    assert result["zip_code"] == "74875"
    assert result["city"] == "Test City"
    assert result["membership_number"] == ""
    assert result["qa_only_warning_visible"] is False
    assert result["confirm_disabled"] is True
    assert result["form_submitted"] is False
    assert screenshot.is_file()
    assert screenshot.stat().st_size > 0


@pytest.mark.asyncio
async def test_local_mock_labels_synthetic_guest_number_without_claiming_tier(tmp_path):
    screenshot = tmp_path / "synthetic-review.png"
    passenger = {
        "first_name": "TEST",
        "last_name": "APPLICANT",
        "date_of_birth": "01/01/1990",
    }
    address = {
        "street": "100 QA Lane",
        "city": "Test City",
        "state": "OK",
        "postcode": "74875",
        "phone": "2025550101",
        "country": "United States",
    }

    result = await qa.fill_local_mock(
        passenger,
        address,
        "qa@example.test",
        screenshot,
        synthetic_guest_number="9012345678",
    )

    assert result["loyalty_company"] == "QA ONLY - NOT A REAL PROGRAM"
    assert result["loyalty_status"] == "UNVERIFIED QA PLACEHOLDER"
    assert result["membership_number"] == "9012345678"
    assert result["qa_only_warning_visible"] is True
    assert result["synthetic_loyalty_data"] is True
    assert result["card_proof_selected"] is False
    assert result["signature"] == ""
    assert result["confirm_disabled"] is True
    assert result["form_submitted"] is False


@pytest.mark.asyncio
async def test_local_mock_rejects_non_synthetic_guest_number_before_browser(tmp_path):
    with pytest.raises(ValueError, match="reserved 90"):
        await qa.fill_local_mock(
            {
                "first_name": "TEST",
                "last_name": "APPLICANT",
                "date_of_birth": "01/01/1990",
            },
            {},
            "qa@example.test",
            tmp_path / "bad.png",
            synthetic_guest_number="1234567890",
        )

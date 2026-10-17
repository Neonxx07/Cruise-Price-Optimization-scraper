"""MSC Status Match helper input validation, isolated from the live form."""

import pytest

from MSC_Status_Match_Toolkit.msc_status_match_form import validate_form_data


@pytest.fixture
def valid_data():
    return {
        "first_name": "TEST",
        "last_name": "APPLICANT",
        "date_of_birth": "01/01/1990",
        "mobile_number": "202-555-0100",
        "email": "qa@example.com",
        "country_code": "US",
        "zip_code": "74875",
    }


def test_valid_applicant_fields_are_normalized(valid_data):
    result = validate_form_data(valid_data)

    assert result["country_code"] == "US"
    assert result["zip_code"] == "74875"
    assert result["city"] == ""
    assert result["card_proof_path"] == ""


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("date_of_birth", "1/1/1990", "zero-padded"),
        ("date_of_birth", "13/01/1990", "MM/DD/YYYY"),
        ("country_code", "CA", "US or PR"),
        ("zip_code", "7487", "five digits"),
        ("email", "not-an-email", "valid email"),
        ("mobile_number", "12345", "10-digit"),
        ("mobile_number", "212-555-01000", "10-digit"),
    ],
)
def test_invalid_applicant_fields_are_rejected(valid_data, field, value, message):
    valid_data[field] = value

    with pytest.raises(ValueError, match=message):
        validate_form_data(valid_data)


def test_unknown_fields_are_rejected(valid_data):
    valid_data["signature"] = "TEST APPLICANT"

    with pytest.raises(ValueError, match="Unsupported data fields"):
        validate_form_data(valid_data)


def test_loyalty_fields_must_be_complete_and_proof_must_be_allowed(valid_data):
    valid_data.update(
        {
            "loyalty_company": "Example Cruise Line",
            "loyalty_status": "Gold",
        }
    )

    with pytest.raises(ValueError, match="together using"):
        validate_form_data(valid_data)


def test_loyalty_proof_requires_existing_supported_file(valid_data, tmp_path):
    proof = tmp_path / "proof.png"
    proof.write_bytes(b"test image")
    valid_data.update(
        {
            "loyalty_company": "Example Cruise Line",
            "loyalty_status": "Gold",
            "membership_number": "TEST-123",
            "card_proof_path": str(proof),
        }
    )

    result = validate_form_data(valid_data)

    assert result["card_proof_path"] == str(proof.resolve())


def test_oversized_loyalty_proof_is_rejected(valid_data, tmp_path):
    proof = tmp_path / "proof.png"
    proof.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    valid_data.update(
        {
            "loyalty_company": "Example Cruise Line",
            "loyalty_status": "Gold",
            "membership_number": "TEST-123",
            "card_proof_path": str(proof),
        }
    )

    with pytest.raises(ValueError, match="must not exceed 4 MB"):
        validate_form_data(valid_data)

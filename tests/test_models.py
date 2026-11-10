"""Contract tests: malformed types and quantities never reach arithmetic."""

import pytest
from pydantic import ValidationError

from sourcing_api.models import Driver, Evidence, Quote, ReviewRequest


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "-Infinity", "0", "-1"])
@pytest.mark.parametrize("field", ["quantity", "unit_price"])
def test_quote_rejects_invalid_numeric_values(field, value):
    with pytest.raises(ValidationError):
        Quote.model_validate({field: value})


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "-0.1", "1.01"])
def test_driver_share_is_finite_proportion(value):
    with pytest.raises(ValidationError):
        Driver.model_validate({"name": "energy", "share": value})


@pytest.mark.parametrize("field", ["baseline_index", "current_index"])
@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "0", "-1"])
def test_indices_cannot_be_zero_negative_nonfinite_or_boolean(field, value):
    with pytest.raises(ValidationError):
        Driver.model_validate({"name": "energy", field: value})


@pytest.mark.parametrize("page", [True, "1", 0, 21])
def test_evidence_requires_bounded_integer_page(page):
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {
                "document": "fictional.pdf",
                "page": page,
                "quote": "Example",
                "sha256": "a" * 64,
            }
        )


def test_evidence_requires_full_sha256_not_arbitrary_label():
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {
                "document": "fictional.pdf",
                "page": 1,
                "quote": "Example",
                "sha256": "unverified",
            }
        )


def test_unknown_fields_are_not_silently_ignored():
    with pytest.raises(ValidationError):
        Quote.model_validate({"unit_price": "100", "unit_prcie": "112"})


def test_quote_validity_cannot_precede_its_issue_date():
    with pytest.raises(ValidationError):
        Quote.model_validate({"quote_date": "2026-11-01", "valid_until": "2026-10-31"})


def test_empty_driver_basket_is_rejected_before_review():
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(
            {
                "as_of": "2026-11-09",
                "baseline": {},
                "proposal": {},
                "drivers": [],
                "assumptions": ["Synthetic"],
            }
        )


def test_empty_assumptions_are_rejected_before_review():
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(
            {
                "as_of": "2026-11-09",
                "baseline": {},
                "proposal": {},
                "drivers": [{"name": "energy"}],
                "assumptions": [],
            }
        )


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "0", "-1"])
def test_annual_volume_must_be_a_positive_number(value):
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(
            {
                "as_of": "2026-11-09",
                "annual_volume": value,
                "baseline": {},
                "proposal": {},
                "drivers": [{"name": "energy"}],
                "assumptions": ["Synthetic"],
            }
        )

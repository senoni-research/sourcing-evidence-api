"""Acceptance tests for the declared-basket decision boundary and arithmetic."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from sourcing_api.domain import review
from sourcing_api.models import ReviewRequest


def request_data():
    def evidence(text, page=1):
        return {
            "document": "fictional-case.pdf",
            "page": page,
            "quote": text,
            "sha256": "a" * 64,
        }

    def quote(price, quote_date):
        result = {
            "supplier": "Fictional Supplier",
            "sku": "SYNTH-001",
            "revision": "A",
            "currency": "GBP",
            "unit": "piece",
            "quantity": "10000",
            "unit_price": price,
            "quote_date": quote_date,
            "valid_until": "2026-12-31",
            "freight": "included",
        }
        result["evidence"] = {field: evidence(f"{field}: {value}") for field, value in result.items()}
        return result

    def driver(name, share, current):
        return {
            "name": name,
            "share": share,
            "baseline_index": "100",
            "current_index": current,
            "baseline_period": "2026-01-01",
            "current_period": "2026-10-01",
            "evidence": evidence(f"{name}: share={share}; index=100 to {current}", 2),
        }

    return {
        "as_of": "2026-11-09",
        "annual_volume": "10000",
        "baseline": quote("100", "2026-01-01"),
        "proposal": quote("112", "2026-11-01"),
        "drivers": [
            driver("materials", "0.5", "106"),
            driver("energy", "0.2", "90"),
            driver("labour", "0.2", "104"),
            driver("freight", "0.1", "105"),
        ],
        "assumptions": [
            "Synthetic demonstration only; the baseline price is fully indexed.",
            "Unchanged margin, productivity, specification and quantity basis.",
        ],
    }


def run(data=None):
    return review(ReviewRequest.model_validate(request_data() if data is None else data))


def codes(result):
    return {issue.code for issue in result.issues}


def test_published_scenario_and_component_contributions():
    result = run()
    assert result.status == "scenario_available"
    assert result.issues == []
    c = result.calculation
    assert c is not None
    assert c.baseline_price == Decimal(100)
    assert c.proposed_price == Decimal(112)
    assert c.proposed_change_pct == Decimal(12)
    assert c.model_change_pct == Decimal("2.3")
    assert c.scenario_price == Decimal("102.30")
    assert c.gap_per_unit == Decimal("9.70")
    assert c.annualised_gap == Decimal("97000.00")
    assert {d.name: d.contribution_pp for d in c.contributions} == {
        "materials": Decimal(3),
        "energy": Decimal(-2),
        "labour": Decimal("0.8"),
        "freight": Decimal("0.5"),
    }
    assert any("not evidence of overcharging" in note for note in result.limitations)


@pytest.mark.parametrize(
    "field",
    [
        "supplier",
        "sku",
        "revision",
        "currency",
        "unit",
        "quantity",
        "unit_price",
        "quote_date",
    ],
)
@pytest.mark.parametrize("side", ["baseline", "proposal"])
def test_missing_quote_fact_withholds_result(side, field):
    data = request_data()
    data[side][field] = None
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert any(i.code == "missing_quote_fact" and i.field == f"{side}.{field}" for i in result.issues)


@pytest.mark.parametrize(
    "field",
    [
        "share",
        "baseline_index",
        "current_index",
        "baseline_period",
        "current_period",
        "evidence",
    ],
)
def test_missing_driver_evidence_is_not_silently_zero_filled(field):
    data = request_data()
    data["drivers"][1][field] = None
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert any(i.code == "missing_driver_fact" and i.field == f"drivers.1.{field}" for i in result.issues)


@pytest.mark.parametrize(
    "field,value",
    [
        ("supplier", "Another Supplier"),
        ("sku", "OTHER-SKU"),
        ("revision", "B"),
        ("currency", "EUR"),
        ("unit", "box"),
        ("quantity", "5000"),
        ("freight", "excluded"),
    ],
)
def test_different_quote_scope_cannot_produce_a_comparison(field, value):
    data = request_data()
    data["proposal"][field] = value
    result = run(data)
    assert result.status == "not_comparable"
    assert result.calculation is None
    assert any(i.code == "scope_mismatch" and i.field == field for i in result.issues)


def test_incomplete_basket_is_not_renormalised():
    data = request_data()
    data["drivers"][1]["share"] = "0.1"
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert "incomplete_basket" in codes(result)


def test_duplicate_driver_detected_even_with_capitalisation_and_space():
    data = request_data()
    data["drivers"][1]["name"] = " MATERIALS "
    result = run(data)
    assert result.calculation is None
    assert "duplicate_driver" in codes(result)


@pytest.mark.parametrize(
    "current,expected",
    [
        ("2026-01-01", "stale_index"),
        ("2026-11-10", "future_index"),
        ("2025-12-01", "reversed_index_period"),
    ],
)
def test_invalid_index_period_blocks_calculation(current, expected):
    data = request_data()
    for driver in data["drivers"]:
        driver["current_period"] = current
    result = run(data)
    assert result.calculation is None
    assert expected in codes(result)


def test_index_freshness_boundary_is_inclusive():
    data = request_data()
    ninety_days_ago = date.fromordinal(date(2026, 11, 9).toordinal() - 90).isoformat()
    for driver in data["drivers"]:
        driver["current_period"] = ninety_days_ago
    assert run(data).status == "scenario_available"


def test_different_index_baselines_do_not_mix_in_one_basket():
    data = request_data()
    data["drivers"][1]["baseline_period"] = "2026-02-01"
    result = run(data)
    assert result.calculation is None
    assert "inconsistent_periods" in codes(result)


@pytest.mark.parametrize(
    "valid_until,expected",
    [
        (None, "missing_validity"),
        ("2026-11-08", "expired_proposal"),
    ],
)
def test_proposal_must_have_current_validity(valid_until, expected):
    data = request_data()
    data["proposal"]["valid_until"] = valid_until
    result = run(data)
    assert result.calculation is None
    assert expected in codes(result)


def test_quote_is_still_valid_on_its_expiry_day():
    data = request_data()
    data["proposal"]["valid_until"] = "2026-11-09"
    assert run(data).status == "scenario_available"


def test_future_quote_is_withheld():
    data = request_data()
    data["proposal"]["quote_date"] = "2026-11-10"
    result = run(data)
    assert result.calculation is None
    assert "future_quote" in codes(result)


def test_proposal_before_baseline_is_withheld():
    data = request_data()
    data["proposal"]["quote_date"] = "2025-12-31"
    result = run(data)
    assert result.calculation is None
    assert "reversed_quotes" in codes(result)


def test_unknown_freight_withholds_calculation():
    data = request_data()
    data["baseline"]["freight"] = data["proposal"]["freight"] = "unknown"
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert "unknown_freight" in codes(result)


def test_annual_volume_is_optional_and_does_not_change_unit_scenario():
    data = request_data()
    data["annual_volume"] = None
    result = run(data)
    assert result.calculation.scenario_price == Decimal("102.30")
    assert result.calculation.annualised_gap is None


def test_a_correction_changes_only_its_dependent_contribution():
    before = run()
    data = request_data()
    data["drivers"][1]["current_index"] = "95"
    after = run(data)
    assert after.calculation.scenario_price == Decimal("103.30")
    assert after.calculation.gap_per_unit == Decimal("8.70")
    assert after.calculation.annualised_gap == Decimal(87000)
    for old, new in zip(before.calculation.contributions, after.calculation.contributions, strict=True):
        if old.name == "energy":
            assert new.contribution_pp == Decimal(-1)
        else:
            assert old == new
    assert before.analysis_fingerprint != after.analysis_fingerprint


def test_replaying_identical_input_is_deterministic_and_does_not_mutate_it():
    data = request_data()
    unchanged = deepcopy(data)
    first, second = run(data), run(data)
    assert first == second
    assert data == unchanged
    assert len(first.analysis_fingerprint) == 64


def test_negative_gap_is_retained_instead_of_labelled_as_savings():
    data = request_data()
    data["proposal"]["unit_price"] = "101"
    result = run(data)
    assert result.calculation.gap_per_unit == Decimal("-1.30")
    assert result.calculation.annualised_gap == Decimal(-13000)


def test_half_penny_rounding_occurs_at_price_boundary():
    data = request_data()
    for driver in data["drivers"]:
        driver["current_index"] = "100.005"
    result = run(data)
    assert result.calculation.scenario_price == Decimal("100.01")
    assert result.calculation.gap_per_unit == Decimal("11.99")
    assert result.calculation.annualised_gap == Decimal(119900)


def test_same_unsupported_currency_is_withheld_in_bounded_demo():
    data = request_data()
    data["baseline"]["currency"] = data["proposal"]["currency"] = "EUR"
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert "unsupported_currency" in codes(result)


def test_missing_price_evidence_withholds_scenario():
    data = request_data()
    del data["baseline"]["evidence"]["unit_price"]
    result = run(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert any("evidence" in i.code for i in result.issues)


def test_accepted_extreme_numbers_never_crash_decimal_quantisation():
    data = request_data()
    maximum = "9999999999"
    data["baseline"]["unit_price"] = maximum
    data["annual_volume"] = maximum
    for driver in data["drivers"]:
        driver["baseline_index"] = "0.000001"
        driver["current_index"] = maximum
    result = run(data)
    assert result.status == "scenario_available"
    c = result.calculation
    # Integer arithmetic gives an independent exact reference for large accepted inputs.
    expected_price = Decimal(int(maximum) ** 2 * 1_000_000)
    assert c.scenario_price == expected_price
    assert c.annualised_gap.is_finite()
    assert c.annualised_gap < 0

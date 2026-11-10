"""Pure pricing scenario. No HTTP, model calls, file access or approval actions."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .models import Calculation, Contribution, Issue, ReviewRequest, ReviewResponse

MODEL_VERSION = "declared-index-basket/1.0"
MAX_INDEX_AGE_DAYS = 90
LIMITATIONS = [
    "This is a conditional index-basket scenario, not a verified supplier should-cost estimate.",
    "The gap is a discussion point, not evidence of overcharging or a saving achieved.",
    "Shares, indices and annual volume are supplied inputs; their commercial validity requires human review.",
    "The model assumes the baseline price moves proportionally with the declared basket; margin, hedging, productivity, contracts and FX may change the result.",
    "Evidence excerpts establish traceability, not independent verification or semantic correctness.",
]


def rounded(value: Decimal, places="0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def review(request: ReviewRequest) -> ReviewResponse:
    with localcontext() as context:
        context.prec = 80
        return _review(request)


def _review(request: ReviewRequest) -> ReviewResponse:
    issues: list[Issue] = []
    mismatch = False

    def issue(code, message, field):
        issues.append(Issue(code=code, message=message, field=field))

    for side in ("baseline", "proposal"):
        quote = getattr(request, side)
        for field in (
            "supplier",
            "sku",
            "revision",
            "currency",
            "unit",
            "quantity",
            "unit_price",
            "quote_date",
        ):
            if getattr(quote, field) is None:
                issue("missing_quote_fact", f"Supply {side} {field.replace('_', ' ')}.", f"{side}.{field}")
        if quote.freight == "unknown":
            issue("unknown_freight", f"Clarify whether {side} freight is included.", f"{side}.freight")
        if quote.quote_date and quote.quote_date > request.as_of:
            issue("future_quote", f"The {side} quote date is after the review date.", f"{side}.quote_date")
        if quote.currency and quote.currency != "GBP":
            issue(
                "unsupported_currency",
                "This bounded prototype supports GBP only; no currency conversion is performed.",
                f"{side}.currency",
            )
        for field in QuoteFields:
            value = getattr(quote, field)
            if value is not None and value != "unknown" and field not in quote.evidence:
                issue(
                    "missing_quote_evidence",
                    f"Provide a source reference for {side} {field.replace('_', ' ')}.",
                    f"{side}.evidence.{field}",
                )
    baseline, proposal = request.baseline, request.proposal
    for field in ("supplier", "sku", "revision", "currency", "unit", "quantity", "freight"):
        old, new = getattr(baseline, field), getattr(proposal, field)
        if old is not None and new is not None and old != new:
            mismatch = True
            issue(
                "scope_mismatch",
                f"{field.replace('_', ' ').title()} differs between baseline and proposal.",
                field,
            )
    if not proposal.valid_until:
        issue("missing_validity", "Confirm the proposed quote's validity date.", "proposal.valid_until")
    elif proposal.valid_until < request.as_of:
        issue(
            "expired_proposal",
            "The proposed quote has expired; obtain a current quote.",
            "proposal.valid_until",
        )
    if baseline.quote_date and proposal.quote_date and proposal.quote_date < baseline.quote_date:
        issue("reversed_quotes", "The proposal predates the baseline quote.", "proposal.quote_date")

    names = set()
    periods = set()
    for i, driver in enumerate(request.drivers):
        key = driver.name.casefold().strip()
        if key in names:
            issue("duplicate_driver", f"Duplicate driver: {driver.name}.", f"drivers.{i}.name")
        names.add(key)
        for field in (
            "share",
            "baseline_index",
            "current_index",
            "baseline_period",
            "current_period",
            "evidence",
        ):
            if getattr(driver, field) is None:
                issue(
                    "missing_driver_fact",
                    f"Supply {driver.name} {field.replace('_', ' ')}; it is not treated as zero.",
                    f"drivers.{i}.{field}",
                )
        if driver.baseline_period and driver.current_period:
            if baseline.quote_date and (driver.baseline_period.year, driver.baseline_period.month) != (
                baseline.quote_date.year,
                baseline.quote_date.month,
            ):
                issue(
                    "misaligned_price_basis",
                    f"{driver.name}: baseline index month must match the baseline quote month under the demo policy.",
                    f"drivers.{i}.baseline_period",
                )
            periods.add((driver.baseline_period, driver.current_period))
            if driver.current_period < driver.baseline_period:
                issue(
                    "reversed_index_period",
                    f"{driver.name}: current period precedes baseline.",
                    f"drivers.{i}.current_period",
                )
            if driver.current_period > request.as_of:
                issue(
                    "future_index",
                    f"{driver.name}: index period is in the future.",
                    f"drivers.{i}.current_period",
                )
            elif (request.as_of - driver.current_period).days > MAX_INDEX_AGE_DAYS:
                issue(
                    "stale_index",
                    f"{driver.name}: index is more than {MAX_INDEX_AGE_DAYS} days old under the demo policy.",
                    f"drivers.{i}.current_period",
                )
    if len(periods) > 1:
        issue(
            "inconsistent_periods",
            "Driver indices must use the same baseline and current periods.",
            "drivers",
        )
    if all(d.share is not None for d in request.drivers):
        total = sum((d.share for d in request.drivers), Decimal(0))
        if total != Decimal(1):
            issue(
                "incomplete_basket",
                f"Declared shares total {total}; require exactly 1. No silent renormalisation.",
                "drivers",
            )

    evidence = []
    seen = set()
    for e in [
        *baseline.evidence.values(),
        *proposal.evidence.values(),
        *request.metadata_evidence.values(),
        *(d.evidence for d in request.drivers if d.evidence),
    ]:
        signature = (e.sha256, e.page, e.quote)
        if signature not in seen:
            evidence.append(e)
            seen.add(signature)

    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256((MODEL_VERSION + canonical).encode()).hexdigest()
    calculation = None
    if issues:
        status = "not_comparable" if mismatch else "needs_information"
        summary = (
            "Calculation withheld: quote scopes differ."
            if mismatch
            else "Calculation withheld until the missing or inconsistent evidence is resolved."
        )
        questions = list(dict.fromkeys(i.message for i in issues))
    else:
        contributions = []
        change = Decimal(0)
        for d in request.drivers:
            delta = d.current_index / d.baseline_index - 1
            change += d.share * delta
            contributions.append(
                Contribution(
                    name=d.name,
                    share=d.share,
                    index_change_pct=rounded(delta * 100, "0.0001"),
                    contribution_pp=rounded(d.share * delta * 100, "0.0001"),
                )
            )
        # Round once at the price boundary. Annualised gap uses the displayed unit gap.
        scenario = rounded(baseline.unit_price * (1 + change))
        gap = rounded(proposal.unit_price - scenario)
        calculation = Calculation(
            currency=baseline.currency,
            unit=baseline.unit,
            baseline_price=baseline.unit_price,
            proposed_price=proposal.unit_price,
            proposed_change_pct=rounded((proposal.unit_price / baseline.unit_price - 1) * 100, "0.0001"),
            model_change_pct=rounded(change * 100, "0.0001"),
            scenario_price=scenario,
            gap_per_unit=gap,
            annualised_gap=rounded(gap * request.annual_volume) if request.annual_volume else None,
            contributions=contributions,
        )
        status = "scenario_available"
        summary = "The declared cost-driver basket is complete. Review the conditional scenario and discuss the remaining difference with the supplier."
        questions = [
            "Can the supplier reconcile the proposed price with the declared cost shares and index periods?",
            "Do margin, hedging, productivity, volume, FX or contractual terms explain the remaining difference?",
            "Are these indices representative of the supplier's actual purchasing and production costs?",
        ]
    return ReviewResponse(
        review_id=f"review_{fingerprint[:16]}",
        analysis_fingerprint=fingerprint,
        model_version=MODEL_VERSION,
        status=status,
        summary=summary,
        issues=issues,
        calculation=calculation,
        questions=questions,
        assumptions=request.assumptions,
        evidence=evidence,
        extraction_mode=request.extraction_mode,
        limitations=LIMITATIONS,
    )


QuoteFields = (
    "supplier",
    "sku",
    "revision",
    "currency",
    "unit",
    "quantity",
    "unit_price",
    "quote_date",
    "valid_until",
    "freight",
)

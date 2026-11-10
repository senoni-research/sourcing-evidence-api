from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


Money = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=6)]
Share = Annotated[Decimal, Field(ge=0, le=1, max_digits=10, decimal_places=8)]
Label = Annotated[str, Field(min_length=1, max_length=120)]


class Evidence(StrictModel):
    document: Annotated[str, Field(min_length=1, max_length=200)]
    page: Annotated[int, Field(ge=1, le=20, strict=True)]
    quote: Annotated[str, Field(min_length=1, max_length=1000)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Quote(StrictModel):
    supplier: Label | None = None
    sku: Label | None = None
    revision: Label | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    unit: Label | None = None
    quantity: Money | None = None
    unit_price: Money | None = None
    quote_date: date | None = None
    valid_until: date | None = None
    freight: Literal["included", "excluded", "unknown"] = "unknown"
    evidence: dict[str, Evidence] = Field(default_factory=dict, max_length=15)

    @field_validator("quantity", "unit_price", mode="before")
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("A number cannot be a boolean")
        return value

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.quote_date and self.valid_until and self.valid_until < self.quote_date:
            raise ValueError("valid_until must not precede quote_date")
        return self


class Driver(StrictModel):
    name: Label
    share: Share | None = None
    baseline_index: Money | None = None
    current_index: Money | None = None
    baseline_period: date | None = None
    current_period: date | None = None
    evidence: Evidence | None = None

    @field_validator("share", "baseline_index", "current_index", mode="before")
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("A number cannot be a boolean")
        return value


class ReviewRequest(StrictModel):
    as_of: date
    annual_volume: Money | None = None
    baseline: Quote
    proposal: Quote
    drivers: list[Driver] = Field(min_length=1, max_length=12)
    assumptions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=12
    )
    extraction_mode: Literal["structured_input", "template", "openai_compatible"] = "structured_input"
    metadata_evidence: dict[str, Evidence] = Field(default_factory=dict, max_length=15)

    @field_validator("annual_volume", mode="before")
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("A number cannot be a boolean")
        return value


class Issue(StrictModel):
    code: str
    message: str
    field: str


class Contribution(StrictModel):
    name: str
    share: Decimal
    index_change_pct: Decimal
    contribution_pp: Decimal


class Calculation(StrictModel):
    currency: str
    unit: str
    baseline_price: Decimal
    proposed_price: Decimal
    proposed_change_pct: Decimal
    model_change_pct: Decimal
    scenario_price: Decimal
    gap_per_unit: Decimal
    annualised_gap: Decimal | None
    contributions: list[Contribution]


class ReviewResponse(StrictModel):
    review_id: str
    request_id: str = ""
    analysis_fingerprint: str
    model_version: str
    status: Literal["scenario_available", "needs_information", "not_comparable"]
    summary: str
    issues: list[Issue]
    calculation: Calculation | None
    questions: list[str]
    assumptions: list[str]
    evidence: list[Evidence]
    extraction_mode: str
    evidence_validation: str = "not_verified_structured_input"
    limitations: list[str]
    processing_ms: float = 0

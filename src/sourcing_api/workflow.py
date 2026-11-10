"""Evidence-review workflow for the fictional public demonstration.

Only reviewer *decisions* are persisted: the case, an optional allowlisted override with its
justification, optional what-if inputs and the audit events. Facts, results and thresholds are
recomputed from the fictional source pack on every read, so a stored record cannot disagree with
the numbers it is shown beside.

Three kinds of value are kept apart throughout:

* source facts      - parsed from the fictional pack, never rewritten;
* reviewer override - one allowlisted field, with a reason and either a cited passage or an
                      explicit "reviewer assumption" label;
* what-if inputs    - hypothetical index movements, never part of the reviewed result.

Every change carries the review version the caller last saw. A mismatch, either in the service
or inside the storage transaction, is a conflict (HTTP 409) and nothing is written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .domain import QuoteFields, review
from .examples import EXAMPLES, document_pdf
from .extraction import Document, TemplateExtractor, read_document
from .models import Issue, ReviewRequest, ReviewResponse

CaseName = Literal["price_increase", "missing_evidence", "scope_mismatch"]
DriverName = Literal["materials", "energy", "labour", "freight"]
CorrectableField = Literal["proposal.revision", "drivers.energy.current_index"]
ReviewStatus = Literal["scenario_available", "needs_information", "not_comparable"]
ValueOrigin = Literal["source", "reviewer", "not_stated"]

CASES: tuple[str, ...] = ("scope_mismatch", "missing_evidence", "price_increase")
DRIVERS: tuple[str, ...] = ("materials", "energy", "labour", "freight")
# The single field a reviewer may override in each fictional case.
CORRECTABLE: dict[str, str] = {
    "scope_mismatch": "proposal.revision",
    "missing_evidence": "drivers.energy.current_index",
}
MAX_EVENTS = 40
RETENTION = timedelta(days=7)
SCHEMA_VERSION = 2
OWNER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")

QUOTE_LABELS = {
    "supplier": "supplier",
    "sku": "item",
    "revision": "revision",
    "currency": "currency",
    "unit": "unit",
    "quantity": "quantity",
    "unit_price": "unit price",
    "quote_date": "quote date",
    "valid_until": "valid until",
    "freight": "freight",
}
DRIVER_ATTRIBUTES = {
    "share": "cost share",
    "baseline_index": "baseline index",
    "current_index": "current index",
    "baseline_period": "baseline period",
    "current_period": "current period",
}
PAGE_TITLES = ("Baseline quotation", "Price proposal", "Cost-driver assumptions", "Supporting correspondence")


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


# --------------------------------------------------------------------------- errors


class ReviewNotFound(Exception):
    pass


class WorkflowConflict(Exception):
    def __init__(self, message: str, code: str = "stale_review"):
        self.code = code
        super().__init__(message)


class WorkflowUnavailable(Exception):
    pass


class InvalidChange(Exception):
    def __init__(self, message: str, code: str = "invalid_review_change"):
        self.code = code
        super().__init__(message)


class StoredReviewInvalid(Exception):
    pass


STALE = "This review changed elsewhere since you loaded it. The latest saved version is shown; check it and repeat your change if it is still needed."
HISTORY_FULL = (
    f"This review has used all {MAX_EVENTS} history entries. Restart the case to continue; "
    "a restart discards the earlier history and records that it did so."
)

# --------------------------------------------------------------------------- inputs


class Citation(WorkflowModel):
    page: Annotated[int, Field(ge=1, le=10)]
    line: Annotated[int, Field(ge=1, le=200)]


class CorrectionInput(WorkflowModel):
    """A deliberate reviewer override. Nothing is prefilled or inferred by the service."""

    expected_version: Annotated[int, Field(ge=1)]
    field: CorrectableField
    value: Annotated[str, Field(min_length=1, max_length=12)]
    basis: Literal["cited_passage", "reviewer_assumption"]
    citation: Citation | None = None
    reason: Annotated[str, Field(min_length=10, max_length=500)]

    @model_validator(mode="after")
    def consistent(self):
        if self.field == "proposal.revision":
            if not re.fullmatch(r"[A-Za-z0-9]{1,3}", self.value):
                raise ValueError("A revision is one to three letters or digits.")
            self.value = self.value.upper()
        else:
            try:
                number = Decimal(self.value)
            except InvalidOperation as exc:
                raise ValueError("The energy index must be a number.") from exc
            if not number.is_finite() or not 0 < number <= 10000 or -number.as_tuple().exponent > 4:
                raise ValueError("The energy index must be above 0, at most 10000, with up to 4 decimals.")
            self.value = _decimal_text(number)
        if self.basis == "cited_passage" and self.citation is None:
            raise ValueError("Cite the supporting passage, or record the override as a reviewer assumption.")
        if self.basis == "reviewer_assumption" and self.citation is not None:
            raise ValueError("A reviewer assumption does not carry a cited passage.")
        return self


class RevertInput(WorkflowModel):
    expected_version: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class SensitivityInput(WorkflowModel):
    expected_version: Annotated[int, Field(ge=1)]
    changes_pct: dict[DriverName, Annotated[Decimal, Field(ge=-90, le=200, max_digits=7, decimal_places=2)]]

    @model_validator(mode="after")
    def all_drivers(self):
        if set(self.changes_pct) != set(DRIVERS):
            raise ValueError("Supply all four named cost-driver assumptions.")
        return self


class VersionInput(WorkflowModel):
    expected_version: Annotated[int, Field(ge=1)]


class RestartInput(WorkflowModel):
    # Optional only so that an unreadable stored record can still be replaced.
    expected_version: Annotated[int, Field(ge=1)] | None = None


# --------------------------------------------------------------------------- persisted model


class SourceRef(WorkflowModel):
    """Where a value is stated: document identity, page, line and the exact line text."""

    document: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page: int
    line: int
    quote: str


class Correction(WorkflowModel):
    field: CorrectableField
    value: str
    basis: Literal["cited_passage", "reviewer_assumption"]
    citation: SourceRef | None = None
    reason: str
    recorded_at: datetime
    recorded_in_version: int


class WhatIf(WorkflowModel):
    changes_pct: dict[DriverName, Decimal]
    calculated_at: datetime
    calculated_in_version: int


class AuditEvent(WorkflowModel):
    sequence: Annotated[int, Field(ge=1, le=MAX_EVENTS)]
    review_version: int
    at: datetime
    action: Literal[
        "review_opened",
        "review_restarted",
        "correction_applied",
        "correction_amended",
        "correction_reverted",
        "what_if_calculated",
        "what_if_discarded",
    ]
    # "reviewed" events change the reviewed result; "hypothetical" events never do.
    branch: Literal["reviewed", "hypothetical"]
    field: CorrectableField | None = None
    value_before: str | None = None
    value_before_origin: ValueOrigin | None = None
    value_after: str | None = None
    value_after_origin: ValueOrigin | None = None
    basis: Literal["cited_passage", "reviewer_assumption"] | None = None
    citation: SourceRef | None = None
    reason: str = ""
    inputs_before: dict[DriverName, Decimal] | None = None
    inputs_after: dict[DriverName, Decimal] | None = None
    what_if_discarded: bool = False
    discarded_events: int | None = None
    status_before: ReviewStatus | None = None
    status_after: ReviewStatus
    reviewed_fingerprint_before: str | None = None
    reviewed_fingerprint_after: str
    what_if_fingerprint: str | None = None


class StoredReview(WorkflowModel):
    schema_version: Literal[2] = SCHEMA_VERSION
    case: CaseName
    generation: Annotated[int, Field(ge=1)] = 1
    version: Annotated[int, Field(ge=1)] = 1
    correction: Correction | None = None
    what_if: WhatIf | None = None
    events: list[AuditEvent] = Field(min_length=1, max_length=MAX_EVENTS)

    @model_validator(mode="after")
    def coherent(self):
        if [event.sequence for event in self.events] != list(range(1, len(self.events) + 1)):
            raise ValueError("Event sequence numbers must be consecutive.")
        if self.events[-1].review_version != self.version:
            raise ValueError("The latest event must describe the stored version.")
        if self.correction and CORRECTABLE.get(self.case) != self.correction.field:
            raise ValueError("The stored override is not allowed for this case.")
        return self


# --------------------------------------------------------------------------- response model


class SourcePage(WorkflowModel):
    page: int
    title: str
    lines: list[str]


class SourceDocument(WorkflowModel):
    name: str
    sha256: str
    download_path: str
    pages: list[SourcePage]


class Fact(WorkflowModel):
    field: str
    group: Literal["baseline", "proposal", "drivers", "review_basis"]
    label: str
    source_value: str | None = Field(
        description="As stated in the document; null when it states no usable value."
    )
    source: SourceRef | None
    reviewer_value: str | None = Field(
        description="The reviewer's override, if any. Never replaces source_value."
    )
    effective_value: str | None = Field(description="The value the reviewed calculation uses.")
    origin: ValueOrigin
    blocking: bool


class BlockingIssue(WorkflowModel):
    code: str
    message: str
    fields: list[str] = Field(description="Facts this issue refers to; each has its own source reference.")
    correctable_field: CorrectableField | None


class CaseHeader(WorkflowModel):
    """Observed quotation facts. Available even while the derived calculation is withheld."""

    supplier: str | None
    item: str | None
    baseline_revision: str | None
    proposal_revision_source: str | None
    proposal_revision_effective: str | None
    currency: str | None
    unit: str | None
    baseline_price: Decimal | None
    proposed_price: Decimal | None
    proposed_change_pct: Decimal | None
    as_of: date


class CorrectionView(Correction):
    label: str
    source_value: str | None
    citation_mentions_value: bool | None = Field(
        description="Text match only. It is not a judgement that the passage supports the override."
    )
    resolves_review: bool


class WhatIfView(WhatIf):
    result: ReviewResponse


class ThresholdRow(WorkflowModel):
    driver: DriverName
    share: Decimal
    held_movement_pct: Decimal = Field(description="This driver's own movement under the stated basis.")
    required_movement_pct: Decimal | None = Field(
        description="Movement of this driver alone that reaches the proposed price, the other three held."
    )


class ThresholdSet(WorkflowModel):
    basis: Literal["reviewed", "what_if"]
    target_price: Decimal
    rows: list[ThresholdRow]


class HistoryUse(WorkflowModel):
    limit: int
    used: int
    remaining: int


class StorageInfo(WorkflowModel):
    backend: Literal["firestore", "local_test_store", "memory"]
    durable: bool
    retention_days: int
    expires_at: datetime


class Integrity(WorkflowModel):
    latest_reviewed_event_matches_result: bool
    latest_what_if_event_matches_result: bool | None
    citation_matches_current_document: bool | None


class ReviewView(WorkflowModel):
    case: CaseName
    title: str
    generation: int
    version: int
    outcome: Literal["opened", "resumed", "restarted", "recorded", "no_change"]
    header: CaseHeader
    status: ReviewStatus
    blocking: list[BlockingIssue]
    resolved_by_override: list[BlockingIssue]
    correctable_field: CorrectableField | None
    facts: list[Fact]
    correction: CorrectionView | None
    reviewed: ReviewResponse
    what_if_available: bool
    what_if: WhatIfView | None
    thresholds: list[ThresholdSet]
    events: list[AuditEvent]
    history: HistoryUse
    source: SourceDocument
    storage: StorageInfo
    integrity: Integrity


class CaseReviewSummary(WorkflowModel):
    version: int
    status: ReviewStatus
    has_override: bool
    has_what_if: bool
    events: int


class CaseSummary(WorkflowModel):
    case: CaseName
    title: str
    description: str
    review: CaseReviewSummary | None


class CaseList(WorkflowModel):
    cases: list[CaseSummary]


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(UTC)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _text(value) -> str | None:
    if value is None or value == "unknown":
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def owner_hash(token: str) -> str:
    return hashlib.sha256(("sourcing-demo-owner/v2:" + token).encode()).hexdigest()


def new_owner_token() -> str:
    return secrets.token_urlsafe(32)


def valid_owner_token(token: str | None) -> bool:
    return bool(token) and OWNER_TOKEN.fullmatch(token) is not None


@dataclass(frozen=True)
class Source:
    document: Document
    request: ReviewRequest
    lines: tuple[tuple[str, ...], ...]


_SOURCES: dict[str, Source] = {}
_SOURCE_LOCK = threading.Lock()


def _load_source(case: str) -> Source:
    """Parse the downloadable fictional PDF once; evidence hashes refer to those exact bytes."""
    with _SOURCE_LOCK:
        if case not in _SOURCES:
            document = read_document(document_pdf(case, supporting=True), case + ".pdf")
            # The labelled-template extractor has no awaits; drive its coroutine to completion.
            coroutine = TemplateExtractor().extract(document)
            try:
                coroutine.send(None)
            except StopIteration as done:
                request = done.value
            else:  # pragma: no cover - the template extractor never suspends
                raise RuntimeError("The template extractor suspended unexpectedly.")
            lines = tuple(tuple(line.strip() for line in page.splitlines()) for page in document.pages)
            _SOURCES[case] = Source(document, request, lines)
        return _SOURCES[case]


def _ref(source: Source, page: int, quote: str) -> SourceRef:
    lines = source.lines[page - 1]
    line = lines.index(quote.strip()) + 1
    return SourceRef(
        document=source.document.name,
        sha256=source.document.sha256,
        page=page,
        line=line,
        quote=quote.strip(),
    )


def _evidence_ref(source: Source, evidence) -> SourceRef | None:
    return _ref(source, evidence.page, evidence.quote) if evidence else None


def _energy(data: ReviewRequest):
    return next(driver for driver in data.drivers if driver.name == "energy")


def _apply(data: ReviewRequest, correction: Correction | None) -> ReviewRequest:
    data = data.model_copy(deep=True)
    if correction is None:
        return data
    if correction.field == "proposal.revision":
        data.proposal.revision = correction.value
    else:
        _energy(data).current_index = Decimal(correction.value)
    return data


def _source_value(data: ReviewRequest, field: str) -> str | None:
    if field == "proposal.revision":
        return _text(data.proposal.revision)
    return _text(_energy(data).current_index)


def _what_if_result(data: ReviewRequest, changes: dict[str, Decimal]) -> ReviewResponse:
    data = data.model_copy(deep=True)
    for driver in data.drivers:
        driver.current_index = driver.baseline_index * (1 + changes[driver.name] / 100)
    return review(data)


def _issue_fields(issue: Issue, data: ReviewRequest) -> list[str]:
    """Map a domain issue to the typed facts it concerns."""
    parts = issue.field.split(".")
    if len(parts) == 1 and parts[0] in QuoteFields:
        return [f"baseline.{parts[0]}", f"proposal.{parts[0]}"]
    if parts[0] in ("baseline", "proposal") and parts[-1] in QuoteFields:
        return [f"{parts[0]}.{parts[-1]}"]
    if parts[0] == "drivers" and len(parts) == 3 and parts[1].isdigit() and int(parts[1]) < len(data.drivers):
        name = data.drivers[int(parts[1])].name
        attribute = parts[2] if parts[2] in DRIVER_ATTRIBUTES else "share"
        return [f"drivers.{name}.{attribute}"]
    return []


def _blocking(issues: list[Issue], data: ReviewRequest, case: str) -> list[BlockingIssue]:
    result = []
    for issue in issues:
        fields = _issue_fields(issue, data)
        correctable = CORRECTABLE.get(case)
        result.append(
            BlockingIssue(
                code=issue.code,
                message=issue.message,
                fields=fields,
                correctable_field=correctable if correctable in fields else None,
            )
        )
    return result


def _thresholds(data: ReviewRequest, basis: str, movements: dict[str, Decimal]) -> ThresholdSet:
    """Single-driver movement that reaches the proposal, holding the others at ``movements``."""
    target = data.proposal.unit_price / data.baseline.unit_price - 1
    rows = []
    for driver in data.drivers:
        others = sum(
            (other.share * movements[other.name] for other in data.drivers if other.name != driver.name),
            Decimal(0),
        )
        required = None
        if driver.share:
            required = ((target - others) / driver.share * 100).quantize(Decimal("0.0001"))
        rows.append(
            ThresholdRow(
                driver=driver.name,
                share=driver.share,
                held_movement_pct=(movements[driver.name] * 100).quantize(Decimal("0.0001")),
                required_movement_pct=required,
            )
        )
    return ThresholdSet(basis=basis, target_price=data.proposal.unit_price, rows=rows)


def _citation_mentions(field: str, value: str, quote: str) -> bool:
    """Lexical check only: the cited line states the asserted value."""
    if field == "proposal.revision":
        return re.search(rf"\brevision\W{{0,3}}{re.escape(value)}\b", quote, re.IGNORECASE) is not None
    numbers = re.findall(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w-]|\.\d)", quote)
    return any(Decimal(number) == Decimal(value) for number in numbers)


def _facts(source: Source, correction: Correction | None, blocked: set[str]) -> list[Fact]:
    facts: list[Fact] = []

    def add(field, group, label, source_value, ref):
        reviewer = correction.value if correction and correction.field == field else None
        text = _text(source_value)
        facts.append(
            Fact(
                field=field,
                group=group,
                label=label,
                source_value=text,
                source=ref,
                reviewer_value=reviewer,
                effective_value=reviewer if reviewer is not None else text,
                origin="reviewer" if reviewer is not None else "source" if text is not None else "not_stated",
                blocking=field in blocked,
            )
        )

    data = source.request
    for side in ("baseline", "proposal"):
        quote = getattr(data, side)
        for name in QuoteFields:
            add(
                f"{side}.{name}",
                side,
                f"{side.title()} {QUOTE_LABELS[name]}",
                getattr(quote, name),
                _evidence_ref(source, quote.evidence.get(name)),
            )
    for driver in data.drivers:
        for attribute, label in DRIVER_ATTRIBUTES.items():
            add(
                f"drivers.{driver.name}.{attribute}",
                "drivers",
                f"{driver.name.title()} {label}",
                getattr(driver, attribute),
                _evidence_ref(source, driver.evidence),
            )
    add(
        "as_of",
        "review_basis",
        "Review date",
        data.as_of,
        _evidence_ref(source, data.metadata_evidence.get("as_of")),
    )
    add(
        "annual_volume",
        "review_basis",
        "Annual volume (illustrative)",
        data.annual_volume,
        _evidence_ref(source, data.metadata_evidence.get("annual_volume")),
    )
    return facts


def _header(source: ReviewRequest, effective: ReviewRequest) -> CaseHeader:
    base, proposal = source.baseline, source.proposal
    change = None
    if base.unit_price and proposal.unit_price and base.currency == proposal.currency:
        change = ((proposal.unit_price / base.unit_price - 1) * 100).quantize(Decimal("0.01"))
    return CaseHeader(
        supplier=proposal.supplier or base.supplier,
        item=proposal.sku or base.sku,
        baseline_revision=base.revision,
        proposal_revision_source=proposal.revision,
        proposal_revision_effective=effective.proposal.revision,
        currency=base.currency,
        unit=base.unit,
        baseline_price=base.unit_price,
        proposed_price=proposal.unit_price,
        proposed_change_pct=change,
        as_of=source.as_of,
    )


# --------------------------------------------------------------------------- stores


@dataclass(frozen=True)
class StoredRecord:
    payload: dict
    version: int
    expires_at: datetime


class WorkflowStore(Protocol):
    kind: str

    def get(self, case: str, owner: str) -> StoredRecord | None: ...

    def create(self, case: str, owner: str, payload: dict, version: int) -> StoredRecord: ...

    def replace(
        self, case: str, owner: str, payload: dict, version: int, expected_version: int | None
    ) -> StoredRecord: ...


class MemoryWorkflowStore:
    """Explicitly non-durable development/test adapter, never a production claim."""

    kind = "memory"

    def __init__(self):
        self._records: dict[tuple[str, str], StoredRecord] = {}
        self._lock = threading.Lock()

    def get(self, case, owner):
        with self._lock:
            record = self._records.get((case, owner))
            if record is None or record.expires_at <= _now():
                return None
            return deepcopy(record)

    def create(self, case, owner, payload, version):
        with self._lock:
            existing = self._records.get((case, owner))
            if existing is not None and existing.expires_at > _now():
                raise WorkflowConflict("A review already exists for this case.", "review_exists")
            return self._put(case, owner, payload, version)

    def replace(self, case, owner, payload, version, expected_version):
        with self._lock:
            existing = self._records.get((case, owner))
            if existing is None or existing.expires_at <= _now():
                raise ReviewNotFound("Review not found or expired.")
            # The comparison and the write share one lock: a stale writer can never overwrite.
            if expected_version is not None and existing.version != expected_version:
                raise WorkflowConflict(STALE)
            return self._put(case, owner, payload, version)

    def _put(self, case, owner, payload, version):
        record = StoredRecord(deepcopy(payload), version, _now() + RETENTION)
        self._records[(case, owner)] = record
        self._persist()
        return deepcopy(record)

    def _persist(self):
        pass


class LocalWorkflowStore(MemoryWorkflowStore):
    """Single-process JSON persistence for local verification. Not a deployment claim."""

    kind = "local_test_store"

    def __init__(self, path: str):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for item in json.loads(self.path.read_text()):
                self._records[(item["case"], item["owner"])] = StoredRecord(
                    item["payload"], item["version"], datetime.fromisoformat(item["expires_at"])
                )

    def _persist(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                [
                    {
                        "case": case,
                        "owner": owner,
                        "payload": record.payload,
                        "version": record.version,
                        "expires_at": record.expires_at.isoformat(),
                    }
                    for (case, owner), record in self._records.items()
                ]
            )
        )
        temporary.replace(self.path)


class FirestoreWorkflowStore:
    """One small document per owner and case. Replacement compares versions in a transaction."""

    kind = "firestore"
    COLLECTION = "sourcing_evidence_demo_reviews_v2"

    def __init__(
        self,
        project: str | None = None,
        database: str = "(default)",
        collection: str | None = None,
        client=None,
    ):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project or os.getenv("GOOGLE_CLOUD_PROJECT"), database=database)
        self.client = client
        self.collection = client.collection(collection or self.COLLECTION)

    def _reference(self, case: str, owner: str):
        return self.collection.document(hashlib.sha256((case + ":" + owner).encode()).hexdigest())

    @staticmethod
    def _document(case, owner, payload, version):
        return {
            "case": case,
            "owner_hash": owner,
            "version": version,
            "state": payload,
            "expires_at": _now() + RETENTION,
        }

    @staticmethod
    def _live(snapshot, case, owner) -> dict | None:
        """Return the stored fields of an existing, matching, unexpired document."""
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("case") != case or data.get("owner_hash") != owner:
            return None
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, datetime):
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= _now():
            return None
        return {**data, "expires_at": expires_at}

    def get(self, case, owner):
        data = self._live(self._reference(case, owner).get(), case, owner)
        if data is None:
            return None
        version = data.get("version")
        return StoredRecord(
            data.get("state") if isinstance(data.get("state"), dict) else {},
            version if isinstance(version, int) else 0,
            data["expires_at"],
        )

    def create(self, case, owner, payload, version):
        from google.cloud import firestore

        reference = self._reference(case, owner)
        document = self._document(case, owner, payload, version)

        @firestore.transactional
        def write(transaction):
            # An expired or foreign document may be replaced; a live one may not.
            if self._live(reference.get(transaction=transaction), case, owner) is not None:
                raise WorkflowConflict("A review already exists for this case.", "review_exists")
            transaction.set(reference, document)

        write(self.client.transaction())
        return StoredRecord(payload, version, document["expires_at"])

    def replace(self, case, owner, payload, version, expected_version):
        from google.cloud import firestore

        reference = self._reference(case, owner)
        document = self._document(case, owner, payload, version)

        @firestore.transactional
        def write(transaction):
            self.replace_in_transaction(transaction, reference, case, owner, document, expected_version)

        write(self.client.transaction())
        return StoredRecord(payload, version, document["expires_at"])

    def replace_in_transaction(self, transaction, reference, case, owner, document, expected_version):
        """Read, compare and write inside one transaction. A stale caller writes nothing."""
        current = self._live(reference.get(transaction=transaction), case, owner)
        if current is None:
            raise ReviewNotFound("Review not found or expired.")
        # Read the version exactly as get() reports it, so a damaged record stays replaceable.
        version = current.get("version")
        version = version if isinstance(version, int) else 0
        if expected_version is not None and version != expected_version:
            raise WorkflowConflict(STALE)
        transaction.set(reference, document)


# --------------------------------------------------------------------------- service


class WorkflowService:
    def __init__(self, store: WorkflowStore):
        self.store = store
        self.timeout_seconds = 8

    async def _call_store(self, method, *args):
        try:
            return await asyncio.wait_for(run_in_threadpool(method, *args), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise WorkflowUnavailable("The review store did not respond. Please try again shortly.") from exc

    # -- reads ---------------------------------------------------------------

    async def open(self, case: str, token: str | None) -> tuple[ReviewView, str]:
        """Resume this owner's review of the case, or create it. The owner token is stable."""
        self._require_case(case)
        if not valid_owner_token(token):
            token = new_owner_token()
        else:
            record = await self._call_store(self.store.get, case, owner_hash(token))
            if record is not None:
                return self._view(self._validated(record), record, "resumed"), token
        stored = self._fresh(case, generation=1, version=1, action="review_opened")
        try:
            record = await self._save(case, token, stored, create=True)
        except WorkflowConflict:
            # Two tabs opened the same case at once; the other one created it first.
            return await self.get(case, token), token
        return self._view(stored, record, "opened"), token

    async def get(self, case: str, token: str | None) -> ReviewView:
        stored, record = await self._load(case, token)
        return self._view(stored, record, "resumed")

    async def summaries(self, token: str | None) -> CaseList:
        titles = {example["id"]: example for example in EXAMPLES}
        cases = []
        for case in CASES:
            summary = None
            if valid_owner_token(token):
                record = await self._call_store(self.store.get, case, owner_hash(token))
                if record is not None:
                    try:
                        stored = self._validated(record)
                    except StoredReviewInvalid:
                        stored = None
                    if stored is not None:
                        summary = CaseReviewSummary(
                            version=stored.version,
                            status=stored.events[-1].status_after,
                            has_override=stored.correction is not None,
                            has_what_if=stored.what_if is not None,
                            events=len(stored.events),
                        )
            cases.append(
                CaseSummary(
                    case=case,
                    title=titles[case]["title"],
                    description=titles[case]["description"],
                    review=summary,
                )
            )
        return CaseList(cases=cases)

    # -- changes -------------------------------------------------------------

    async def correct(self, case: str, token: str | None, change: CorrectionInput) -> ReviewView:
        stored, record = await self._load(case, token)
        self._require_version(stored, change.expected_version)
        source = _load_source(case)
        if CORRECTABLE.get(case) != change.field:
            raise InvalidChange(
                "This fact is not open to reviewer override in this case; only a field with a "
                "blocking issue can be overridden.",
                "field_not_correctable",
            )
        source_value = _source_value(source.request, change.field)
        if source_value is not None and self._same_value(change.field, change.value, source_value):
            raise InvalidChange(
                "That is the value the source document already states, so there is nothing to override. "
                "To return to the source value, revert the override instead.",
                "matches_source",
            )
        citation = None
        if change.basis == "cited_passage":
            citation = self._citation(source, change.citation)
            if not _citation_mentions(change.field, change.value, citation.quote):
                raise InvalidChange(
                    f"The cited passage (page {citation.page}, line {citation.line}) does not state the value "
                    f"{change.value}. Cite a passage that states it, or record the override as a reviewer assumption.",
                    "citation_does_not_state_value",
                )
        previous = stored.correction
        if (
            previous is not None
            and self._same_value(change.field, previous.value, change.value)
            and previous.basis == change.basis
            and previous.citation == citation
            and previous.reason == change.reason
        ):
            return self._view(stored, record, "no_change")
        self._require_capacity(stored)
        before = review(_apply(source.request, previous))
        version = stored.version + 1
        correction = Correction(
            field=change.field,
            value=change.value,
            basis=change.basis,
            citation=citation,
            reason=change.reason,
            recorded_at=_now(),
            recorded_in_version=version,
        )
        after = review(_apply(source.request, correction))
        event = AuditEvent(
            sequence=len(stored.events) + 1,
            review_version=version,
            at=correction.recorded_at,
            action="correction_amended" if previous else "correction_applied",
            branch="reviewed",
            field=change.field,
            value_before=previous.value if previous else source_value,
            value_before_origin="reviewer"
            if previous
            else "source"
            if source_value is not None
            else "not_stated",
            value_after=change.value,
            value_after_origin="reviewer",
            basis=change.basis,
            citation=citation,
            reason=change.reason,
            # A what-if describes hypothetical movements from a reviewed basis; when that basis
            # changes, the earlier what-if is discarded rather than silently re-attached.
            what_if_discarded=stored.what_if is not None,
            inputs_before=stored.what_if.changes_pct if stored.what_if else None,
            status_before=before.status,
            status_after=after.status,
            reviewed_fingerprint_before=before.analysis_fingerprint,
            reviewed_fingerprint_after=after.analysis_fingerprint,
        )
        updated = stored.model_copy(
            update={
                "version": version,
                "correction": correction,
                "what_if": None,
                "events": [*stored.events, event],
            }
        )
        return self._view(updated, await self._save(case, token, updated, stored.version), "recorded")

    async def revert(self, case: str, token: str | None, change: RevertInput) -> ReviewView:
        stored, record = await self._load(case, token)
        self._require_version(stored, change.expected_version)
        if stored.correction is None:
            return self._view(stored, record, "no_change")
        self._require_capacity(stored)
        source = _load_source(case)
        before = review(_apply(source.request, stored.correction))
        after = review(source.request)
        source_value = _source_value(source.request, stored.correction.field)
        version = stored.version + 1
        event = AuditEvent(
            sequence=len(stored.events) + 1,
            review_version=version,
            at=_now(),
            action="correction_reverted",
            branch="reviewed",
            field=stored.correction.field,
            value_before=stored.correction.value,
            value_before_origin="reviewer",
            value_after=source_value,
            value_after_origin="source" if source_value is not None else "not_stated",
            reason=change.reason,
            what_if_discarded=stored.what_if is not None,
            inputs_before=stored.what_if.changes_pct if stored.what_if else None,
            status_before=before.status,
            status_after=after.status,
            reviewed_fingerprint_before=before.analysis_fingerprint,
            reviewed_fingerprint_after=after.analysis_fingerprint,
        )
        updated = stored.model_copy(
            update={
                "version": version,
                "correction": None,
                "what_if": None,
                "events": [*stored.events, event],
            }
        )
        return self._view(updated, await self._save(case, token, updated, stored.version), "recorded")

    async def what_if(self, case: str, token: str | None, change: SensitivityInput) -> ReviewView:
        stored, record = await self._load(case, token)
        self._require_version(stored, change.expected_version)
        data = _apply(_load_source(case).request, stored.correction)
        reviewed = review(data)
        if reviewed.status != "scenario_available":
            raise InvalidChange(
                "A what-if is unavailable while the review is blocked. Resolve the blocking issue first.",
                "review_blocked",
            )
        previous = stored.what_if.changes_pct if stored.what_if else None
        if previous is not None and all(previous[name] == change.changes_pct[name] for name in DRIVERS):
            return self._view(stored, record, "no_change")
        self._require_capacity(stored)
        version = stored.version + 1
        result = _what_if_result(data, change.changes_pct)
        event = AuditEvent(
            sequence=len(stored.events) + 1,
            review_version=version,
            at=_now(),
            action="what_if_calculated",
            branch="hypothetical",
            inputs_before=previous,
            inputs_after=change.changes_pct,
            status_before=reviewed.status,
            status_after=reviewed.status,
            reviewed_fingerprint_before=reviewed.analysis_fingerprint,
            reviewed_fingerprint_after=reviewed.analysis_fingerprint,
            what_if_fingerprint=result.analysis_fingerprint,
        )
        updated = stored.model_copy(
            update={
                "version": version,
                "what_if": WhatIf(
                    changes_pct=change.changes_pct, calculated_at=event.at, calculated_in_version=version
                ),
                "events": [*stored.events, event],
            }
        )
        return self._view(updated, await self._save(case, token, updated, stored.version), "recorded")

    async def discard_what_if(self, case: str, token: str | None, change: VersionInput) -> ReviewView:
        stored, record = await self._load(case, token)
        self._require_version(stored, change.expected_version)
        if stored.what_if is None:
            return self._view(stored, record, "no_change")
        self._require_capacity(stored)
        reviewed = review(_apply(_load_source(case).request, stored.correction))
        version = stored.version + 1
        event = AuditEvent(
            sequence=len(stored.events) + 1,
            review_version=version,
            at=_now(),
            action="what_if_discarded",
            branch="hypothetical",
            inputs_before=stored.what_if.changes_pct,
            what_if_discarded=True,
            status_before=reviewed.status,
            status_after=reviewed.status,
            reviewed_fingerprint_before=reviewed.analysis_fingerprint,
            reviewed_fingerprint_after=reviewed.analysis_fingerprint,
        )
        updated = stored.model_copy(
            update={"version": version, "what_if": None, "events": [*stored.events, event]}
        )
        return self._view(updated, await self._save(case, token, updated, stored.version), "recorded")

    async def restart(self, case: str, token: str | None, change: RestartInput) -> ReviewView:
        """Begin the case again. Earlier history is discarded, not archived; the first event says so."""
        self._require_case(case)
        if not valid_owner_token(token):
            raise ReviewNotFound("Review not found. Open the case first.")
        record = await self._call_store(self.store.get, case, owner_hash(token))
        if record is None:
            raise ReviewNotFound("Review not found or expired. Open the case first.")
        try:
            previous = self._validated(record)
        except StoredReviewInvalid:
            # The content is unreadable, but the stored version number is not. A caller that names
            # a version must name that one, and the replacement is conditional on the record still
            # being at the version read here, so it cannot overwrite a concurrent change.
            if change.expected_version is not None and change.expected_version != record.version:
                raise WorkflowConflict(STALE) from None
            stored = self._fresh(case, 1, max(record.version, 0) + 1, "review_restarted")
            return self._view(stored, await self._save(case, token, stored, record.version), "restarted")
        if change.expected_version is None:
            raise InvalidChange("Supply the review version you are restarting from.", "version_required")
        self._require_version(previous, change.expected_version)
        stored = self._fresh(
            case,
            previous.generation + 1,
            previous.version + 1,
            "review_restarted",
            discarded_events=len(previous.events),
            fingerprint_before=previous.events[-1].reviewed_fingerprint_after,
            status_before=previous.events[-1].status_after,
            what_if_discarded=previous.what_if is not None,
        )
        return self._view(stored, await self._save(case, token, stored, previous.version), "restarted")

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _require_case(case: str):
        if case not in CASES:
            raise ReviewNotFound("Unknown fictional case.")

    @staticmethod
    def _require_version(stored: StoredReview, expected: int):
        if stored.version != expected:
            raise WorkflowConflict(STALE)

    @staticmethod
    def _require_capacity(stored: StoredReview):
        if len(stored.events) >= MAX_EVENTS:
            raise WorkflowConflict(HISTORY_FULL, "history_full")

    @staticmethod
    def _same_value(field: str, left: str, right: str) -> bool:
        if field == "proposal.revision":
            return left.upper() == right.upper()
        return Decimal(left) == Decimal(right)

    @staticmethod
    def _citation(source: Source, citation: Citation) -> SourceRef:
        if citation.page > len(source.lines) or citation.line > len(source.lines[citation.page - 1]):
            raise InvalidChange("The cited page or line is not in the source document.", "citation_not_found")
        quote = source.lines[citation.page - 1][citation.line - 1]
        if not quote:
            raise InvalidChange("The cited line is empty.", "citation_not_found")
        return SourceRef(
            document=source.document.name,
            sha256=source.document.sha256,
            page=citation.page,
            line=citation.line,
            quote=quote,
        )

    @staticmethod
    def _cited_line(source: Source, citation: SourceRef) -> str | None:
        if citation.page > len(source.lines) or citation.line > len(source.lines[citation.page - 1]):
            return None
        return source.lines[citation.page - 1][citation.line - 1]

    @staticmethod
    def _fresh(case, generation, version, action, **extra) -> StoredReview:
        result = review(_load_source(case).request)
        event = AuditEvent(
            sequence=1,
            review_version=version,
            at=_now(),
            action=action,
            branch="reviewed",
            status_before=extra.get("status_before"),
            status_after=result.status,
            reviewed_fingerprint_before=extra.get("fingerprint_before"),
            reviewed_fingerprint_after=result.analysis_fingerprint,
            discarded_events=extra.get("discarded_events"),
            what_if_discarded=extra.get("what_if_discarded", False),
        )
        return StoredReview(case=case, generation=generation, version=version, events=[event])

    @staticmethod
    def _validated(record: StoredRecord) -> StoredReview:
        try:
            stored = StoredReview.model_validate(record.payload)
        except ValidationError as exc:
            raise StoredReviewInvalid(
                "The saved review could not be read. Restart this case to continue."
            ) from exc
        if stored.version != record.version:
            raise StoredReviewInvalid("The saved review could not be read. Restart this case to continue.")
        return stored

    async def _load(self, case: str, token: str | None) -> tuple[StoredReview, StoredRecord]:
        self._require_case(case)
        if not valid_owner_token(token):
            raise ReviewNotFound("Review not found. Open the case first.")
        record = await self._call_store(self.store.get, case, owner_hash(token))
        if record is None:
            raise ReviewNotFound("Review not found or expired. Open the case first.")
        return self._validated(record), record

    async def _save(
        self, case, token, stored: StoredReview, expected_version=None, create=False
    ) -> StoredRecord:
        # Validate the complete state exactly as it will be read back, before anything is written.
        payload = stored.model_dump(mode="json")
        StoredReview.model_validate(payload)
        if create:
            return await self._call_store(self.store.create, case, owner_hash(token), payload, stored.version)
        return await self._call_store(
            self.store.replace, case, owner_hash(token), payload, stored.version, expected_version
        )

    def _view(self, stored: StoredReview, record: StoredRecord, outcome: str) -> ReviewView:
        case = stored.case
        source = _load_source(case)
        source_review = review(source.request)
        effective = _apply(source.request, stored.correction)
        reviewed = review(effective)
        blocking = _blocking(reviewed.issues, effective, case)
        remaining = {(issue.code, tuple(issue.fields)) for issue in blocking}
        resolved = [
            issue
            for issue in _blocking(source_review.issues, source.request, case)
            if (issue.code, tuple(issue.fields)) not in remaining
        ]
        blocked_fields = {field for issue in blocking for field in issue.fields}
        available = reviewed.status == "scenario_available"

        correction = None
        if stored.correction:
            label = next(
                fact.label for fact in _facts(source, None, set()) if fact.field == stored.correction.field
            )
            correction = CorrectionView(
                **stored.correction.model_dump(),
                label=label,
                source_value=_source_value(source.request, stored.correction.field),
                citation_mentions_value=(
                    _citation_mentions(
                        stored.correction.field, stored.correction.value, stored.correction.citation.quote
                    )
                    if stored.correction.citation
                    else None
                ),
                resolves_review=available,
            )

        what_if = None
        thresholds = []
        if available:
            movements = {
                driver.name: driver.current_index / driver.baseline_index - 1 for driver in effective.drivers
            }
            thresholds.append(_thresholds(effective, "reviewed", movements))
            if stored.what_if:
                result = _what_if_result(effective, stored.what_if.changes_pct)
                what_if = WhatIfView(**stored.what_if.model_dump(), result=result)
                thresholds.append(
                    _thresholds(
                        effective,
                        "what_if",
                        {name: value / 100 for name, value in stored.what_if.changes_pct.items()},
                    )
                )

        latest_reviewed = stored.events[-1].reviewed_fingerprint_after
        latest_what_if = next(
            (event for event in reversed(stored.events) if event.action == "what_if_calculated"), None
        )
        titles = {example["id"]: example["title"] for example in EXAMPLES}
        return ReviewView(
            case=case,
            title=titles[case],
            generation=stored.generation,
            version=stored.version,
            outcome=outcome,
            header=_header(source.request, effective),
            status=reviewed.status,
            blocking=blocking,
            resolved_by_override=resolved,
            correctable_field=CORRECTABLE.get(case),
            facts=_facts(source, stored.correction, blocked_fields),
            correction=correction,
            reviewed=reviewed,
            what_if_available=available,
            what_if=what_if,
            thresholds=thresholds,
            events=stored.events,
            history=HistoryUse(
                limit=MAX_EVENTS, used=len(stored.events), remaining=MAX_EVENTS - len(stored.events)
            ),
            source=SourceDocument(
                name=source.document.name,
                sha256=source.document.sha256,
                download_path=f"/demo/{case}/document",
                pages=[
                    SourcePage(page=number, title=PAGE_TITLES[number - 1], lines=list(lines))
                    for number, lines in enumerate(source.lines, 1)
                ],
            ),
            storage=StorageInfo(
                backend=self.store.kind,
                durable=self.store.kind == "firestore",
                retention_days=RETENTION.days,
                expires_at=record.expires_at,
            ),
            integrity=Integrity(
                latest_reviewed_event_matches_result=latest_reviewed == reviewed.analysis_fingerprint,
                latest_what_if_event_matches_result=(
                    latest_what_if.what_if_fingerprint == what_if.result.analysis_fingerprint
                    if what_if and latest_what_if
                    else None
                ),
                citation_matches_current_document=(
                    stored.correction.citation.sha256 == source.document.sha256
                    and self._cited_line(source, stored.correction.citation)
                    == stored.correction.citation.quote
                    if stored.correction and stored.correction.citation
                    else None
                ),
            ),
        )

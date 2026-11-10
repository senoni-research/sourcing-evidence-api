"""The parser/provider boundary must fail closed and preserve source provenance.

All HTTP calls are mocked. These tests make no live-model accuracy claim.
"""

import asyncio
import hashlib
import io
import json
from decimal import Decimal

import httpx
import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from sourcing_api.extraction import (
    MAX_FILE_BYTES,
    MAX_PAGES,
    MAX_TEXT_CHARS,
    ExtractionFailure,
    OpenAICompatibleExtractor,
    TemplateExtractor,
    read_document,
    validate_evidence,
)


def source_text():
    baseline = "\n".join(
        [
            "SENONI fictional test fixture",
            "as_of = 2026-11-09",
            "annual_volume = 10000",
            "baseline.supplier = Fictional Components Ltd",
            "baseline.sku = PART-001",
            "baseline.revision = A",
            "baseline.currency = GBP",
            "baseline.unit = piece",
            "baseline.quantity = 1000",
            "baseline.unit_price = 100",
            "baseline.quote_date = 2026-01-01",
            "baseline.valid_until = 2026-02-01",
            "baseline.freight = included",
        ]
    )
    proposal = "\n".join(
        [
            "proposal.supplier = Fictional Components Ltd",
            "proposal.sku = PART-001",
            "proposal.revision = A",
            "proposal.currency = GBP",
            "proposal.unit = piece",
            "proposal.quantity = 1000",
            "proposal.unit_price = 112",
            "proposal.quote_date = 2026-11-01",
            "proposal.valid_until = 2026-11-30",
            "proposal.freight = included",
        ]
    )
    drivers = "\n".join(
        [
            "driver.materials = share:0.5; baseline_index:100; current_index:106; baseline_period:2026-01-01; current_period:2026-10-01",
            "driver.energy = share:0.2; baseline_index:100; current_index:90; baseline_period:2026-01-01; current_period:2026-10-01",
            "driver.labour = share:0.2; baseline_index:100; current_index:104; baseline_period:2026-01-01; current_period:2026-10-01",
            "driver.freight = share:0.1; baseline_index:100; current_index:105; baseline_period:2026-01-01; current_period:2026-10-01",
            "assumption = Supplied cost shares represent the complete price basket.",
            "assumption = Margin, hedging and productivity are held constant in this scenario.",
        ]
    )
    return "\f".join([baseline, proposal, drivers])


@pytest.fixture
def document():
    return read_document(source_text().encode(), "fictional-review.txt")


@pytest.fixture
def extracted(document):
    return asyncio.run(TemplateExtractor().extract(document))


def failure_from_text(text):
    document = read_document(text.encode(), "fixture.txt")
    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(TemplateExtractor().extract(document))
    assert error.value.status == 422
    return error.value


def test_labelled_extraction_preserves_prices_pages_and_sha(document, extracted):
    assert extracted.baseline.unit_price == Decimal("100")
    assert extracted.proposal.unit_price == Decimal("112")
    assert extracted.extraction_mode == "template"
    assert extracted.baseline.evidence["unit_price"].page == 1
    assert extracted.proposal.evidence["unit_price"].page == 2
    assert extracted.drivers[0].evidence.page == 3
    assert extracted.proposal.evidence["unit_price"].quote == "proposal.unit_price = 112"
    assert (
        extracted.proposal.evidence["unit_price"].sha256 == hashlib.sha256(source_text().encode()).hexdigest()
    )
    assert extracted.metadata_evidence["annual_volume"].page == 1
    assert len(extracted.assumptions) == 2
    validate_evidence(extracted, document)


def test_unknown_commercial_fact_is_preserved_as_missing():
    text = source_text().replace("proposal.unit_price = 112", "proposal.unit_price = UNKNOWN")
    result = asyncio.run(TemplateExtractor().extract(read_document(text.encode(), "fixture.txt")))
    assert result.proposal.unit_price is None
    assert "unit_price" not in result.proposal.evidence


def test_missing_driver_value_is_preserved_as_unknown():
    text = source_text().replace("current_index:90", "current_index:UNKNOWN")
    result = asyncio.run(TemplateExtractor().extract(read_document(text.encode(), "fixture.txt")))
    assert result.drivers[1].current_index is None
    assert result.drivers[1].evidence is not None


@pytest.mark.parametrize(
    "extra",
    [
        "\nproposal.unit_price = 112",  # Duplicates are rejected even when identical.
        "\nproposal.unit_price = 115",
        "\nas_of = 2026-11-10",
    ],
)
def test_duplicate_facts_are_not_silently_overwritten(extra):
    error = failure_from_text(source_text() + extra)
    assert error.code == "duplicate_fact"


def test_duplicate_driver_fields_are_rejected():
    text = source_text().replace("share:0.5;", "share:0.5; share:0.4;")
    assert failure_from_text(text).code == "duplicate_fact"


@pytest.mark.parametrize(
    "bad_line",
    [
        "unexpected = ignored value",
        "proposal.discount = 20",
    ],
)
def test_unknown_labels_are_not_accepted(bad_line):
    assert failure_from_text(source_text() + "\n" + bad_line).code == "unknown_field"


@pytest.mark.parametrize(
    "replacement",
    [
        "driver.energy = UNKNOWN",
        "driver.energy = no-colon-here",
        "driver.energy = share:0.2; broken-token; current_index:90",
    ],
)
def test_malformed_driver_record_returns_controlled_failure(replacement):
    old = next(line for line in source_text().splitlines() if line.startswith("driver.energy"))
    failure_from_text(source_text().replace(old, replacement))


@pytest.mark.parametrize(
    "old,new",
    [
        ("as_of = 2026-11-09", "as_of = invalid-date"),
        ("proposal.unit_price = 112", "proposal.unit_price = not-a-number"),
        ("proposal.unit_price = 112", "proposal.unit_price = -1"),
        ("current_index:90", "current_index:NaN"),
    ],
)
def test_invalid_extracted_values_return_controlled_failure(old, new):
    assert failure_from_text(source_text().replace(old, new)).code == "invalid_extraction"


def test_missing_required_review_date_returns_controlled_failure():
    assert failure_from_text(source_text().replace("as_of = 2026-11-09\n", "")).code == "invalid_extraction"


def test_oversized_evidence_line_returns_controlled_failure():
    failure_from_text(
        source_text().replace(
            "baseline.supplier = Fictional Components Ltd",
            "baseline.supplier = " + "x" * 1100,
        )
    )


@pytest.mark.parametrize(
    "content,name,code,status",
    [
        (b"", "empty.txt", "empty_document", 422),
        (b" \n\t", "empty.txt", "no_text", 422),
        (b"\xff\xfe", "bad.txt", "invalid_text", 422),
        (b"fake pdf", "fake.pdf", "invalid_pdf", 422),
        (b"%PDF-not-an-actual-pdf", "fake.pdf", "invalid_pdf", 422),
        (b"text", "source.docx", "unsupported_document", 415),
        (b"x" * (MAX_FILE_BYTES + 1), "large.txt", "document_too_large", 413),
        (b"x" * (MAX_TEXT_CHARS + 1), "long.txt", "document_text_limit", 422),
        (
            b"\f".join([b"page"] * (MAX_PAGES + 1)),
            "pages.txt",
            "document_text_limit",
            422,
        ),
    ],
)
def test_document_boundary_failures(content, name, code, status):
    with pytest.raises(ExtractionFailure) as error:
        read_document(content, name)
    assert (error.value.code, error.value.status) == (code, status)


def test_pdf_text_is_extracted_and_hash_uses_original_bytes():
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(40, 800, "Fictional supplier quotation")
    pdf.showPage()
    pdf.drawString(40, 800, "proposal.unit_price = 112")
    pdf.save()
    content = output.getvalue()
    doc = read_document(content, "fictional.pdf")
    assert len(doc.pages) == 2
    assert "proposal.unit_price = 112" in doc.pages[1]
    assert doc.sha256 == hashlib.sha256(content).hexdigest()


def test_blank_pdf_does_not_claim_successful_extraction():
    writer, output = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=200, height=200)
    writer.write(output)
    with pytest.raises(ExtractionFailure) as error:
        read_document(output.getvalue(), "blank.pdf")
    assert error.value.code == "no_text"


def test_encrypted_pdf_has_a_controlled_failure():
    writer, output = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("test-only-password")
    writer.write(output)
    with pytest.raises(ExtractionFailure) as error:
        read_document(output.getvalue(), "locked.pdf")
    assert error.value.code == "encrypted_pdf"


def test_page_limit_applies_to_pdfs():
    writer, output = PdfWriter(), io.BytesIO()
    for _ in range(MAX_PAGES + 1):
        writer.add_blank_page(width=200, height=200)
    writer.write(output)
    with pytest.raises(ExtractionFailure) as error:
        read_document(output.getvalue(), "many-pages.pdf")
    assert error.value.code == "too_many_pages"


@pytest.mark.parametrize(
    "field,value",
    [
        ("quote", "A quotation that is not on this page"),
        ("sha256", "0" * 64),
        ("document", "different-document.txt"),
        ("page", 10),
        ("page", 1),  # Existing page, but the proposal quote belongs on page 2.
    ],
)
def test_fabricated_or_misdirected_evidence_is_rejected(document, extracted, field, value):
    setattr(extracted.proposal.evidence["unit_price"], field, value)
    with pytest.raises(ExtractionFailure) as error:
        validate_evidence(extracted, document)
    assert error.value.code == "invalid_evidence"


@pytest.mark.parametrize("target", ["quote", "driver", "as_of", "annual_volume", "assumption"])
def test_every_populated_extracted_fact_requires_evidence(document, extracted, target):
    if target == "quote":
        del extracted.proposal.evidence["unit_price"]
    elif target == "driver":
        extracted.drivers[0].evidence = None
    else:
        del extracted.metadata_evidence["assumptions.0" if target == "assumption" else target]
    with pytest.raises(ExtractionFailure) as error:
        validate_evidence(extracted, document)
    assert error.value.code == "missing_evidence"


async def run_provider(document, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleExtractor(
            client,
            "https://approved-gateway.example/v1/",
            "test-model",
            "test-key",
            timeout=3,
        )
        return await adapter.extract(document)


def test_model_adapter_sends_bounded_json_and_returns_validated_facts(document, extracted):
    def handler(request):
        assert str(request.url) == "https://approved-gateway.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.extensions["timeout"]["read"] == 3
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["max_tokens"] == 6000
        assert payload["response_format"] == {"type": "json_object"}
        assert "untrusted data" in payload["messages"][0]["content"]
        supplied = json.loads(payload["messages"][1]["content"])
        assert supplied["sha256"] == document.sha256
        assert len(supplied["pages"]) == 3
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": extracted.model_dump_json()}}]},
        )

    result = asyncio.run(run_provider(document, handler))
    assert result.extraction_mode == "openai_compatible"
    assert result.proposal.unit_price == Decimal("112")
    validate_evidence(result, document)


def test_provider_timeout_returns_controlled_504(document):
    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, handler))
    assert (error.value.code, error.value.status) == ("provider_timeout", 504)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_provider_http_failures_do_not_leak_provider_body(document, status):
    def handler(request):
        return httpx.Response(status, text="Sensitive provider diagnostic")

    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, handler))
    assert (error.value.code, error.value.status) == ("provider_unavailable", 502)
    assert "Sensitive" not in str(error.value)


def test_provider_connection_error_returns_controlled_502(document):
    def handler(request):
        raise httpx.ConnectError("Sensitive infrastructure detail", request=request)

    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, handler))
    assert error.value.status == 502
    assert "Sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": "not-json"}}]},
        {"choices": [{"message": {"content": "{}"}}]},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_provider_malformed_output_returns_controlled_502(document, body):
    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, lambda request: httpx.Response(200, json=body)))
    assert (error.value.code, error.value.status) == ("provider_invalid_output", 502)


def test_provider_invalid_response_json_returns_controlled_502(document):
    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, lambda request: httpx.Response(200, text="not json")))
    assert (error.value.code, error.value.status) == ("provider_invalid_output", 502)


def test_provider_response_size_limit(document):
    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, lambda request: httpx.Response(200, content=b"x" * 250_001)))
    assert (error.value.code, error.value.status) == ("provider_response_limit", 502)


def test_provider_cannot_supply_fabricated_quotation(document, extracted):
    extracted.proposal.evidence["unit_price"].quote = "proposal.unit_price = 999"
    body = {"choices": [{"message": {"content": extracted.model_dump_json()}}]}
    with pytest.raises(ExtractionFailure) as error:
        asyncio.run(run_provider(document, lambda request: httpx.Response(200, json=body)))
    assert (error.value.code, error.value.status) == ("invalid_evidence", 422)

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sourcing_api.domain import review
from sourcing_api.examples import document_text
from sourcing_api.extraction import ExtractionFailure, TemplateExtractor, read_document, validate_evidence
from sourcing_api.main import Settings, create_app
from sourcing_api.models import Evidence


def sample():
    document = read_document(document_text("price_increase").encode(), "source.txt")
    return asyncio.run(TemplateExtractor().extract(document)), document


def test_extra_emitted_citation_cannot_escape_validation():
    data, document = sample()
    extra = data.baseline.evidence["unit_price"].model_copy(
        update={"quote": "Invented evidence not in the source"}
    )
    data.metadata_evidence["extra"] = extra
    with pytest.raises(ExtractionFailure, match="not found"):
        validate_evidence(data, document)


def test_whitespace_evidence_is_not_a_citation():
    with pytest.raises(ValidationError):
        Evidence(document="document.txt", page=1, quote="   ", sha256="a" * 64)


def test_price_and_index_baseline_period_must_align():
    data, _ = sample()
    data.baseline.quote_date = data.baseline.quote_date.replace(month=2)
    data.baseline.valid_until = data.baseline.quote_date.replace(month=3)
    result = review(data)
    assert result.status == "needs_information"
    assert result.calculation is None
    assert any(i.code == "misaligned_price_basis" for i in result.issues)


def test_unexpected_failure_is_sanitised_and_correlated():
    class Broken:
        mode = "template"

        async def extract(self, document):
            raise RuntimeError("CONFIDENTIAL_PAYLOAD")

    with TestClient(create_app(Settings(), extractor=Broken())) as client:
        response = client.post(
            "/v1/reviews/document", files={"file": ("case.txt", document_text("price_increase").encode())}
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
        assert "CONFIDENTIAL_PAYLOAD" not in response.text
        assert client.get("/unknown").json()["error"]["code"] == "http_error"

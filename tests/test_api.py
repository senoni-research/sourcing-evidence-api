import asyncio

import pytest
from fastapi.testclient import TestClient

from sourcing_api.examples import document_pdf, document_text
from sourcing_api.extraction import ExtractionFailure
from sourcing_api.main import Settings, create_app


@pytest.fixture
def client():
    with TestClient(create_app(Settings())) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "case,status",
    [
        ("price_increase", "scenario_available"),
        ("missing_evidence", "needs_information"),
        ("scope_mismatch", "not_comparable"),
    ],
)
def test_json_and_document_agree(client, case, status):
    request = client.get(f"/v1/examples/{case}").json()
    direct = client.post("/v1/reviews", json=request)
    assert direct.status_code == 200
    assert direct.json()["status"] == status
    assert direct.json()["evidence_validation"] == "not_verified_structured_input"
    assert direct.json()["extraction_mode"] == "structured_input"
    pdf = client.get(f"/v1/examples/{case}/document")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    response = client.post(
        "/v1/reviews/document", files={"file": (case + ".pdf", pdf.content, "application/pdf")}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == status
    assert result["evidence_validation"] == "quotations_matched_to_source_text"
    assert result["calculation"] == direct.json()["calculation"]
    assert result["request_id"] == response.headers["X-Request-ID"]
    assert len(result["request_id"]) == 32


def test_extraction_round_trip(client):
    content = document_text("price_increase").encode()
    extracted = client.post("/v1/evidence/extract", files={"file": ("case.txt", content)})
    assert extracted.status_code == 200
    data = extracted.json()
    assert len(data["document_sha256"]) == 64
    replay = client.post("/v1/reviews", json=data["request"])
    assert replay.json()["calculation"]["scenario_price"] == "102.30"


def test_readiness_is_not_provider_connectivity_claim(client):
    assert client.get("/health/live").json()["status"] == "alive"
    ready = client.get("/health/ready").json()
    assert ready["provider_connectivity_checked"] is False


def test_api_key_and_protected_metrics():
    with TestClient(create_app(Settings(service_api_key="a" * 32))) as client:
        for url in ("/v1/examples", "/metrics"):
            assert client.get(url).status_code == 401
            assert client.get(url, headers={"X-API-Key": "wrong"}).status_code == 401
            assert client.get(url, headers={"X-API-Key": "a" * 32}).status_code == 200
        assert client.get("/health/live").status_code == 200


def test_validation_does_not_echo_raw_input(client):
    marker = "PRIVATE DOCUMENT SENTINEL"
    response = client.post("/v1/reviews", json={"as_of": marker})
    assert response.status_code == 422
    assert marker not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_request_body_limit(client):
    response = client.post(
        "/v1/reviews", content=b"x" * 1_200_001, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_upload_limit_and_type(client):
    assert (
        client.post("/v1/reviews/document", files={"file": ("case.txt", b"x" * 1_000_001)}).status_code == 413
    )
    assert client.post("/v1/reviews/document", files={"file": ("case.exe", b"payload")}).status_code == 415


def test_malformed_document_controlled(client):
    content = document_text("price_increase").replace("share:0.50", "broken")
    result = client.post("/v1/reviews/document", files={"file": ("case.txt", content.encode())})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "invalid_driver"


def test_provider_error_maps_to_http():
    class Failed:
        mode = "openai_compatible"

        async def extract(self, document):
            raise ExtractionFailure("provider_timeout", "Provider timeout", 504)

    with TestClient(create_app(Settings(), extractor=Failed())) as client:
        response = client.post(
            "/v1/reviews/document", files={"file": ("case.pdf", document_pdf("price_increase"))}
        )
        assert response.status_code == 504
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_total_provider_deadline():
    class Slow:
        mode = "openai_compatible"

        async def extract(self, document):
            await asyncio.sleep(2)

    with TestClient(create_app(Settings(model_timeout_seconds=0.01), extractor=Slow())) as client:
        response = client.post(
            "/v1/reviews/document", files={"file": ("case.txt", document_text("price_increase").encode())}
        )
        assert response.status_code == 504


def test_metrics_count_business_outcomes(client):
    for case in ("price_increase", "missing_evidence"):
        client.post("/v1/reviews", json=client.get("/v1/examples/" + case).json())
    metrics = client.get("/metrics").text
    assert "sourcing_scenarios_total 1" in metrics
    assert "sourcing_withheld_total 1" in metrics
    assert "Fictional Northbridge" not in metrics


def test_openapi_and_ui(client):
    schema = client.get("/openapi.json").json()
    assert "/v1/reviews/document" in schema["paths"]
    request_ref = schema["paths"]["/v1/reviews"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"].split("/")[-1]
    assert "baseline" in schema["components"]["schemas"][request_ref]["properties"]
    assert client.get("/").status_code == 200
    assert client.get("/v1/examples/no-such-case").status_code == 404


@pytest.mark.parametrize(
    "settings",
    [
        Settings(app_env="production"),
        Settings(extractor_mode="other"),
        Settings(model_timeout_seconds=0),
        Settings(
            extractor_mode="openai_compatible",
            model_base_url="http://external.example/v1",
            model_name="test",
            model_api_key="test",
        ),
    ],
)
def test_unsafe_configuration_rejected(settings):
    with pytest.raises(ValueError):
        create_app(settings)

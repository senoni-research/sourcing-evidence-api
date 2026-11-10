"""Exercise all three scenarios against the real ASGI app or a running HTTP server."""

import argparse
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sourcing_api.main import Settings, create_app


def run(client, output: Path | None):
    records = []
    expected = {
        "price_increase": "scenario_available",
        "missing_evidence": "needs_information",
        "scope_mismatch": "not_comparable",
    }
    for case, status in expected.items():
        source = client.get(f"/v1/examples/{case}/document")
        source.raise_for_status()
        result = client.post(
            "/v1/reviews/document", files={"file": (case + ".pdf", source.content, "application/pdf")}
        )
        result.raise_for_status()
        body = result.json()
        assert body["status"] == status
        assert (body["calculation"] is None) == (status != "scenario_available")
        if body["calculation"]:
            assert body["calculation"]["scenario_price"] == "102.30"
            assert body["calculation"]["annualised_gap"] == "97000.00"
        records.append({"case": case, "http_status": result.status_code, "response": body})
        print(f"{case}: HTTP {result.status_code} / {status} / {len(body['evidence'])} evidence excerpts")
        if output:
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{case}.pdf").write_bytes(source.content)
            (output / f"{case}.response.json").write_text(json.dumps(body, indent=2) + "\n")
            request = client.get(f"/v1/examples/{case}").json()
            (output / f"{case}.request.json").write_text(json.dumps(request, indent=2) + "\n")
    if output:
        (output / "recorded_runs.json").write_text(json.dumps(records, indent=2) + "\n")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--in-process", action="store_true")
    group.add_argument("--base-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    if args.in_process:
        with TestClient(create_app(Settings()), headers=headers) as client:
            run(client, args.output)
    else:
        with httpx.Client(base_url=args.base_url, headers=headers, trust_env=False, timeout=30) as client:
            run(client, args.output)

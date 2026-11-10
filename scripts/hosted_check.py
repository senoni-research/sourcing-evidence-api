"""Check a deployed demonstration over HTTP, including recovery across a service restart.

    python scripts/hosted_check.py --base-url https://... --save .demo-data/hosted.json
    # deploy a new revision (or otherwise replace every running instance), then:
    python scripts/hosted_check.py --base-url https://... --resume .demo-data/hosted.json

The first run creates a fictional review, corrects it, calculates a what-if, shows that a stale
writer is refused, and saves the owner cookie with the state it read. The second run sends that
cookie to the restarted service and requires the identical state. The saved file holds a demo
session token for fictional data only; it is git-ignored.
"""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

COOKIE = "sourcing_demo_owner"
CORRECTION = {
    "field": "proposal.revision",
    "value": "A",
    "basis": "cited_passage",
    "citation": {"page": 4, "line": 9},
    "reason": "Supplier clarification on page 4 confirms the proposal applies to revision A.",
}
WHAT_IF = {"materials": 10, "energy": -20, "labour": 0, "freight": 5}


def saved(view):
    return {key: value for key, value in view.items() if key != "outcome"}


def post(client, path, body=None, status=200):
    response = client.post(path, json=body)
    assert response.status_code == status, (path, response.status_code, response.text[:300])
    return response


def first_run(client, target):
    opened = post(client, "/demo/scope_mismatch/sessions")
    cookie = opened.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie, "cookie flags"
    view = opened.json()
    assert view["status"] == "not_comparable" and view["storage"]["backend"] == "firestore", view["storage"]
    view = post(
        client, "/demo/scope_mismatch/correction", {"expected_version": view["version"], **CORRECTION}
    ).json()
    assert view["reviewed"]["calculation"]["scenario_price"] == "102.30"
    stale_version = view["version"]
    view = post(
        client,
        "/demo/scope_mismatch/sensitivity",
        {"expected_version": stale_version, "changes_pct": WHAT_IF},
    ).json()
    assert view["what_if"]["result"]["calculation"]["scenario_price"] == "101.50"
    thresholds = {item["basis"]: item["rows"][0]["required_movement_pct"] for item in view["thresholds"]}
    assert thresholds == {"reviewed": "25.4000", "what_if": "31.0000"}, thresholds

    stale = post(
        client,
        "/demo/scope_mismatch/sensitivity",
        {"expected_version": stale_version, "changes_pct": {**WHAT_IF, "materials": 1}},
        status=409,
    )
    assert stale.json()["error"]["code"] == "stale_review"

    # Switching cases keeps the first review.
    post(client, "/demo/missing_evidence/sessions")
    state = client.get("/demo/scope_mismatch/session").json()
    assert saved(state) == saved(view), "the stale writer or the case switch changed the saved review"
    assert all(state["integrity"].values()), state["integrity"]

    pdf = client.get(state["source"]["download_path"]).content
    assert hashlib.sha256(pdf).hexdigest() == state["source"]["sha256"], "evidence hash is the PDF's hash"

    docs = client.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text, "interactive API docs"
    for path in ("/redoc", "/metrics", "/v1/examples", "/v1/examples/price_increase/document"):
        assert client.get(path).status_code == 404, path
    assert post(client, "/v1/reviews", {}, status=404)
    schema = client.get("/openapi.json").json()
    assert all(path.startswith("/demo/") for path in schema["paths"]), list(schema["paths"])

    target.write_text(json.dumps({"cookie": client.cookies[COOKIE], "state": saved(state)}))
    print(f"first run passed: version {state['version']}, {len(state['events'])} history entries saved")
    print(f"last request id: {opened.headers['x-request-id']}")


def resume(client, source):
    record = json.loads(source.read_text())
    client.cookies.set(COOKIE, record["cookie"])
    response = client.get("/demo/scope_mismatch/session")
    assert response.status_code == 200, response.text[:300]
    state = response.json()
    assert saved(state) == record["state"], "state after the restart differs from the state saved before it"
    listed = {item["case"]: item["review"] for item in client.get("/demo/reviews").json()["cases"]}
    assert listed["scope_mismatch"]["has_override"] and listed["missing_evidence"]["events"] == 1, listed
    print(
        f"resume passed: identical state, version {state['version']}, {len(state['events'])} history entries"
    )
    print(f"resume request id: {response.headers['x-request-id']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--save", type=Path)
    group.add_argument("--resume", type=Path)
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        if args.save:
            first_run(client, args.save)
        else:
            resume(client, args.resume)


if __name__ == "__main__":
    main()

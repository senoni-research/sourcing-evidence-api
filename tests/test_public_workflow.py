"""Regressions for the hosted evidence-review workflow.

Each block corresponds to a finding or acceptance check in the 10 November 2026 critical review.
"""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sourcing_api.main import OWNER_COOKIE, Settings, create_app
from sourcing_api.workflow import (
    MAX_EVENTS,
    FirestoreWorkflowStore,
    MemoryWorkflowStore,
    ReviewNotFound,
    WorkflowConflict,
    WorkflowService,
    owner_hash,
)

PUBLIC = dict(app_env="production", public_demo=True, workflow_backend="firestore")
REASON = "Supplier clarification on page 4 confirms the proposal applies to revision A."
WHAT_IF = {"materials": 10, "energy": -20, "labour": 0, "freight": 5}


def make_client(store, **overrides):
    return TestClient(
        create_app(Settings(**{**PUBLIC, **overrides}), workflow_store=store), base_url="https://testserver"
    )


@pytest.fixture
def store():
    return MemoryWorkflowStore()


@pytest.fixture
def client(store):
    with make_client(store) as test_client:
        yield test_client


def open_case(client, case):
    response = client.post(f"/demo/{case}/sessions")
    assert response.status_code == 200, response.text
    return response.json()


def revision_a(version, **changes):
    return {
        "expected_version": version,
        "field": "proposal.revision",
        "value": "A",
        "basis": "cited_passage",
        "citation": {"page": 4, "line": 9},
        "reason": REASON,
        **changes,
    }


def energy_90(version, **changes):
    return {
        "expected_version": version,
        "field": "drivers.energy.current_index",
        "value": "90",
        "basis": "cited_passage",
        "citation": {"page": 4, "line": 7},
        "reason": "The fictional index bulletin on page 4 states the October value.",
        **changes,
    }


def correct(client, case, body, status=200):
    response = client.post(f"/demo/{case}/correction", json=body)
    assert response.status_code == status, response.text
    return response.json()


def what_if(client, case, version, changes=WHAT_IF, status=200):
    response = client.post(
        f"/demo/{case}/sensitivity", json={"expected_version": version, "changes_pct": changes}
    )
    assert response.status_code == status, response.text
    return response.json()


def saved(view):
    """A view without the per-response outcome, for comparing what is stored."""
    return {key: value for key, value in view.items() if key != "outcome"}


# ----------------------------------------------------------------- restricted public surface


def test_public_demo_is_deliberately_restricted(client):
    docs = client.get("/docs")
    assert docs.status_code == 200 and "text/html" in docs.headers["content-type"]
    assert "swagger-ui" in docs.text and "'/openapi.json'" in docs.text
    assert "Sourcing Evidence API · demo routes" in docs.text
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/demo/reviews",
        "/demo/{case}/sessions",
        "/demo/{case}/session",
        "/demo/{case}/correction",
        "/demo/{case}/correction/revert",
        "/demo/{case}/sensitivity",
        "/demo/{case}/sensitivity/discard",
        "/demo/{case}/restart",
        "/demo/{case}/document",
    }
    for method, path in [
        ("post", "/v1/reviews"),
        ("post", "/v1/reviews/document"),
        ("post", "/v1/evidence/extract"),
        ("get", "/v1/examples"),
        ("get", "/v1/examples/price_increase"),
        ("get", "/v1/examples/price_increase/document"),
        ("get", "/metrics"),
        ("get", "/redoc"),
        ("get", "/docs/oauth2-redirect"),
    ]:
        assert getattr(client, method)(path).status_code == 404, path
    assert client.get("/health/ready").json() == {
        "status": "ready",
        "version": "0.1.0",
        "extractor_mode": "template",
        "provider_connectivity_checked": False,
        "public_demo": True,
        "workflow_persistence": "memory",
    }
    page = client.get("/").text
    assert "noindex,nofollow" in page
    assert "MODEL_API_KEY" not in page


def test_public_schema_lists_no_v1_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert paths and not [path for path in paths if path.startswith("/v1")]


def test_published_schema_matches_the_public_app(client):
    """Regenerate docs/openapi.json with: UPDATE_PUBLISHED_SCHEMA=1 pytest -k published_schema"""
    published = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    if os.environ.get("UPDATE_PUBLISHED_SCHEMA"):
        published.write_text(json.dumps(client.app.openapi(), indent=2, ensure_ascii=False) + "\n")
    assert json.loads(published.read_text()) == client.app.openapi()


def test_public_demo_requires_durability_and_no_model():
    for settings in [
        Settings(public_demo=True, workflow_backend="firestore"),
        Settings(app_env="production", public_demo=True),
        Settings(app_env="production", public_demo=True, workflow_backend="local_test_store"),
        Settings(app_env="production", public_demo=True, extractor_mode="openai_compatible"),
    ]:
        with pytest.raises(ValueError):
            create_app(settings)


def test_demo_routes_are_absent_from_a_keyed_production_api():
    settings = Settings(app_env="production", service_api_key="k" * 32)
    with TestClient(create_app(settings), base_url="https://testserver") as keyed:
        assert keyed.post("/demo/price_increase/sessions").status_code == 404
        assert keyed.get("/demo/reviews").status_code == 404


def test_owner_cookie_is_scoped_http_only_and_same_site(client):
    response = client.post("/demo/price_increase/sessions")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(OWNER_COOKIE + "=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/demo" in cookie
    local = make_client(MemoryWorkflowStore(), workflow_backend="local_test_store", local_insecure_demo=True)
    assert "Secure" not in local.post("/demo/price_increase/sessions").headers["set-cookie"]


def test_rate_limit_returns_the_typed_error(store):
    with make_client(store, public_demo_rate_limit=3) as limited:
        assert [limited.get("/health/live").status_code for _ in range(4)] == [200, 200, 200, 429]
        body = limited.get("/health/live").json()
        assert body["error"]["code"] == "demo_rate_limit" and body["request_id"]


def test_downloaded_source_pack_is_the_document_the_evidence_cites(client):
    view = open_case(client, "scope_mismatch")
    pdf = client.get(view["source"]["download_path"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert hashlib.sha256(pdf.content).hexdigest() == view["source"]["sha256"]
    assert client.get("/demo/unknown/document").status_code == 404


# ----------------------------------------------------------------- P1: case switching


def test_case_switching_preserves_each_review_and_its_history(client):
    conflict = open_case(client, "scope_mismatch")
    owner = client.cookies[OWNER_COOKIE]
    corrected = correct(client, "scope_mismatch", revision_a(conflict["version"]))
    assert corrected["status"] == "scenario_available"

    missing = open_case(client, "missing_evidence")
    assert client.cookies[OWNER_COOKIE] == owner, "one stable owner identity for every case"
    fixed = correct(client, "missing_evidence", energy_90(missing["version"]))
    open_case(client, "price_increase")

    resumed = client.get("/demo/scope_mismatch/session")
    assert resumed.status_code == 200
    assert saved(resumed.json()) == saved(corrected)
    reopened = open_case(client, "scope_mismatch")
    assert reopened["outcome"] == "resumed"
    assert [event["action"] for event in reopened["events"]] == ["review_opened", "correction_applied"]
    assert saved(open_case(client, "missing_evidence")) == saved(fixed)

    summary = {item["case"]: item["review"] for item in client.get("/demo/reviews").json()["cases"]}
    assert summary["scope_mismatch"] == {
        "version": 2,
        "status": "scenario_available",
        "has_override": True,
        "has_what_if": False,
        "events": 2,
    }
    assert summary["missing_evidence"]["has_override"] and summary["price_increase"]["events"] == 1


def test_reviews_belong_to_their_owner(client, store):
    open_case(client, "scope_mismatch")
    stranger = make_client(store)
    assert stranger.get("/demo/scope_mismatch/session").status_code == 404
    assert all(item["review"] is None for item in stranger.get("/demo/reviews").json()["cases"])
    stranger.cookies.set(OWNER_COOKIE, "not-a-token", path="/demo")
    assert stranger.get("/demo/scope_mismatch/session").json()["error"]["code"] == "review_not_found"
    assert stranger.post("/demo/unknown/sessions").status_code == 404


# ----------------------------------------------------------------- P1: stale writes


def test_a_stale_writer_gets_409_and_cannot_erase_history(client, store):
    first = open_case(client, "scope_mismatch")
    second_tab = make_client(store)
    second_tab.cookies.set(OWNER_COOKIE, client.cookies[OWNER_COOKIE], path="/demo")
    assert second_tab.get("/demo/scope_mismatch/session").json()["version"] == first["version"]

    winner = correct(client, "scope_mismatch", revision_a(first["version"]))
    stale = second_tab.post(
        "/demo/scope_mismatch/correction",
        json=revision_a(first["version"], basis="reviewer_assumption", citation=None, reason="A stale tab."),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_review"
    for path, body in [
        ("/demo/scope_mismatch/sensitivity", {"expected_version": 1, "changes_pct": WHAT_IF}),
        ("/demo/scope_mismatch/correction/revert", {"expected_version": 1, "reason": "A stale revert."}),
        ("/demo/scope_mismatch/sensitivity/discard", {"expected_version": 1}),
        ("/demo/scope_mismatch/restart", {"expected_version": 1}),
    ]:
        assert second_tab.post(path, json=body).status_code == 409, path

    # Recovery path: reload the latest version, then repeat the change against it.
    latest = second_tab.get("/demo/scope_mismatch/session").json()
    assert saved(latest) == saved(winner)
    assert what_if(second_tab, "scope_mismatch", latest["version"])["version"] == latest["version"] + 1


def test_store_compares_versions_atomically_under_concurrent_writers(store):
    store.create("scope_mismatch", "owner", {"writer": "initial"}, 1)
    outcomes = []
    barrier = threading.Barrier(8)

    def write(number):
        barrier.wait()
        try:
            store.replace("scope_mismatch", "owner", {"writer": number}, 2, 1)
            outcomes.append(number)
        except WorkflowConflict:
            outcomes.append(None)

    threads = [threading.Thread(target=write, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [number for number in outcomes if number is not None]
    assert len(winners) == 1 and len(outcomes) == 8
    assert store.get("scope_mismatch", "owner").payload == {"writer": winners[0]}


def test_service_keeps_no_revision_between_requests(store):
    service = WorkflowService(store)
    assert not hasattr(service, "revision")


def test_service_race_between_read_and_write_is_rejected_by_the_store(client, store):
    """The service read version 1; another writer commits before its write reaches the store."""
    view = open_case(client, "scope_mismatch")
    original_replace = store.replace

    def interleaved(case, owner, payload, version, expected_version):
        store.replace = original_replace
        other = make_client(store)
        other.cookies.set(OWNER_COOKIE, client.cookies[OWNER_COOKIE], path="/demo")
        correct(
            other, "scope_mismatch", revision_a(view["version"], reason="The other writer got there first.")
        )
        return original_replace(case, owner, payload, version, expected_version)

    store.replace = interleaved
    response = client.post("/demo/scope_mismatch/correction", json=revision_a(view["version"]))
    assert response.status_code == 409
    current = client.get("/demo/scope_mismatch/session").json()
    assert current["version"] == 2
    assert current["correction"]["reason"] == "The other writer got there first."


class FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeReference:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return FakeSnapshot(self.data)


class FakeTransaction:
    def __init__(self):
        self.writes = []

    def set(self, reference, document):
        self.writes.append(document)


def firestore_adapter():
    class Client:
        def collection(self, name):
            return name

    return FirestoreWorkflowStore(client=Client())


def stored_document(version, expires_in=timedelta(days=1)):
    return {
        "case": "scope_mismatch",
        "owner_hash": "owner",
        "version": version,
        "state": {},
        "expires_at": datetime.now(UTC) + expires_in,
    }


def test_firestore_adapter_compares_the_version_it_reads_in_the_transaction():
    adapter = firestore_adapter()
    new = stored_document(3)

    transaction = FakeTransaction()
    with pytest.raises(WorkflowConflict):  # the snapshot moved on to version 3; the caller saw 2
        adapter.replace_in_transaction(
            transaction, FakeReference(stored_document(3)), "scope_mismatch", "owner", new, 2
        )
    assert transaction.writes == [], "a stale writer must write nothing"

    adapter.replace_in_transaction(
        transaction, FakeReference(stored_document(2)), "scope_mismatch", "owner", new, 2
    )
    assert transaction.writes == [new]

    for missing in (
        None,
        stored_document(2, timedelta(seconds=-1)),
        {**stored_document(2), "owner_hash": "x"},
    ):
        with pytest.raises(ReviewNotFound):
            adapter.replace_in_transaction(
                FakeTransaction(), FakeReference(missing), "scope_mismatch", "owner", new, 2
            )


# ----------------------------------------------------------------- P1: event bounds


def fill_history(client, case="price_increase"):
    view = open_case(client, case)
    for number in range(MAX_EVENTS - 1):
        view = what_if(client, case, view["version"], {**WHAT_IF, "materials": number})
    assert view["history"] == {"limit": MAX_EVENTS, "used": MAX_EVENTS, "remaining": 0}
    return view


def test_every_mutation_respects_the_history_limit_and_the_record_still_loads(client):
    full = fill_history(client)
    version = full["version"]
    attempts = [
        ("/demo/price_increase/sensitivity", {"expected_version": version, "changes_pct": WHAT_IF}),
        ("/demo/price_increase/sensitivity/discard", {"expected_version": version}),
    ]
    for path, body in attempts:
        response = client.post(path, json=body)
        assert response.status_code == 409, path
        assert response.json()["error"]["code"] == "history_full"
    reloaded = client.get("/demo/price_increase/session")
    assert reloaded.status_code == 200
    assert saved(reloaded.json()) == saved(full)

    restarted = client.post("/demo/price_increase/restart", json={"expected_version": version})
    assert restarted.status_code == 200
    view = restarted.json()
    assert view["outcome"] == "restarted" and view["generation"] == 2 and view["version"] == version + 1
    assert view["what_if"] is None and len(view["events"]) == 1
    assert view["events"][0]["action"] == "review_restarted"
    assert view["events"][0]["discarded_events"] == MAX_EVENTS
    assert what_if(client, "price_increase", view["version"])["history"]["used"] == 2


def test_corrections_and_reverts_respect_the_history_limit(client):
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    while view["history"]["remaining"]:
        view = what_if(client, "scope_mismatch", view["version"], {**WHAT_IF, "labour": view["version"]})
    for path, body in [
        ("/demo/scope_mismatch/correction", revision_a(view["version"], reason="A different justification.")),
        (
            "/demo/scope_mismatch/correction/revert",
            {"expected_version": view["version"], "reason": "Reverting."},
        ),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "history_full", path
    assert client.get("/demo/scope_mismatch/session").json()["correction"]["value"] == "A"


# ----------------------------------------------------------------- P2: thresholds follow active inputs


def test_break_even_thresholds_match_the_inputs_they_are_shown_beside(client):
    view = what_if(client, "price_increase", open_case(client, "price_increase")["version"])
    assert view["reviewed"]["calculation"]["scenario_price"] == "102.30"
    assert view["what_if"]["result"]["calculation"]["scenario_price"] == "101.50"
    assert view["what_if"]["result"]["calculation"]["model_change_pct"] == "1.5000"
    by_basis = {
        item["basis"]: {row["driver"]: float(row["required_movement_pct"]) for row in item["rows"]}
        for item in view["thresholds"]
    }
    # Holding energy -20%, labour 0% and freight +5%: 0.5*m - 4 + 0.5 = 12, so m = 31%.
    assert by_basis["what_if"] == {"materials": 31.0, "energy": 32.5, "labour": 52.5, "freight": 110.0}
    assert by_basis["reviewed"] == {"materials": 25.4, "energy": 38.5, "labour": 52.5, "freight": 102.0}
    held = {
        item["basis"]: {row["driver"]: float(row["held_movement_pct"]) for row in item["rows"]}
        for item in view["thresholds"]
    }
    assert held["what_if"] == {"materials": 10.0, "energy": -20.0, "labour": 0.0, "freight": 5.0}
    assert held["reviewed"] == {"materials": 6.0, "energy": -10.0, "labour": 4.0, "freight": 5.0}


def test_each_threshold_reaches_the_proposal_when_applied(client):
    view = what_if(client, "price_increase", open_case(client, "price_increase")["version"])
    rows = next(item for item in view["thresholds"] if item["basis"] == "what_if")["rows"]
    for row in rows:
        changes = {**WHAT_IF, row["driver"]: float(row["required_movement_pct"])}
        view = what_if(client, "price_increase", view["version"], changes)
        assert view["what_if"]["result"]["calculation"]["scenario_price"] == "112.00", row["driver"]


def test_what_if_is_refused_while_blocked_and_never_changes_the_reviewed_result(client):
    blocked = open_case(client, "scope_mismatch")
    assert blocked["what_if_available"] is False and blocked["thresholds"] == []
    response = client.post(
        "/demo/scope_mismatch/sensitivity",
        json={"expected_version": blocked["version"], "changes_pct": WHAT_IF},
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "review_blocked"

    start = open_case(client, "price_increase")
    after = what_if(client, "price_increase", start["version"])
    assert after["reviewed"] == start["reviewed"]
    assert after["what_if"]["result"]["analysis_fingerprint"] != after["reviewed"]["analysis_fingerprint"]
    for body in [{"materials": 10}, {**WHAT_IF, "energy": 201}, {**WHAT_IF, "labour": "x"}]:
        assert what_if(client, "price_increase", after["version"], body, status=422)["error"]["code"] == (
            "invalid_request"
        )


# ----------------------------------------------------------------- P2: typed responses


def test_public_routes_declare_typed_success_and_error_schemas(client):
    schema = client.get("/openapi.json").json()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            content = operation["responses"]["200"]["content"]
            if path.endswith("/document"):
                assert "application/pdf" in content
            else:
                success = content["application/json"]["schema"]
                expected = "CaseList" if path == "/demo/reviews" else "ReviewView"
                assert success == {"$ref": f"#/components/schemas/{expected}"}, (method, path)
            errors = [code for code in operation["responses"] if code != "200"]
            assert errors, path
            for code in errors:
                assert operation["responses"][code]["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/ErrorResponse"
                }, (path, code)
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    view = schema["components"]["schemas"]["ReviewView"]
    assert {"facts", "blocking", "correction", "reviewed", "what_if", "thresholds", "events"} <= set(
        view["required"]
    )


def test_invalid_persisted_state_is_refused_not_served(client, store):
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    key = ("scope_mismatch", owner_hash(client.cookies[OWNER_COOKIE]))
    record = store._records[key]
    record.payload["events"][-1]["sequence"] = 7  # no longer a consecutive history
    for response in (
        client.get("/demo/scope_mismatch/session"),
        client.post("/demo/scope_mismatch/sessions"),
    ):
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "stored_review_invalid"
    assert (
        client.post(
            "/demo/scope_mismatch/sensitivity",
            json={"expected_version": view["version"], "changes_pct": WHAT_IF},
        ).status_code
        == 500
    )
    listed = {item["case"]: item["review"] for item in client.get("/demo/reviews").json()["cases"]}
    assert listed["scope_mismatch"] is None

    recovered = client.post("/demo/scope_mismatch/restart", json={})
    assert recovered.status_code == 200
    assert (
        recovered.json()["status"] == "not_comparable" and recovered.json()["version"] == view["version"] + 1
    )


def test_every_saved_state_is_validated_before_it_is_written(client, store):
    seen = []
    original = store.replace

    def recording(case, owner, payload, version, expected_version):
        seen.append(payload)
        return original(case, owner, payload, version, expected_version)

    store.replace = recording
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    what_if(client, "scope_mismatch", view["version"])
    from sourcing_api.workflow import StoredReview

    assert len(seen) == 2
    for payload in seen:
        StoredReview.model_validate(payload)


# ----------------------------------------------------------------- P2: evidence beside the issue


@pytest.mark.parametrize(
    "case, blocked",
    [
        ("scope_mismatch", {"baseline.revision": ("A", 1), "proposal.revision": ("B", 2)}),
        ("missing_evidence", {"drivers.energy.current_index": (None, 3)}),
    ],
)
def test_every_blocked_field_has_its_own_visible_source(client, case, blocked):
    view = open_case(client, case)
    assert view["reviewed"]["calculation"] is None
    facts = {fact["field"]: fact for fact in view["facts"]}
    referenced = {field for issue in view["blocking"] for field in issue["fields"]}
    assert referenced == set(blocked)
    for field, (value, page) in blocked.items():
        fact = facts[field]
        assert fact["blocking"] and fact["source_value"] == value and fact["source"]["page"] == page
        lines = view["source"]["pages"][page - 1]["lines"]
        assert lines[fact["source"]["line"] - 1] == fact["source"]["quote"]
        assert fact["source"]["sha256"] == view["source"]["sha256"]
    assert view["blocking"][0]["correctable_field"] == view["correctable_field"]


def test_missing_energy_passage_is_the_energy_line(client):
    view = open_case(client, "missing_evidence")
    fact = next(fact for fact in view["facts"] if fact["field"] == "drivers.energy.current_index")
    assert fact["source"]["quote"].startswith("driver.energy = ")
    assert "current_index:UNKNOWN" in fact["source"]["quote"]
    assert fact["origin"] == "not_stated" and fact["effective_value"] is None


def test_all_facts_point_at_real_lines_of_the_source_pack(client):
    for case in ("price_increase", "missing_evidence", "scope_mismatch"):
        view = open_case(client, case)
        assert len(view["facts"]) == 42
        for fact in view["facts"]:
            source = fact["source"]
            assert view["source"]["pages"][source["page"] - 1]["lines"][source["line"] - 1] == source["quote"]


def test_observed_prices_remain_available_while_the_calculation_is_withheld(client):
    header = open_case(client, "scope_mismatch")["header"]
    assert header["baseline_price"] == "100.00" and header["proposed_price"] == "112.00"
    assert header["proposed_change_pct"] == "12.00"
    assert header["baseline_revision"] == "A" and header["proposal_revision_source"] == "B"


# ----------------------------------------------------------------- deliberate, reversible corrections


def test_a_correction_needs_a_deliberate_value_reason_and_basis(client):
    version = open_case(client, "scope_mismatch")["version"]
    complete = revision_a(version)
    for missing in ("value", "reason", "basis", "field", "expected_version"):
        body = {key: value for key, value in complete.items() if key != missing}
        assert correct(client, "scope_mismatch", body, status=422)["error"]["code"] == "invalid_request", (
            missing
        )
    for body in [
        revision_a(version, reason="too short"),
        revision_a(version, citation=None),
        revision_a(version, basis="reviewer_assumption"),
        revision_a(version, value="not a revision"),
        revision_a(version, evidence={"invented": "ignored"}),
        energy_90(version, value="0"),
        energy_90(version, value="abc"),
    ]:
        assert correct(client, "scope_mismatch", body, status=422)["error"]["code"] == "invalid_request"
    rejected = {
        "matches_source": revision_a(version, value="B"),
        "citation_does_not_state_value": revision_a(version, citation={"page": 4, "line": 10}),
        "citation_not_found": revision_a(version, citation={"page": 9, "line": 1}),
        "field_not_correctable": energy_90(version),
    }
    for code, body in rejected.items():
        assert correct(client, "scope_mismatch", body, status=422)["error"]["code"] == code
    assert client.get("/demo/scope_mismatch/session").json()["version"] == version
    complete_case = open_case(client, "price_increase")
    assert complete_case["correctable_field"] is None
    body = revision_a(complete_case["version"])
    assert correct(client, "price_increase", body, status=422)["error"]["code"] == "field_not_correctable"


def test_source_fact_and_reviewer_assertion_stay_distinct_after_resolution(client):
    before = open_case(client, "scope_mismatch")
    view = correct(client, "scope_mismatch", revision_a(before["version"]))
    assert view["outcome"] == "recorded" and view["status"] == "scenario_available"
    assert view["reviewed"]["calculation"]["scenario_price"] == "102.30"
    fact = next(fact for fact in view["facts"] if fact["field"] == "proposal.revision")
    assert (fact["source_value"], fact["reviewer_value"], fact["effective_value"]) == ("B", "A", "A")
    assert fact["origin"] == "reviewer" and fact["source"]["quote"] == "proposal.revision = B"
    assert view["header"]["proposal_revision_source"] == "B"
    assert view["header"]["proposal_revision_effective"] == "A"
    assert view["blocking"] == []
    assert [issue["code"] for issue in view["resolved_by_override"]] == ["scope_mismatch"]
    correction = view["correction"]
    assert correction["source_value"] == "B" and correction["value"] == "A"
    assert correction["citation"]["quote"].startswith("The price proposal applies to PANEL-100 revision A")
    assert correction["citation_mentions_value"] is True and correction["resolves_review"] is True
    assert any(e["quote"] == "proposal.revision = B" for e in view["reviewed"]["evidence"])


def test_unsupported_override_is_labelled_as_a_reviewer_assumption(client):
    version = open_case(client, "missing_evidence")["version"]
    view = correct(
        client,
        "missing_evidence",
        energy_90(version, value="95.5", basis="reviewer_assumption", citation=None),
    )
    correction = view["correction"]
    assert correction["basis"] == "reviewer_assumption" and correction["citation"] is None
    assert correction["citation_mentions_value"] is None and correction["source_value"] is None
    assert view["status"] == "scenario_available"
    assert view["events"][-1]["value_before_origin"] == "not_stated"
    assert view["events"][-1]["value_before"] is None and view["events"][-1]["value_after"] == "95.5"


def test_an_override_that_does_not_resolve_the_issue_is_recorded_as_unresolved(client):
    version = open_case(client, "scope_mismatch")["version"]
    view = correct(
        client,
        "scope_mismatch",
        revision_a(version, value="C", basis="reviewer_assumption", citation=None),
    )
    assert view["status"] == "not_comparable" and view["reviewed"]["calculation"] is None
    assert view["correction"]["resolves_review"] is False
    assert view["blocking"][0]["correctable_field"] == "proposal.revision"
    assert view["what_if_available"] is False


def test_corrections_can_be_amended_and_reverted_with_true_before_and_after_values(client):
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))

    repeated = correct(client, "scope_mismatch", revision_a(view["version"]))
    assert repeated["outcome"] == "no_change"
    assert repeated["version"] == view["version"] and len(repeated["events"]) == 2

    amended = correct(
        client,
        "scope_mismatch",
        revision_a(
            view["version"], basis="reviewer_assumption", citation=None, reason="No passage was cited."
        ),
    )
    event = amended["events"][-1]
    assert event["action"] == "correction_amended"
    assert (event["value_before"], event["value_before_origin"]) == ("A", "reviewer")
    assert (event["value_after"], event["value_after_origin"]) == ("A", "reviewer")
    assert event["reason"] == "No passage was cited." and event["basis"] == "reviewer_assumption"
    assert event["reviewed_fingerprint_before"] == event["reviewed_fingerprint_after"]

    reverted = client.post(
        "/demo/scope_mismatch/correction/revert",
        json={"expected_version": amended["version"], "reason": "Returning to the source document."},
    ).json()
    event = reverted["events"][-1]
    assert event["action"] == "correction_reverted"
    assert (event["value_before"], event["value_before_origin"]) == ("A", "reviewer")
    assert (event["value_after"], event["value_after_origin"]) == ("B", "source")
    assert (event["status_before"], event["status_after"]) == ("scenario_available", "not_comparable")
    assert reverted["correction"] is None and reverted["status"] == "not_comparable"
    assert reverted["reviewed"]["analysis_fingerprint"] == reverted["events"][0]["reviewed_fingerprint_after"]

    again = client.post(
        "/demo/scope_mismatch/correction/revert",
        json={"expected_version": reverted["version"], "reason": "Nothing left to revert."},
    ).json()
    assert again["outcome"] == "no_change" and len(again["events"]) == len(reverted["events"])
    assert (
        client.post(
            "/demo/scope_mismatch/correction/revert",
            json={"expected_version": reverted["version"], "reason": "x"},
        ).status_code
        == 422
    )


# ----------------------------------------------------------------- P2: audit events


def test_history_records_matching_fingerprints_and_separates_hypothetical_events(client):
    view = open_case(client, "missing_evidence")
    fingerprints = [view["reviewed"]["analysis_fingerprint"]]
    view = correct(client, "missing_evidence", energy_90(view["version"]))
    fingerprints.append(view["reviewed"]["analysis_fingerprint"])
    assert fingerprints[0] != fingerprints[1]
    view = what_if(client, "missing_evidence", view["version"])
    assert what_if(client, "missing_evidence", view["version"])["outcome"] == "no_change"
    second = what_if(client, "missing_evidence", view["version"], {**WHAT_IF, "freight": 9})
    opened, corrected, first_what_if, second_what_if = second["events"]

    assert opened["reviewed_fingerprint_after"] == fingerprints[0]
    assert (corrected["reviewed_fingerprint_before"], corrected["reviewed_fingerprint_after"]) == tuple(
        fingerprints
    )
    assert corrected["branch"] == "reviewed" and corrected["citation"]["page"] == 4
    for event in (first_what_if, second_what_if):
        assert event["branch"] == "hypothetical"
        assert event["reviewed_fingerprint_before"] == event["reviewed_fingerprint_after"] == fingerprints[1]
        assert event["what_if_fingerprint"] not in fingerprints
    assert first_what_if["what_if_fingerprint"] == view["what_if"]["result"]["analysis_fingerprint"]
    assert second_what_if["what_if_fingerprint"] == second["what_if"]["result"]["analysis_fingerprint"]
    assert second_what_if["inputs_before"] == {
        "materials": "10",
        "energy": "-20",
        "labour": "0",
        "freight": "5",
    }
    assert second_what_if["inputs_after"]["freight"] == "9"
    assert [event["review_version"] for event in second["events"]] == [1, 2, 3, 4]
    assert second["integrity"] == {
        "latest_reviewed_event_matches_result": True,
        "latest_what_if_event_matches_result": True,
        "citation_matches_current_document": True,
    }


def test_changing_the_reviewed_basis_discards_the_what_if_and_says_so(client):
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    view = what_if(client, "scope_mismatch", view["version"])
    reverted = client.post(
        "/demo/scope_mismatch/correction/revert",
        json={"expected_version": view["version"], "reason": "Returning to the source document."},
    ).json()
    assert reverted["what_if"] is None and reverted["thresholds"] == []
    assert reverted["events"][-1]["what_if_discarded"] is True
    assert reverted["events"][-1]["inputs_before"]["materials"] == "10"

    view = what_if(client, "price_increase", open_case(client, "price_increase")["version"])
    discarded = client.post(
        "/demo/price_increase/sensitivity/discard", json={"expected_version": view["version"]}
    ).json()
    assert discarded["what_if"] is None and discarded["events"][-1]["action"] == "what_if_discarded"
    assert [item["basis"] for item in discarded["thresholds"]] == ["reviewed"]


# ----------------------------------------------------------------- restart recovery


def test_corrected_state_survives_a_new_service_instance(tmp_path):
    path = tmp_path / "reviews.json"
    settings = dict(workflow_backend="local_test_store", workflow_file=str(path), local_insecure_demo=True)
    with TestClient(create_app(Settings(**{**PUBLIC, **settings})), base_url="https://testserver") as first:
        view = correct(first, "scope_mismatch", revision_a(open_case(first, "scope_mismatch")["version"]))
        view = what_if(first, "scope_mismatch", view["version"])
        token = first.cookies[OWNER_COOKIE]
        assert view["storage"] == {**view["storage"], "backend": "local_test_store", "durable": False}
    with TestClient(create_app(Settings(**{**PUBLIC, **settings})), base_url="https://testserver") as second:
        second.cookies.set(OWNER_COOKIE, token, path="/demo")
        resumed = second.get("/demo/scope_mismatch/session")
        assert resumed.status_code == 200
        assert saved(resumed.json()) == saved(view)
        assert what_if(second, "scope_mismatch", view["version"], {**WHAT_IF, "labour": 1})["version"] == 4


# ----------------------------------------------------------------- remaining edges


def test_energy_override_compares_numbers_not_strings(client):
    version = open_case(client, "missing_evidence")["version"]
    assert correct(client, "missing_evidence", energy_90(version, value="A-1"), status=422)
    view = correct(client, "missing_evidence", energy_90(version, value="90.0"))
    assert view["correction"]["value"] == "90" and view["correction"]["citation_mentions_value"] is True
    assert view["reviewed"]["calculation"]["scenario_price"] == "102.30"
    assert (
        correct(client, "missing_evidence", energy_90(view["version"], value="90.00"))["outcome"]
        == "no_change"
    )
    wrong = energy_90(view["version"], value="100")
    assert (
        correct(client, "missing_evidence", wrong, status=422)["error"]["code"]
        == "citation_does_not_state_value"
    )
    discard = client.post(
        "/demo/missing_evidence/sensitivity/discard", json={"expected_version": view["version"]}
    )
    assert discard.json()["outcome"] == "no_change"


def test_restart_needs_an_existing_review_and_a_version(client, store):
    assert client.post("/demo/scope_mismatch/restart", json={"expected_version": 1}).status_code == 404
    view = open_case(client, "scope_mismatch")
    missing = client.post("/demo/scope_mismatch/restart", json={})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "version_required"
    assert client.post("/demo/missing_evidence/restart", json={"expected_version": 1}).status_code == 404
    restarted = client.post("/demo/scope_mismatch/restart", json={"expected_version": view["version"]}).json()
    assert restarted["events"][0]["discarded_events"] == 1 and restarted["generation"] == 2


def test_expired_review_is_gone_and_the_case_can_be_opened_again(client, store, monkeypatch):
    import sourcing_api.workflow as workflow

    correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    later = datetime.now(UTC) + timedelta(days=8)
    monkeypatch.setattr(workflow, "_now", lambda: later)
    assert client.get("/demo/scope_mismatch/session").status_code == 404
    expired = client.post("/demo/scope_mismatch/correction", json=revision_a(2, reason="Too late to amend."))
    assert expired.status_code == 404
    reopened = open_case(client, "scope_mismatch")
    assert reopened["outcome"] == "opened" and reopened["correction"] is None and reopened["version"] == 1


def test_two_tabs_opening_the_same_new_case_share_one_review(client, store):
    open_case(client, "price_increase")  # establishes the owner cookie
    original_get = store.get
    calls = []

    def first_read_misses(case, owner):
        calls.append(case)
        if len(calls) == 1:
            other = make_client(store)
            other.cookies.set(OWNER_COOKIE, client.cookies[OWNER_COOKIE], path="/demo")
            store.get = original_get
            open_case(other, "scope_mismatch")
            store.get = first_read_misses
            return None
        return original_get(case, owner)

    store.get = first_read_misses
    view = open_case(client, "scope_mismatch")
    assert view["outcome"] == "resumed" and view["version"] == 1 and len(view["events"]) == 1


def test_a_slow_store_gives_503_and_a_version_mismatch_is_refused(client, store):
    view = open_case(client, "price_increase")
    key = ("price_increase", owner_hash(client.cookies[OWNER_COOKIE]))
    record = store._records[key]
    store._records[key] = type(record)(record.payload, record.version + 5, record.expires_at)
    assert client.get("/demo/price_increase/session").json()["error"]["code"] == "stored_review_invalid"
    store._records[key] = record

    import time

    client.app.state.workflows.timeout_seconds = 0.05
    original = store.get
    store.get = lambda case, owner: (time.sleep(0.3), original(case, owner))[1]
    slow = client.get("/demo/price_increase/session")
    assert slow.status_code == 503 and slow.json()["error"]["code"] == "review_store_unavailable"
    store.get = original
    assert client.get("/demo/price_increase/session").json()["version"] == view["version"]


def corrupt(store, client, case="scope_mismatch"):
    key = (case, owner_hash(client.cookies[OWNER_COOKIE]))
    store._records[key].payload["events"][-1]["sequence"] = 7
    return key


def test_restarting_an_unreadable_record_still_respects_versions(client, store):
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    corrupt(store, client)
    wrong = client.post("/demo/scope_mismatch/restart", json={"expected_version": view["version"] - 1})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "stale_review"
    assert client.get("/demo/scope_mismatch/session").status_code == 500, "nothing was replaced"
    right = client.post("/demo/scope_mismatch/restart", json={"expected_version": view["version"]})
    assert right.status_code == 200 and right.json()["version"] == view["version"] + 1


def test_restart_of_an_unreadable_record_cannot_overwrite_a_concurrent_change(client, store):
    """The restart read version 2; another writer replaces the record before the write lands."""
    view = correct(client, "scope_mismatch", revision_a(open_case(client, "scope_mismatch")["version"]))
    corrupt(store, client)
    original_replace = store.replace

    def interleaved(case, owner, payload, version, expected_version):
        store.replace = original_replace
        other = make_client(store)
        other.cookies.set(OWNER_COOKIE, client.cookies[OWNER_COOKIE], path="/demo")
        assert other.post("/demo/scope_mismatch/restart", json={}).status_code == 200
        return original_replace(case, owner, payload, version, expected_version)

    store.replace = interleaved
    late = client.post("/demo/scope_mismatch/restart", json={})
    assert late.status_code == 409 and late.json()["error"]["code"] == "stale_review"
    current = client.get("/demo/scope_mismatch/session").json()
    assert current["version"] == view["version"] + 1 and len(current["events"]) == 1


def test_firestore_adapter_replaces_a_damaged_record_only_at_the_version_that_was_read():
    adapter = firestore_adapter()
    new = stored_document(1)
    damaged = {**stored_document(0), "version": "garbage"}  # get() reports this record as version 0
    transaction = FakeTransaction()
    with pytest.raises(WorkflowConflict):
        adapter.replace_in_transaction(
            transaction, FakeReference(stored_document(4)), "scope_mismatch", "owner", new, 0
        )
    assert transaction.writes == []
    adapter.replace_in_transaction(transaction, FakeReference(damaged), "scope_mismatch", "owner", new, 0)
    assert transaction.writes == [new]

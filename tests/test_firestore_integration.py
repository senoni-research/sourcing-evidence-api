"""Opt-in checks against a real Firestore database, using the deployed storage adapter.

These never run by default. They write fictional records to a separate test collection and
delete them afterwards:

    FIRESTORE_INTEGRATION_DATABASE=sourcing-demo GOOGLE_CLOUD_PROJECT=senoni \
        pytest tests/test_firestore_integration.py -v
"""

import asyncio
import os
import secrets
import threading

import pytest

from sourcing_api.workflow import (
    CorrectionInput,
    FirestoreWorkflowStore,
    SensitivityInput,
    WorkflowConflict,
    WorkflowService,
    owner_hash,
)

DATABASE = os.getenv("FIRESTORE_INTEGRATION_DATABASE")
COLLECTION = "sourcing_evidence_demo_reviews_itest"
pytestmark = pytest.mark.skipif(not DATABASE, reason="set FIRESTORE_INTEGRATION_DATABASE to run")

CORRECTION = dict(
    field="proposal.revision",
    value="A",
    basis="cited_passage",
    citation={"page": 4, "line": 9},
    reason="Supplier clarification on page 4 confirms revision A.",
)
WHAT_IF = {"materials": 10, "energy": -20, "labour": 0, "freight": 5}


def new_store():
    """A separate client per call, as a separate service instance would have."""
    return FirestoreWorkflowStore(database=DATABASE, collection=COLLECTION)


@pytest.fixture
def token():
    value = secrets.token_urlsafe(32)
    yield value
    store = new_store()
    for case in ("scope_mismatch", "price_increase"):
        store._reference(case, owner_hash(value)).delete()


def run(coroutine):
    return asyncio.run(coroutine)


def test_two_stale_writers_cannot_erase_each_other(token):
    first, second = WorkflowService(new_store()), WorkflowService(new_store())
    opened, _ = run(first.open("scope_mismatch", token))
    assert run(second.get("scope_mismatch", token)).version == opened.version == 1

    won = run(first.correct("scope_mismatch", token, CorrectionInput(expected_version=1, **CORRECTION)))
    assert won.version == 2
    with pytest.raises(WorkflowConflict):
        run(
            second.what_if("scope_mismatch", token, SensitivityInput(expected_version=1, changes_pct=WHAT_IF))
        )
    current = run(second.get("scope_mismatch", token))
    assert [event.action for event in current.events] == ["review_opened", "correction_applied"]
    assert current.correction.value == "A" and current.status == "scenario_available"


def test_transaction_rejects_a_stale_version_even_when_the_service_check_is_bypassed(token):
    """Call the adapter directly: the comparison must happen in the storage transaction."""
    service = WorkflowService(new_store())
    run(service.open("scope_mismatch", token))
    run(service.correct("scope_mismatch", token, CorrectionInput(expected_version=1, **CORRECTION)))
    store, owner = new_store(), owner_hash(token)
    before = store.get("scope_mismatch", owner)
    with pytest.raises(WorkflowConflict):
        store.replace("scope_mismatch", owner, {"overwritten": True}, 2, 1)
    after = store.get("scope_mismatch", owner)
    assert after.payload == before.payload and after.version == 2


def test_exactly_one_of_several_concurrent_writers_commits(token):
    service = WorkflowService(new_store())
    run(service.open("price_increase", token))
    outcomes = []
    barrier = threading.Barrier(5)

    def write(number):
        barrier.wait()
        try:
            changes = {**WHAT_IF, "materials": number}
            view = run(
                WorkflowService(new_store()).what_if(
                    "price_increase", token, SensitivityInput(expected_version=1, changes_pct=changes)
                )
            )
            outcomes.append(int(view.what_if.changes_pct["materials"]))
        except WorkflowConflict:
            outcomes.append(None)

    threads = [threading.Thread(target=write, args=(number,)) for number in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [number for number in outcomes if number is not None]
    assert len(outcomes) == 5 and len(winners) == 1
    final = run(service.get("price_increase", token))
    assert final.version == 2 and len(final.events) == 2
    assert int(final.what_if.changes_pct["materials"]) == winners[0]


def test_corrected_review_is_read_back_by_a_new_client(token):
    first = WorkflowService(new_store())
    run(first.open("scope_mismatch", token))
    run(first.correct("scope_mismatch", token, CorrectionInput(expected_version=1, **CORRECTION)))
    saved = run(
        first.what_if("scope_mismatch", token, SensitivityInput(expected_version=2, changes_pct=WHAT_IF))
    )

    resumed = run(WorkflowService(new_store()).get("scope_mismatch", token))
    assert resumed.model_dump(exclude={"outcome"}) == saved.model_dump(exclude={"outcome"})
    assert resumed.storage.backend == "firestore" and resumed.storage.durable
    assert resumed.integrity.latest_reviewed_event_matches_result
    reopened, same_token = run(WorkflowService(new_store()).open("scope_mismatch", token))
    assert reopened.outcome == "resumed" and same_token == token

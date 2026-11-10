# Verification record — 10 November 2026

This record covers the hosted evidence-review build deployed as Cloud Run revision `sourcing-evidence-00007-bz6` and served at `https://sourcing.senoni.com`, and the changes made in response to the critical review of the same date. It lists what was run and observed. It is not a production certification, and it contains no extraction-accuracy figure because nothing in this build extracts from unfamiliar documents.

The deployment was built from the working tree recorded by the commit that adds this file; `src/` was not modified between the deployment and that commit.

## Automated checks

Python 3.12.10, pinned development dependencies.

- `ruff check .` and `ruff format --check .`: passed.
- `pytest --cov=sourcing_api`: **216 passed, 4 skipped**. The four skipped tests are the opt-in Firestore tests below.
- Statement coverage: **96%** (1,445 statements, 54 uncovered). `domain`, `extraction` and `models` 100%; `workflow` 95%; `main` 95%. The uncovered workflow lines are mostly the real Firestore client calls, which only the opt-in tests reach. Coverage measures exercised code, not correctness.
- `python scripts/demo.py --in-process`: the three original cases still return `scenario_available`, `needs_information` and `not_comparable`.

### Do the regressions detect the defects?

Eight defects were put back one at a time, the hosted-workflow tests were run, and the source was restored (and compared byte-for-byte with the copy taken beforehand). Each was caught:

| Defect put back | Test that failed |
| --- | --- |
| Memory store ignores `expected_version` | `test_store_compares_versions_atomically_under_concurrent_writers` |
| Firestore transaction ignores `expected_version` | `test_firestore_adapter_compares_the_version_it_reads_in_the_transaction` |
| Service skips its version check | `test_a_stale_writer_gets_409_and_cannot_erase_history` |
| No history-limit check | `test_every_mutation_respects_the_history_limit_and_the_record_still_loads` |
| What-if break-even uses reviewed inputs | `test_break_even_thresholds_match_the_inputs_they_are_shown_beside` |
| What-if entry records the reviewed fingerprint | `test_history_records_matching_fingerprints_and_separates_hypothetical_events` |
| Amended override logs the source value as "before" | `test_corrections_can_be_amended_and_reverted_with_true_before_and_after_values` |
| Restart of an unreadable record skips the version comparison | `test_restart_of_an_unreadable_record_cannot_overwrite_a_concurrent_change` |

## The real storage adapter

`tests/test_firestore_integration.py`, run with `FIRESTORE_INTEGRATION_DATABASE=sourcing-demo` against the deployed Firestore database (run again after the restart change below), using the same `FirestoreWorkflowStore` class the service uses but a separate collection (`sourcing_evidence_demo_reviews_itest`): **4 passed**. The records it wrote were deleted by the test.

- Two service instances read version 1; the first writes; the second is refused and the first writer's history is intact.
- The adapter is called directly with a stale version, bypassing the service check: the transaction refuses and the stored document is unchanged.
- Five writers start together from the same version: exactly one commits.
- A corrected review with a what-if is read back identically through a new client.

This establishes the behaviour for these cases on this database. It is not a load test.

## Hosted service

`scripts/hosted_check.py --save`, against the public URL, at 16:49 UTC on revision `00005-j2x` and again at 19:29 UTC on `00006-9r6`:

- Opened the conflicting-revision case, recorded an override citing page 4 line 9, calculated the 10 / −20 / 0 / 5 what-if: £102.30 reviewed, £101.50 what-if, materials break-even 25.4% (reviewed basis) and 31.0% (what-if basis).
- A second what-if sent with the superseded version returned 409 `stale_review`; the saved review was unchanged.
- Opening another case and returning left the first review unchanged.
- The SHA-256 of the downloaded PDF equals the hash carried by the evidence references.
- `/docs`, `/metrics`, `/v1/examples`, `/v1/examples/{case}/document` and `/v1/reviews` returned 404; `/openapi.json` lists only `/demo` routes.
- The owner cookie is `HttpOnly`, `Secure`, `SameSite=strict`.

### Recovery across an actual restart

The service was then left idle. Cloud Run's logs show the only instance (ID ending `…3eb6094b2662`) shutting down at 17:04:46 UTC. At 17:08:38 `scripts/hosted_check.py --resume` sent the saved owner cookie. A new instance (ID ending `…599481d900be`, "Started server process" at 17:08:43) served the request and returned a review **identical** to the one saved before the shutdown: version 3, three history entries, the override, the what-if and every fingerprint. The case list reported both opened cases.

So the corrected state survived the loss of every running instance, using Firestore. That restart was an idle scale-to-zero on the same revision.

A new deployment was then exercised as well. Revision `00006-9r6` (the restart change below) was deployed at about 19:28 UTC, and at 19:29 the same saved cookie returned the same identical review from the new revision. The same check was repeated across the next deployment: a review saved on `00006-9r6` was read back identically from `00007-bz6` at 22:09 UTC.

### Landing case, header and Stack card (revision `00007-bz6`)

A link without a hash now opens the complete-evidence case, listed first; a link with a hash (for example `/#scope_mismatch`) opens that case. The header reads "FastAPI service · Pydantic contracts · fictional data", and the engineering view starts with a Stack card and the API contract. `scripts/browser_check.py` now opens the blocked case by its hash. Checked on the hosted service at 22:09 UTC: the bare link, `/#scope_mismatch` and `/#missing_evidence` each open their own case in a fresh browser, and the Stack card shows the Firestore wording, read from the live storage backend. Changing only the hash on a page that is already open does not switch case; the page has never listened for hash changes.

### Custom domain

`sourcing.senoni.com` is mapped to the service with Cloud Run domain mapping. The certificate was issued by Google Trust Services (subject `CN=sourcing.senoni.com`, valid until 8 February 2027). On that hostname at 22:09 UTC, `scripts/hosted_check.py` (save, then resume) and `scripts/browser_check.py` both passed: open a case, record an override, calculate a what-if, switch cases, reload, and recover from a stale second tab. Cloud Run domain mapping is a Preview feature, acceptable for this demonstration and not what Google recommends for production.

### Restart of an unreadable record

A follow-up review found that restarting a record whose content fails validation wrote without any version comparison. It now replaces the record only if it is still at the version read, and refuses a caller-supplied version that does not match. Three tests cover it; this was not reproduced against the hosted service, because that would mean corrupting a stored record by hand.

## Browser checks

`scripts/browser_check.py` in Chromium (Playwright), against a local instance, against the hosted URL on each revision, and against `sourcing.senoni.com`: passed each time. Desktop viewport 1363 × 936, then 390 and 320 pixels wide. It asserts, among other things:

- The blocked case (opened by its hash link) shows the disputed source line selected and marked, the issue beside it, both above the fold; the what-if is disabled with its reason.
- The override form opens empty (no value, no basis, no reason). Submitting it empty lists what is missing and moves focus to that list. Citing a line that does not state the value is refused and the entries are kept.
- After the override: £102.30; "source document states" and "reviewer asserts" both shown; the source line marked as overridden; focus on the override heading; a polite status announcement.
- What-if: editing an input shows "not calculated"; after calculating, 25.40% and 31.00% for materials in their labelled columns; editing again marks the what-if column stale; restoring the value clears it.
- No hash appears in the review view; hashes and the last API exchange are in the engineering view.
- Missing-energy case: the selected source line is `driver.energy = …current_index:UNKNOWN…`.
- Switching cases and reloading resume the saved review.
- A second tab changes the review; the first tab's next change is refused, the latest version is loaded, the typed draft is kept, and resubmitting succeeds.
- Amend opens with the reviewer's own values; revert requires a reason and is recorded with prior and new value.
- At 390 and 320 pixels there is no horizontal overflow; "Show source" switches to the source pane and focuses the line.
- Decision text is at least 14px and no text is below 13px (computed styles of six representative elements).
- No JavaScript errors; the only console errors are the two deliberately refused requests.

A selection of screenshots from both runs was inspected by eye.

## The review's acceptance checks

| # | Check | Evidence |
| --- | --- | --- |
| 1 | Case switching preserves each case's correction and history | Unit test; hosted HTTP check; browser check |
| 2 | Two stale writers cannot erase each other; 409 and a UI recovery path | Unit tests; real Firestore tests; hosted 409; browser stale-tab check |
| 3 | Every mutation respects event bounds; stored records reload | Unit tests at the 40-entry boundary for what-if, discard, correction and revert |
| 4 | Typed response schemas; invalid persisted state does not pass | Schema test over every public route; corrupted-record test returns 500 and restart recovers |
| 5 | Thresholds match active inputs (31% example) | Unit test; a second test applies each threshold and reaches £112.00; hosted and browser checks |
| 6 | Every blocked field has a visible source; source and reviewer values stay distinct | Unit tests on both blocked cases and all 42 facts; browser check |
| 7 | Editing an input marks the result stale; corrections can be amended or reverted | Browser check; unit tests |
| 8 | History shows prior value, new value, reason and matching fingerprint | Unit tests; browser check for the wording |
| 9 | Corrected state survives an actual restart on the intended adapter | Section "Recovery across an actual restart" |
| 10 | Walkthrough without reading paragraphs aloud or scrolling past history | Evidence and issue asserted above the fold at 1363 × 936; the rest is a judgement to make by rehearsing the walkthrough |

## Not verified

- Any extraction from unfamiliar documents, any model, any accuracy, cost or latency figure. None exists in this build.
- Whether a cited passage supports an override. Only existence and a text match of the value are checked.
- Real devices and assistive technology. Narrow layouts were checked in an emulated Chromium viewport; no screen reader was run.
- Load, penetration testing, IAM review, behaviour with two instances serving concurrently under traffic.
- Deletion of expired records. They are no longer served after seven days, but no Firestore TTL policy is configured. The fictional reviews created by the hosted checks above remain stored until one is.
- Docker build on a developer machine, hosted CI, Kubernetes. The image was built by Cloud Build as part of the deployment.
- The original local upload UI in a browser for this build; its automated tests passed.

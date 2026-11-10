# Engineering decision log

Scope: a small supplier price-change evidence API using fictional inputs. The immediate objective is a reviewable implementation with clear behaviour at its boundaries. It is not a procurement platform or a validated costing product.

| Decision | Why | Trade-off and next step |
| --- | --- | --- |
| **Pure Python domain function** | `domain.review()` depends on typed input and returns typed output. It performs no HTTP calls, file access or approvals. The same logic is usable from an API, batch job or test. | The API must explicitly pass all context, including the review date. Keep future storage and provider integrations outside this function. |
| **Typed, explicit API boundary** | Pydantic rejects unknown fields, invalid numbers and invalid dates. Missing business facts can be represented as `null`, producing a clear withheld calculation. Quote comparison checks scope before arithmetic. | Schema validity does not establish semantic or commercial validity. Source quotations can be checked against documents, but human review still matters. |
| **Decimal arithmetic and withholding rules** | Prices use decimal arithmetic with explicit rounding. Shares must total exactly one. Missing data, mismatched periods, stale indices and incompatible quote scope block the scenario. | The basket is an assumption applied to baseline price, not true supplier cost. GBP-only, a 90-day index-age limit and matching the baseline index year/month to the baseline quote are deliberate prototype policies that require agreement for a real use case. |
| **Stateless, synchronous request lifecycle** | The small result is returned directly to its consumer. There is no queue or database to operate. A supplied `as_of` date makes business results reproducible. Document size, page count, text length and concurrent extraction are bounded. | A model-backed review may still take seconds. Larger documents or longer work should use an asynchronous job contract, durable state and an isolated parser worker. The current parser thread is not a sandbox. |
| **Replaceable extraction adapter** | Default labelled-document parsing is deterministic and works without credentials. The optional model adapter shares the extraction boundary and can be tested with injected HTTP responses. The LLM supplies facts; deterministic code computes the price. | Live model accuracy is unmeasured. The request deadline and network timeout bound provider waiting; model evaluation, token-cost instrumentation and data-handling approval remain necessary before real use. |
| **Operational signals without document logging** | Request IDs connect responses to structured HTTP events. Aggregate metrics report request/error counts, duration and scenario/withheld counts. Document bodies, model prompts and credentials are not deliberately logged by application code. | Metrics are per process, and there is no durable audit trail. A deployment must configure log handling and review ingress/provider logging independently. Liveness and configuration readiness do not establish upstream model health. |

## Calculation boundary

For the complete fictional case:

| Driver | Declared share | Index movement | Contribution |
| --- | ---: | ---: | ---: |
| Materials | 50% | +6% | +3.0 percentage points |
| Energy | 20% | -10% | -2.0 percentage points |
| Labour | 20% | +4% | +0.8 percentage points |
| Freight | 10% | +5% | +0.5 percentage points |
| **Total** | **100%** | | **+2.3 percentage points** |

Applying 2.3% to £100 yields £102.30. The £112 proposal is £9.70 above that scenario. The correct next action is to ask what explains the difference. The model has not proved that the proposal is excessive or that £9.70 can be saved.

The scenario assumes proportional price movement, a complete and appropriate basket, unchanged margin/productivity/hedging/FX effects, comparable quote scope and representative index periods. In a real engagement, those assumptions are part of the requirements discussion.

## Evidence and reproducibility

The analysis fingerprint binds the submitted request and domain model version. It is useful for comparing repeated runs; it is not a signature, authorisation record or audit database. A byte-identical document has the same SHA-256. PDF and text representations of similar content can have different hashes and therefore different analysis fingerprints.

For document extraction, a matching excerpt shows that the quoted text exists on the cited page. It does not prove that the extracted value is correctly associated with that text. A real evaluation should distinguish **schema validity**, **citation validity**, **field correctness**, **completeness** and **business usefulness**.

## Hosted demonstration boundary

The public mode is separate from the original local upload UI. It exposes three server-selected fictional cases and one reviewer workflow. The decisions below were revised on 10 November 2026 after a critical review found that the first hosted version lost history under stale writes, stranded reviews when the case changed, and showed break-even figures that did not belong to the inputs beside them.

| Decision | Why | Trade-off and next step |
| --- | --- | --- |
| **Persist decisions, recompute results** | A stored record holds the case, at most one override with its justification, the what-if inputs and the history. Facts, results and thresholds are recomputed from the source pack on every read, so a saved number cannot disagree with the inputs shown beside it. | A change to the model or the fixture changes what an old record displays. Each history entry therefore keeps the result fingerprint it produced, and the engineering view reports whether the latest entry still matches. |
| **Three kinds of value, never merged** | A source fact is what the document states. A reviewer override is one allowlisted field with a reason and either a cited passage or the explicit label "reviewer assumption". What-if inputs are hypothetical and never alter the reviewed result. Each fact in the response carries `source_value`, `reviewer_value` and `effective_value` separately. | Only the field with a blocking issue can be overridden, one per case. A general correction model needs field-level permissions and review rules that do not exist here. |
| **Nothing is prefilled; the reviewer cites the pack** | The first version offered the "right" answer in the form. Now the value, the basis and the reason must be entered. Two cases carry a fourth, unstructured page (a supplier clarification, an index bulletin) that the labelled parser ignores; the reviewer may cite a line from it. | The service checks that the cited line exists and mentions the asserted value. That is a text match. It does not judge whether the passage supports the override, which is the gap citation-quality evaluation would have to measure. |
| **Versioned writes** | Every change carries `expected_version`. The service compares it, and the storage adapter compares it again inside the transaction that writes. A mismatch returns 409 `stale_review` and writes nothing. No request state is kept on the service object. | The check was run against a real Firestore database with two and with five concurrent writers. It has not been load-tested. |
| **One owner identity, one record per case** | A single opaque token in an HTTP-only, secure, same-site cookie identifies the browser for all three cases; only its SHA-256 hash is stored. Opening a case resumes its review instead of replacing it. | This is a demonstration identity, not authentication. Anyone holding the cookie holds the reviews. |
| **Bounded history with an explicit restart** | A review holds at most 40 entries. At the limit every change returns 409 `history_full`. Restarting a case discards its override, what-if and history; the new history begins with an entry recording how many entries were discarded. The complete state is validated before each write and again on each read; an unreadable record returns 500 `stored_review_invalid` and can only be restarted. That restart is still conditional on the stored version number, so it cannot overwrite a concurrent change. | History is discarded on restart, not archived. An audit requirement would need an append-only archive. |
| **Typed public contract** | Each `/demo` route declares `ReviewView` (or `CaseList`) and a shared `ErrorResponse`, published at `/openapi.json`. Public mode also serves `/docs` (Swagger UI over that schema) so a reviewer can read the contract and exercise the routes without cloning the repository; it shows only the `/demo` routes because the schema it renders is the filtered one, and `/redoc` stays off. | The response repeats the source pages and the full result on every call (about 40 kB). Acceptable for three fixtures, wasteful for real documents. The `/docs` page loads Swagger UI from jsDelivr at a floating major version (`swagger-ui-dist@5`) and its favicon from fastapi.tiangolo.com; a page that must not depend on third parties would self-host and pin those files. |

Firestore is required in public mode. `LOCAL_INSECURE_DEMO=true` permits the local JSON store on a developer machine for browser verification only; the engineering view labels it as not durable. Records carry `expires_at` seven days after their last change and are not served after that; nothing deletes them unless a Firestore TTL policy is configured on that field, which has not been done. The request limit is counted in memory per instance and is a courtesy bound, not a quota.

## Explicitly outside this build

Live supplier or market feeds; arbitrary-document extraction accuracy; OCR; real supplier negotiations; enterprise SSO or per-tenant isolation; persistent supplier-document storage; legally binding supplier approval; deployment into a real buyer's systems; load or penetration testing; measured business value. The bounded hosted reviewer workflow is not an enterprise audit or authorisation service. Hosted CI execution needs its own verification record. No evaluation set exists yet, so no extraction-accuracy, unsupported-value or review-routing figure is reported anywhere in this build.

# Sourcing Evidence API

An independent SENONI engineering prototype for one question: **a supplier requests a price increase; what does the declared cost-driver basket explain, and what still needs clarification?**

The service accepts a structured request or a small labelled PDF/text document, checks comparability and evidence completeness, and returns a conditional price scenario with its source trail. All supplied examples are fictional. This is not connected to any real buyer, supplier system or live market data.

## Hosted public demonstration

The hosted mode is a reviewer workspace over three fictional cases. It is deliberately narrower than the local API: no document upload, no model call, no API key in the browser.

What a reviewer can do:

- Read the source pack (the downloadable PDF, shown as page text) beside the facts parsed from it. Every fact carries the page, line and text it came from; a blocking issue names the facts it concerns.
- Resolve a blocked case with **one justified override**. Nothing is prefilled. The reviewer enters the value, gives a reason, and either cites a line of the pack or marks the override as an assumption. The source value stays visible; the override can be amended or reverted.
- Compare the reviewed scenario with a **what-if**. Hypothetical inputs never change the reviewed result. Break-even figures are computed for the reviewed inputs and for the what-if inputs, each labelled with the values it holds fixed.
- Read the history: prior value, new value, reason and basis for each change. Hashes and storage detail are in a separate engineering view.

What the service guarantees, and the test that covers it (`tests/test_public_workflow.py`):

| Behaviour | Covered by |
| --- | --- |
| Switching cases keeps each case's review and history (one owner cookie, one record per case). | `test_case_switching_preserves_each_review_and_its_history` |
| A change based on an old version returns 409 and writes nothing; the comparison is repeated in the storage transaction. | `test_a_stale_writer_gets_409_…`, `test_service_race_…`, `test_firestore_adapter_compares_…` |
| Every change respects the 40-entry history limit; a full record still loads; a restart is the defined way on. | `test_every_mutation_respects_the_history_limit_…` |
| Break-even figures follow the inputs they are shown beside (10 / −20 / 0 / 5 gives 31% for materials). | `test_break_even_thresholds_match_…` |
| Every blocked fact has its own source line, including the missing energy index. | `test_every_blocked_field_has_its_own_visible_source` |
| History entries record the real before and after values and the matching result fingerprint. | `test_history_records_matching_fingerprints_…` |
| Responses and errors are typed in `/openapi.json`; a stored record that fails validation is refused, not served. | `test_public_routes_declare_typed_…`, `test_invalid_persisted_state_is_refused_not_served` |
| Only the page, the `/demo` routes, `/openapi.json`, `/docs` and two health checks are reachable; `/redoc`, `/metrics` and every `/v1` route return 404, and the schema lists no `/v1` path. | `test_public_demo_is_deliberately_restricted`, `test_public_schema_lists_no_v1_route` |

`tests/test_firestore_integration.py` repeats the stale-write, concurrent-write and read-back checks against a real Firestore database. It is skipped unless `FIRESTORE_INTEGRATION_DATABASE` is set, writes to a separate test collection and deletes what it wrote.

Hosted routes (all under `/demo`, all in `/openapi.json`):

| Route | Purpose |
| --- | --- |
| `GET /demo/reviews` | The three cases and this browser's saved review of each. |
| `POST /demo/{case}/sessions` | Open a case: resume its review or create one. Sets the owner cookie. |
| `GET /demo/{case}/session` | Read the saved review. |
| `POST /demo/{case}/correction` | Record or amend the override. |
| `POST /demo/{case}/correction/revert` | Return to the source value. |
| `POST /demo/{case}/sensitivity` | Calculate a what-if. |
| `POST /demo/{case}/sensitivity/discard` | Discard the saved what-if. |
| `POST /demo/{case}/restart` | Start the case again; earlier history is discarded, and the new history says so. |
| `GET /demo/{case}/document` | The fictional source pack as a PDF. Its SHA-256 is the hash every evidence reference carries. |

`GET /docs` serves Swagger UI over that same schema, so the contract can be read and the demo routes called from a browser without running the code. It lists only the `/demo` routes because it renders the filtered `/openapi.json`. Swagger UI loads its script and stylesheet from cdn.jsdelivr.net; the review page itself loads nothing from a third party.

Every change sends `expected_version`. Firestore stores the reviewer's decisions and a hash of the owner token for seven days after the last change; results are recomputed on each read.

Deploy from the project root with a personal GCP account authorised for the target project:

```bash
gcloud run deploy sourcing-evidence --source . --region europe-west1 --project senoni \
  --service-account sourcing-evidence-demo@senoni.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances 0 --max-instances 2 \
  --memory 512Mi --cpu 1 --concurrency 40 --timeout 30 \
  --set-env-vars APP_ENV=production,PUBLIC_DEMO=true,WORKFLOW_BACKEND=firestore,FIRESTORE_DATABASE=sourcing-demo,GOOGLE_CLOUD_PROJECT=senoni
```

The named Firestore database and service account must already exist. Public mode refuses to start without Firestore. After a deployment, `scripts/hosted_check.py` checks the API over HTTP and, run a second time after the instances have been replaced, checks that the saved review comes back unchanged. `scripts/browser_check.py` drives the page in Chromium (it needs Playwright, which is not in the pinned environment).

To look at the hosted page on a developer machine, without Firestore:

```bash
APP_ENV=production PUBLIC_DEMO=true LOCAL_INSECURE_DEMO=true \
  WORKFLOW_BACKEND=local_test_store WORKFLOW_FILE=.demo-data/reviews.json \
  uvicorn sourcing_api.main:app --host 127.0.0.1 --port 8765 --no-access-log
```

Known limits of the hosted mode: the parser reads one labelled fixture format, so this is not AI extraction and no accuracy is measured; a citation is checked for existence and a text match only; the owner cookie is a demonstration identity, not authentication; the request limit is per instance; expired records are no longer served but are deleted only if a Firestore TTL policy is added. This is an interview demonstration, not a procurement service.

## Run locally

Use Python 3.12. From this directory:

On macOS or Linux, `bash run-demo.sh` installs the pinned dependencies in a local virtual environment and starts the demo. The equivalent manual steps are:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
uvicorn sourcing_api.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

On Windows, activate with `.venv\Scripts\Activate.ps1` instead. Open:

- Demo: <http://127.0.0.1:8000/>
- Interactive API documentation: <http://127.0.0.1:8000/docs>
- Machine-readable schema: <http://127.0.0.1:8000/openapi.json>

The default `template` extractor needs no model account or API key. It parses the published labelled-document format; **it is not LLM extraction**. Start with a built-in example, download its PDF, and upload that PDF to inspect the document route.

## Recorded preview

`Sourcing_Evidence_Demo.html` is a portable report of the saved API responses, not a live service. Rebuild it from the included records with:

```bash
python scripts/build_report.py --output Sourcing_Evidence_Demo.html
```

## The three demonstrations

| Case | Intended result |
| --- | --- |
| Complete price-change evidence | A proposed 12% increase versus a 2.3% declared-basket movement: £102.30 scenario price and £9.70/unit difference. |
| Missing energy index | `needs_information`; no calculated price. An unknown index is never treated as zero. |
| Specification revision changed | `not_comparable`; no calculated price. Resolve the scope change before comparing prices. |

The complete case uses a £100 baseline, a £112 proposal and an illustrative annual volume of 10,000 pieces. The £97,000 annualised difference is **a scenario difference, not a saving achieved or an amount available for negotiation**.

The weighting calculation is:

```text
movement = sum(share × (current_index / baseline_index − 1))
scenario_price = baseline_price × (1 + movement)
gap_per_unit = proposed_price − scenario_price
```

The model applies an assumed basket to the entire baseline price. It does not discover a supplier's true costs or distinguish price from cost. Margin, hedging, productivity, FX, volume and contract terms may change the commercial conclusion.

## Verify it

```bash
pytest --cov=sourcing_api --cov-report=term-missing
ruff check .
ruff format --check .
python scripts/demo.py --in-process
```

With the server running, the same demonstration can exercise HTTP:

```bash
python scripts/demo.py --base-url http://127.0.0.1:8000
```

The tests cover domain arithmetic and gates, schema validation, document parsing and evidence checks, simulated extraction-provider outcomes, and the hosted review workflow described above. Provider tests use injected HTTP responses. They do **not** demonstrate live model extraction accuracy, real supplier data quality or business impact. See the [verification record](docs/VERIFICATION.md) for the checks actually run for this build.

## API surface

| Route | Purpose |
| --- | --- |
| `GET /v1/examples` | List the three fictional cases. |
| `GET /v1/examples/{case}` | Obtain an editable structured request. |
| `GET /v1/examples/{case}/document` | Download the corresponding fictional PDF. |
| `POST /v1/reviews` | Review supplied structured facts. |
| `POST /v1/evidence/extract` | Extract a PDF/text upload into typed facts with source references. |
| `POST /v1/reviews/document` | Extract and review a PDF/text upload. |
| `GET /health/live` | Process liveness. |
| `GET /health/ready` | Configuration readiness; does not probe a model provider. |
| `GET /metrics` | Aggregate process counters and request duration. |

Upload endpoints accept multipart field `file`. Documents are limited to 1 MB, 10 pages and 40,000 extracted characters; the raw request limit is 1.2 MB. There is no OCR. GBP is the only supported scenario currency; no currency conversion is performed. Indices use a shared pair of periods; the baseline period must fall in the baseline quote's year/month, and the current period must be no more than 90 days old at `as_of`. These are explicit demonstration policies.

Every review returns an explicit status, a nullable calculation, questions, assumptions, evidence, a model version and an analysis fingerprint. The per-request correlation ID is separate from the deterministic analysis fingerprint. An unsuccessful business review is a valid `200` response with a withheld calculation; invalid API data or failed extraction receives an error response.

## What the evidence checks mean

- **Structured JSON:** references are supplied by the caller. `evidence_validation` is `not_verified_structured_input`; a client cannot use `extraction_mode` to claim that an uploaded source was checked.
- **Uploaded document:** the service checks cited document identity, SHA-256, page and the presence of the quoted text. `evidence_validation` is `quotations_matched_to_source_text`.
- **Neither route establishes truth:** text matching does not prove that a model interpreted a sentence correctly, that a quotation is commercially comparable, or that the supplied indices and shares are appropriate.

The service never approves a price, contacts a supplier or creates a purchase order.

## Optional model extraction

The replaceable adapter uses an OpenAI-compatible chat-completions endpoint. Configure it only against an approved gateway and with an approved model; endpoint compatibility and model quality must be evaluated separately.

```bash
export EXTRACTOR_MODE=openai_compatible
export MODEL_BASE_URL=https://your-approved-gateway.example/v1
export MODEL_NAME=your-approved-model
export MODEL_API_KEY=your-secret
export MODEL_TIMEOUT_SECONDS=15
```

Restart the service after changing these settings. `MODEL_BASE_URL` is server configuration, never a URL supplied in an uploaded document. The adapter does not execute tools or perform arithmetic. It requests typed facts and source quotations, rejects malformed output, and returns controlled timeout/provider failures. There are no automatic provider retries. **The packaged verification uses mocked responses; no live model accuracy claim is made.**

`.env.example` is a configuration reference. The application reads process environment variables; copying it to `.env` alone does not load it into the process. Keep credentials out of the repository and share only fictional data during demonstrations.

## Authentication and deployment boundary

Local development is unauthenticated by default and should remain bound to `127.0.0.1`. Set `SERVICE_API_KEY` to protect `/v1/*` and `/metrics` with the `X-API-Key` header. `APP_ENV=production` refuses startup without a service key of at least 24 characters. This is a prototype credential check, not enterprise identity or tenant isolation.

The Dockerfile runs as a non-root user and declares a liveness check. A local build/run would be:

```bash
docker build -t sourcing-evidence-api:local .
docker run --rm -p 127.0.0.1:8000:8000 sourcing-evidence-api:local
```

Docker build/run was not verified in the authoring environment. CI configuration is included for future repository runs; a workflow file is not evidence that hosted CI has passed.

Use the upload path locally with trusted fictional documents. Its byte/page/text caps do not isolate malicious compressed PDFs or fully bound parser CPU and memory. PDF parsing currently runs in a thread within the API process; public uploads need an isolated, resource-limited document worker.

Before public or enterprise use, also review ingress/TLS, identity and authorisation, tenant boundaries, secret management, dependency scanning, rate limits and deployment policy. The `/v1` API is stateless: it stores no request, document or result. Only the hosted review workflow persists anything, and only the reviewer's decisions on fictional cases (see "Hosted public demonstration"). That history is bounded and discarded on restart, so it is not an audit store. Process metrics are per instance. These are explicit next implementation decisions, not completed features.

## Read the engineering choices

- [Decision log](docs/DECISIONS.md): design choices, trade-offs and deployment boundary.
- [Verification record](docs/VERIFICATION.md): what was run for this build, and what was not.

This implementation was developed with AI assistance. Its value as an interview demonstration depends on the presenter understanding, checking and being able to defend the choices in it.

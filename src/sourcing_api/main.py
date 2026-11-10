import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .domain import review
from .examples import EXAMPLES, document_pdf, document_text
from .extraction import (
    MAX_FILE_BYTES,
    ExtractionFailure,
    OpenAICompatibleExtractor,
    TemplateExtractor,
    read_document,
)
from .models import ReviewRequest, ReviewResponse
from .workflow import (
    CASES,
    CaseList,
    CorrectionInput,
    FirestoreWorkflowStore,
    InvalidChange,
    LocalWorkflowStore,
    MemoryWorkflowStore,
    RestartInput,
    RevertInput,
    ReviewNotFound,
    ReviewView,
    SensitivityInput,
    StoredReviewInvalid,
    VersionInput,
    WorkflowConflict,
    WorkflowService,
    WorkflowUnavailable,
)

logger = logging.getLogger("sourcing_api.requests")
MAX_REQUEST_BYTES = 1_200_000
PUBLIC_CASES = set(CASES)
OWNER_COOKIE = "sourcing_demo_owner"


class ErrorDetail(BaseModel):
    code: str
    message: str


class Problem(BaseModel):
    field: str
    type: str
    message: str


class ErrorResponse(BaseModel):
    """Every error from the public demonstration has this shape."""

    error: ErrorDetail
    request_id: str
    problems: list[Problem] | None = None


DEMO_ERRORS = {
    404: {
        "model": ErrorResponse,
        "description": "`review_not_found`: this browser has no review of the case.",
    },
    409: {
        "model": ErrorResponse,
        "description": "`stale_review`: the review changed since `expected_version`; nothing was written. "
        "`history_full`: the history limit is reached; restart the case.",
    },
    422: {
        "model": ErrorResponse,
        "description": "`invalid_request`: the body does not match the schema. Other codes "
        "(`matches_source`, `citation_does_not_state_value`, `review_blocked`, ...) reject a change "
        "that is not allowed in the current review state.",
    },
    429: {"model": ErrorResponse, "description": "`demo_rate_limit`: per-instance request limit reached."},
    500: {
        "model": ErrorResponse,
        "description": "`stored_review_invalid`: the saved review failed validation and is not served.",
    },
    503: {"model": ErrorResponse, "description": "`review_store_unavailable`: storage did not respond."},
}


@dataclass(frozen=True)
class Settings:
    service_api_key: str = ""
    extractor_mode: str = "template"
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    model_timeout_seconds: float = 15
    app_env: str = "development"
    public_demo: bool = False
    public_demo_rate_limit: int = 60
    local_insecure_demo: bool = False
    workflow_backend: str = "memory"
    workflow_file: str = ".demo-data/workflows.json"
    firestore_database: str = "(default)"

    @classmethod
    def from_env(cls):
        return cls(
            service_api_key=os.getenv("SERVICE_API_KEY", ""),
            extractor_mode=os.getenv("EXTRACTOR_MODE", "template"),
            model_base_url=os.getenv("MODEL_BASE_URL", ""),
            model_name=os.getenv("MODEL_NAME", ""),
            model_api_key=os.getenv("MODEL_API_KEY", ""),
            model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "15")),
            app_env=os.getenv("APP_ENV", "development"),
            public_demo=os.getenv("PUBLIC_DEMO", "").lower() in ("1", "true", "yes"),
            public_demo_rate_limit=int(os.getenv("PUBLIC_DEMO_RATE_LIMIT", "60")),
            local_insecure_demo=os.getenv("LOCAL_INSECURE_DEMO", "").lower() in ("1", "true", "yes"),
            workflow_backend=os.getenv("WORKFLOW_BACKEND", "memory"),
            workflow_file=os.getenv("WORKFLOW_FILE", ".demo-data/workflows.json"),
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
        )

    def validate(self):
        if self.public_demo:
            if self.app_env != "production":
                raise ValueError("PUBLIC_DEMO requires APP_ENV=production")
            if not 1 <= self.public_demo_rate_limit <= 1000:
                raise ValueError("PUBLIC_DEMO_RATE_LIMIT must be between 1 and 1000")
            local_check = self.local_insecure_demo and self.workflow_backend == "local_test_store"
            if self.workflow_backend != "firestore" and not local_check:
                raise ValueError(
                    "PUBLIC_DEMO requires durable WORKFLOW_BACKEND=firestore. Only LOCAL_INSECURE_DEMO, "
                    "for verification on a developer machine, may use local_test_store."
                )
            if self.extractor_mode != "template":
                raise ValueError("PUBLIC_DEMO never enables a model extractor")
        if self.workflow_backend not in ("memory", "local_test_store", "firestore"):
            raise ValueError("Unsupported WORKFLOW_BACKEND")
        if self.app_env == "production" and not self.public_demo and len(self.service_api_key) < 24:
            raise ValueError("Production mode requires a service API key of at least 24 characters.")
        if self.extractor_mode not in ("template", "openai_compatible"):
            raise ValueError("Unsupported EXTRACTOR_MODE")
        if not 0 < self.model_timeout_seconds <= 60:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be between 0 and 60")
        if self.extractor_mode == "openai_compatible":
            parsed = urlparse(self.model_base_url)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("MODEL_BASE_URL must be a clean, server-configured base URL")
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
            ):
                raise ValueError("Use HTTPS for a remote model gateway")
            if not self.model_name or not self.model_api_key:
                raise ValueError("Model mode requires MODEL_NAME and MODEL_API_KEY")


def get_extractor(request: Request):
    return request.app.state.extractor


key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_key(request: Request, supplied: str | None = Depends(key_header)):
    expected = request.app.state.settings.service_api_key
    if expected and not hmac.compare_digest(expected.encode(), (supplied or "").encode()):
        raise HTTPException(401, "A valid X-API-Key is required")


def error_body(request, code, message):
    return {
        "error": {"code": code, "message": message},
        "request_id": getattr(request.state, "request_id", ""),
    }


class BodyLimitMiddleware:
    """Bound the raw body before multipart or JSON parsing, including chunked bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_REQUEST_BYTES:
                request_id = uuid.uuid4().hex
                response = JSONResponse(
                    {
                        "error": {"code": "request_too_large", "message": "Request exceeds 1.2 MB."},
                        "request_id": request_id,
                    },
                    413,
                    headers={"X-Request-ID": request_id},
                )
                return await response(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def unavailable():
    raise HTTPException(404, "This endpoint is available only in the local development demonstration.")


def create_app(settings: Settings | None = None, extractor=None, workflow_store=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    workflow_store = workflow_store or (
        FirestoreWorkflowStore(database=settings.firestore_database)
        if settings.workflow_backend == "firestore"
        else LocalWorkflowStore(settings.workflow_file)
        if settings.workflow_backend == "local_test_store"
        else MemoryWorkflowStore()
    )
    workflows = WorkflowService(workflow_store)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            app.state.extractor = extractor or (
                TemplateExtractor()
                if settings.extractor_mode == "template"
                else OpenAICompatibleExtractor(
                    client,
                    settings.model_base_url,
                    settings.model_name,
                    settings.model_api_key,
                    settings.model_timeout_seconds,
                )
            )
            app.state.extraction_slots = asyncio.Semaphore(4)
            yield

    app = FastAPI(
        title="Sourcing Evidence API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.public_demo else "/docs",
        redoc_url=None,
        openapi_url=None if settings.public_demo else "/openapi.json",
        description="Independent synthetic-data prototype. A conditional cost-driver scenario; never supplier approval or a verified should-cost estimate.",
    )
    app.state.settings = settings
    app.state.workflows = workflows
    app.state.demo_requests = {}
    app.state.metrics = {"requests": 0, "errors": 0, "seconds": 0.0, "scenarios": 0, "withheld": 0}

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        start = time.perf_counter()
        if settings.public_demo:
            # Cloud Run appends the connecting address as the last X-Forwarded-For entry.
            # This limit is held in memory per instance; it is a courtesy bound, not a global quota.
            forwarded = request.headers.get("x-forwarded-for", "")
            ip = forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown")
            now = time.monotonic()
            if len(app.state.demo_requests) > 2000:
                app.state.demo_requests = {
                    key: stamps for key, stamps in app.state.demo_requests.items() if now - stamps[-1] < 60
                }
            recent = [timestamp for timestamp in app.state.demo_requests.get(ip, []) if now - timestamp < 60]
            if len(recent) >= settings.public_demo_rate_limit:
                return JSONResponse(
                    error_body(
                        request, "demo_rate_limit", "Demo request limit reached. Try again in one minute."
                    ),
                    status_code=429,
                    headers={"Retry-After": "60", "X-Request-ID": request.state.request_id},
                )
            recent.append(now)
            app.state.demo_requests[ip] = recent
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "unhandled_error",
                        "request_id": request.state.request_id,
                        "error_type": type(exc).__name__,
                    }
                )
            )
            response = JSONResponse(
                error_body(request, "internal_error", "The request could not be completed."), status_code=500
            )
        elapsed = time.perf_counter() - start
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        metrics = app.state.metrics
        metrics["requests"] += 1
        metrics["errors"] += int(response.status_code >= 400)
        metrics["seconds"] += elapsed
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round(elapsed * 1000, 3),
                }
            )
        )
        return response

    @app.exception_handler(ExtractionFailure)
    async def extraction_error(request, exc):
        return JSONResponse(error_body(request, exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo submitted documents, secrets, or model data into error responses.
        problems = [
            {"field": ".".join(map(str, e["loc"])), "type": e["type"], "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                **error_body(request, "invalid_request", "Request does not match the API schema."),
                "problems": problems,
            },
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse(error_body(request, "http_error", str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(ReviewNotFound)
    async def missing_workflow(request, exc):
        return JSONResponse(error_body(request, "review_not_found", str(exc)), status_code=404)

    @app.exception_handler(WorkflowConflict)
    async def workflow_conflict(request, exc):
        return JSONResponse(error_body(request, exc.code, str(exc)), status_code=409)

    @app.exception_handler(WorkflowUnavailable)
    async def workflow_unavailable(request, exc):
        return JSONResponse(error_body(request, "review_store_unavailable", str(exc)), status_code=503)

    @app.exception_handler(InvalidChange)
    async def invalid_change(request, exc):
        return JSONResponse(error_body(request, exc.code, str(exc)), status_code=422)

    @app.exception_handler(StoredReviewInvalid)
    async def stored_review_invalid(request, exc):
        # Never serve a record that fails validation; the reviewer can restart the case.
        return JSONResponse(error_body(request, "stored_review_invalid", str(exc)), status_code=500)

    @app.exception_handler(ResponseValidationError)
    async def response_invalid(request, exc):
        logger.error(
            json.dumps({"event": "response_validation_failed", "request_id": request.state.request_id})
        )
        return JSONResponse(
            error_body(request, "internal_error", "The request could not be completed."), status_code=500
        )

    @app.get("/", include_in_schema=False)
    async def home():
        filename = "public.html" if settings.public_demo else "index.html"
        return FileResponse(Path(__file__).parent / "static" / filename)

    @app.get("/health/live", include_in_schema=False)
    async def public_liveness():
        return {"status": "alive"}

    @app.get("/health/live", include_in_schema=False, tags=["operations"])
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=not settings.public_demo, tags=["operations"])
    async def ready():
        # Configuration readiness only; not a paid call to an external model.
        return {
            "status": "ready",
            "version": __version__,
            "extractor_mode": settings.extractor_mode,
            "provider_connectivity_checked": False,
            "public_demo": settings.public_demo,
            "workflow_persistence": workflow_store.kind,
        }

    @app.get(
        "/metrics",
        include_in_schema=not settings.public_demo,
        response_class=PlainTextResponse,
        dependencies=[Depends(require_key)],
        tags=["operations"],
    )
    async def metrics():
        m = app.state.metrics
        return f"# TYPE sourcing_http_requests_total counter\nsourcing_http_requests_total {m['requests']}\n# TYPE sourcing_http_errors_total counter\nsourcing_http_errors_total {m['errors']}\n# TYPE sourcing_http_duration_seconds_sum counter\nsourcing_http_duration_seconds_sum {m['seconds']:.6f}\n# TYPE sourcing_scenarios_total counter\nsourcing_scenarios_total {m['scenarios']}\n# TYPE sourcing_withheld_total counter\nsourcing_withheld_total {m['withheld']}\n"

    @app.get(
        "/v1/examples",
        include_in_schema=not settings.public_demo,
        dependencies=[Depends(require_key)],
        tags=["fictional examples"],
    )
    async def examples():
        return {"items": EXAMPLES}

    def require_example(case):
        if case not in PUBLIC_CASES:
            raise HTTPException(404, "Unknown example")

    if settings.public_demo or settings.workflow_backend == "local_test_store":
        demo = {"tags": ["fictional evidence review"], "include_in_schema": settings.public_demo}
        typed = {**demo, "response_model": ReviewView, "responses": DEMO_ERRORS}

        def owner(request: Request) -> str | None:
            return request.cookies.get(OWNER_COOKIE)

        @app.get(
            "/demo/reviews",
            **demo,
            response_model=CaseList,
            responses={429: DEMO_ERRORS[429], 503: DEMO_ERRORS[503]},
            summary="List the fictional cases and this browser's saved review of each",
        )
        async def list_demo(request: Request):
            return await workflows.summaries(owner(request))

        @app.post("/demo/{case}/sessions", **typed, summary="Open a case: resume its review or create one")
        async def open_demo(case: str, request: Request, response: Response):
            view, token = await workflows.open(case, owner(request))
            # One stable, opaque owner identity for every case; only its hash is stored.
            response.set_cookie(
                OWNER_COOKIE,
                token,
                max_age=604800,
                httponly=True,
                secure=settings.public_demo and not settings.local_insecure_demo,
                samesite="strict",
                path="/demo",
            )
            return view

        @app.get("/demo/{case}/session", **typed, summary="Read the saved review without changing it")
        async def resume_demo(case: str, request: Request):
            return await workflows.get(case, owner(request))

        @app.post("/demo/{case}/correction", **typed, summary="Record or amend a justified reviewer override")
        async def correct_demo(case: str, correction: CorrectionInput, request: Request):
            return await workflows.correct(case, owner(request), correction)

        @app.post(
            "/demo/{case}/correction/revert", **typed, summary="Revert the override to the source value"
        )
        async def revert_demo(case: str, change: RevertInput, request: Request):
            return await workflows.revert(case, owner(request), change)

        @app.post("/demo/{case}/sensitivity", **typed, summary="Calculate a hypothetical what-if")
        async def sensitivity_demo(case: str, sensitivity: SensitivityInput, request: Request):
            return await workflows.what_if(case, owner(request), sensitivity)

        @app.post("/demo/{case}/sensitivity/discard", **typed, summary="Discard the saved what-if")
        async def discard_sensitivity_demo(case: str, change: VersionInput, request: Request):
            return await workflows.discard_what_if(case, owner(request), change)

        @app.post("/demo/{case}/restart", **typed, summary="Restart the case; earlier history is discarded")
        async def restart_demo(case: str, change: RestartInput, request: Request):
            return await workflows.restart(case, owner(request), change)

        @app.get(
            "/demo/{case}/document",
            **demo,
            response_class=Response,
            responses={200: {"content": {"application/pdf": {}}}, 404: DEMO_ERRORS[404]},
            summary="Download the fictional source pack the review cites",
        )
        async def demo_document(case: str):
            if case not in PUBLIC_CASES:
                raise ReviewNotFound("Unknown fictional case.")
            content = await run_in_threadpool(document_pdf, case, True)
            return Response(
                content,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{case}-review-pack.pdf"'},
            )

    @app.get(
        "/v1/examples/{case}",
        response_model=ReviewRequest,
        include_in_schema=not settings.public_demo,
        dependencies=[Depends(require_key)],
        tags=["fictional examples"],
    )
    async def example_request(case: str):
        require_example(case)
        return await TemplateExtractor().extract(read_document(document_text(case).encode(), f"{case}.txt"))

    @app.get(
        "/v1/examples/{case}/document",
        include_in_schema=not settings.public_demo,
        tags=["fictional examples"],
    )
    async def example_document(case: str):
        require_example(case)
        content = await run_in_threadpool(document_pdf, case)
        return Response(
            content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{case}.pdf"'},
        )

    def finish(data: ReviewRequest, request: Request, started, evidence_validation):
        result = review(data)
        result.request_id = request.state.request_id
        result.evidence_validation = evidence_validation
        result.processing_ms = round((time.perf_counter() - started) * 1000, 3)
        app.state.metrics["scenarios" if result.status == "scenario_available" else "withheld"] += 1
        return result

    @app.post(
        "/v1/reviews", response_model=ReviewResponse, dependencies=[Depends(require_key)], tags=["reviews"]
    )
    async def structured_review(data: ReviewRequest, request: Request):
        start = time.perf_counter()
        data.extraction_mode = "structured_input"  # clients cannot claim source validation
        return finish(data, request, start, "not_verified_structured_input")

    async def extract_upload(file: UploadFile, request: Request, provider):
        try:
            await asyncio.wait_for(app.state.extraction_slots.acquire(), timeout=0.1)
        except TimeoutError as exc:
            raise ExtractionFailure(
                "extraction_busy", "Extraction capacity is busy. Retry later.", 429
            ) from exc
        try:
            content = await file.read(MAX_FILE_BYTES + 1)
            document = await run_in_threadpool(read_document, content, file.filename or "upload")
            # A total provider deadline complements httpx's individual network timeouts.
            try:
                data = await asyncio.wait_for(
                    provider.extract(document), timeout=settings.model_timeout_seconds + 1
                )
            except TimeoutError as exc:
                raise ExtractionFailure(
                    "provider_timeout", "Extraction timed out; no calculation was produced.", 504
                ) from exc
            return data, document
        finally:
            app.state.extraction_slots.release()
            await file.close()

    @app.post("/v1/evidence/extract", dependencies=[Depends(require_key)], tags=["evidence"])
    async def extract_evidence(
        request: Request, file: UploadFile = File(...), provider=Depends(get_extractor)
    ):
        data, document = await extract_upload(file, request, provider)
        return {
            "request": data.model_dump(mode="json"),
            "extraction_mode": provider.mode,
            "evidence_validation": "quotations_matched_to_source_text",
            "document_sha256": document.sha256,
        }

    @app.post(
        "/v1/reviews/document",
        response_model=ReviewResponse,
        dependencies=[Depends(require_key)],
        tags=["reviews"],
    )
    async def document_review(
        request: Request, file: UploadFile = File(...), provider=Depends(get_extractor)
    ):
        start = time.perf_counter()
        data, _ = await extract_upload(file, request, provider)
        return finish(data, request, start, "quotations_matched_to_source_text")

    if settings.public_demo:

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            raise HTTPException(404)

        @app.get("/openapi.json", include_in_schema=False)
        async def public_openapi():
            return app.openapi()

        @app.get("/docs", include_in_schema=False)
        async def public_docs():
            return get_swagger_ui_html(
                openapi_url="/openapi.json", title="Sourcing Evidence API · demo routes"
            )

        retained = []
        for route in app.router.routes:
            allowed = isinstance(route, APIRoute) and (
                route.path == "/"
                or route.path == "/openapi.json"
                or route.path == "/docs"
                or route.path.startswith("/demo/")
                or (
                    route.path in ("/health/live", "/health/ready")
                    and route.name in ("public_liveness", "ready")
                )
            )
            if allowed:
                retained.append(route)
        app.router.routes[:] = retained
        app.openapi_schema = app.openapi()

    app.add_middleware(BodyLimitMiddleware)
    return app


app = create_app()

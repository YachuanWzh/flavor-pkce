"""API Gateway — JWT verification + transparent proxy to upstream LLM API.

Supports both regular (non-streaming) and SSE streaming responses.
Every request is logged as a single JSON line and tracked via Prometheus
metrics exposed at ``/metrics``.  Audit logs are persisted to SQLite and
browsable at ``/audit``.

Token usage (prompt / completion tokens and provider prompt-cache
breakdowns) is extracted from both non-streaming and streaming
responses.  For SSE streams the full usage is recovered from the
buffered body once the stream is drained (Anthropic ``message_start``
/ ``message_delta``; OpenAI final usage chunk).
"""

import json as _json
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from prometheus_client import generate_latest, REGISTRY

import gateway.config
from gateway.database import (
    init_audit_db, query_logs, query_log_by_id, clear_logs, verify_integrity,
)
from gateway.ssrf import validate_upstream_url
from gateway.logging import setup_logging, log_request
from gateway.metrics import (
    REQUEST_COUNT,
    REQUEST_DURATION,
    UPSTREAM_ERRORS,
    ACTIVE_CONNECTIONS,
    normalize_path,
)

# ---- Bootstrap ----

setup_logging()
init_audit_db()

app = FastAPI(title="PKCE API Gateway")

_public_key = None
_routing_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_revoked_cache: dict[str, tuple[float, bool]] = {}


async def _is_jti_revoked(jti: str) -> bool:
    """Ask the auth server whether this access-token jti was revoked.

    The verdict is cached for REVOCATION_CACHE_TTL_SECONDS.  If the auth
    server is unreachable we fail *open* (the JWT signature/expiry were
    already verified locally); revocation is a mitigation, not the primary
    gate.
    """
    cached = _revoked_cache.get(jti)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    url = (
        f"{gateway.config.AUTH_SERVER_INTERNAL_URL.rstrip('/')}"
        f"/internal/tokens/revoked"
    )
    revoked = False
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                url,
                params={"jti": jti},
                headers={
                    "X-Internal-Service-Token": gateway.config.INTERNAL_SERVICE_TOKEN,
                },
            )
        if response.status_code == 200:
            revoked = bool(response.json().get("revoked"))
    except (httpx.HTTPError, ValueError):
        revoked = False

    ttl = gateway.config.REVOCATION_CACHE_TTL_SECONDS
    if ttl > 0:
        _revoked_cache[jti] = (time.monotonic() + ttl, revoked)
    return revoked

# Path prefixes that the observability middleware should NOT audit-log.
_AUDIT_SKIP_PREFIXES = ("/metrics", "/api/logs", "/api/stats", "/health", "/report")

# Path to the static audit-viewer HTML page.
_AUDIT_HTML_PATH = Path(__file__).parent / "web" / "logs.html"

# Path to the static usage-report HTML page.
_REPORT_HTML_PATH = Path(__file__).parent / "web" / "report.html"


def load_public_key():
    """Load the RSA public key. Cached after first load."""
    global _public_key
    if _public_key is not None:
        return _public_key
    with open(gateway.config.JWT_PUBLIC_KEY_PATH, "rb") as f:
        _public_key = serialization.load_pem_public_key(
            f.read(), backend=default_backend(),
        )
    return _public_key


def verify_jwt(token: str) -> dict | None:
    """Verify JWT signature and expiration. Returns payload or None."""
    try:
        return pyjwt.decode(token, load_public_key(), algorithms=["RS256"])
    except Exception:
        return None


async def _resolve_user_routing(payload: dict) -> tuple[dict | None, JSONResponse | None]:
    """Resolve a signed token to private upstream routing configuration."""
    user_id = str(payload.get("sub", ""))
    version = payload.get("config_version")
    if not user_id or not isinstance(version, int):
        return {
            "service_name": "legacy-upstream",
            "upstream_url": gateway.config.UPSTREAM_URL,
            "upstream_api_key": gateway.config.UPSTREAM_API_KEY,
            "upstream_auth_type": gateway.config.UPSTREAM_AUTH_TYPE,
            "models": [],
        }, None
    key = (user_id, version)
    cached = _routing_cache.get(key)
    if gateway.config.ROUTING_CACHE_TTL_SECONDS > 0 and cached is not None and cached[0] > time.monotonic():
        return cached[1], None
    url = (
        f"{gateway.config.AUTH_SERVER_INTERNAL_URL.rstrip('/')}"
        f"/internal/users/{user_id}/llm-config"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(
                url,
                params={"version": version},
                headers={
                    "X-Internal-Service-Token": gateway.config.INTERNAL_SERVICE_TOKEN,
                },
            )
    except httpx.HTTPError:
        return None, JSONResponse(
            status_code=502,
            content={"error": "User LLM configuration unavailable"},
        )
    if response.status_code == 409:
        return None, JSONResponse(
            status_code=401,
            content={"error": "configuration_changed", "message": "Run /login to refresh LLM configuration"},
        )
    if response.status_code != 200:
        return None, JSONResponse(
            status_code=502,
            content={"error": "User LLM configuration unavailable"},
        )
    try:
        routing = response.json()
    except ValueError:
        return None, JSONResponse(status_code=502, content={"error": "Invalid routing response"})

    # SSRF guard (P0-7): refuse to proxy to private/metadata/loopback targets,
    # even when the user controls their own upstream_url. Operator-approved
    # hosts (UPSTREAM_URL_ALLOWLIST) bypass the check.
    if not validate_upstream_url(
        str(routing.get("upstream_url", "")),
        gateway.config.UPSTREAM_URL_ALLOWLIST,
    ):
        return None, JSONResponse(
            status_code=400,
            content={
                "error": "upstream_url not allowed",
                "detail": "The configured upstream URL is not a public HTTP(S) endpoint",
            },
        )

    if gateway.config.ROUTING_CACHE_TTL_SECONDS > 0:
        _routing_cache[key] = (
            time.monotonic() + gateway.config.ROUTING_CACHE_TTL_SECONDS,
            routing,
        )
    return routing, None


# ---------------------------------------------------------------------------
# Fixed routes (must be registered BEFORE the catch-all proxy)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4",
    )


# ---------------------------------------------------------------------------
# Audit-log API
# ---------------------------------------------------------------------------

def _extract_jwt_from_request(request: Request) -> str | None:
    """Extract a JWT from the Authorization header or x-api-key header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.headers.get("x-api-key") or None


def _require_admin(request: Request) -> dict:
    """Require a valid JWT whose role claim is 'admin'.

    This is the primary gate for the audit-log API.  It returns the
    verified payload so callers can attribute the action.
    """
    token = _extract_jwt_from_request(request)
    payload = verify_jwt(token) if token else None
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Administrator access required",
        )
    return payload


def _require_audit_token(request: Request) -> dict:
    """Gate the audit data API behind admin JWT or the legacy shared secret (P0-1).

    Primary auth: a signed JWT with role=admin, supplied either via the
    ``Authorization: Bearer`` header (scripts/SPA) or the ``access_token``
    HttpOnly cookie set by the auth server on login (SSO in the browser).
    Legacy fallback: the operator-configured AUDIT_API_TOKEN via the
    ``x-audit-token`` header. When no credential matches, the API is
    fail-closed (401/403).
    """
    token = _extract_jwt_from_request(request)
    if not token:
        token = request.cookies.get("access_token", "")
    if token:
        payload = verify_jwt(token)
        if payload is not None:
            if payload.get("role") == "admin":
                return payload
            raise HTTPException(
                status_code=403,
                detail="Administrator access required",
            )

    configured = gateway.config.AUDIT_API_TOKEN
    if not configured:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )
    supplied = request.headers.get("x-audit-token", "")
    if not secrets.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=401,
            detail="Invalid audit token",
        )
    return {"role": "admin"}


@app.get("/api/logs")
def api_logs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    keyword: str | None = Query(None),
    user: str | None = Query(None),
):
    """Paginated audit-log query with optional filters."""
    _require_audit_token(request)
    return query_logs({
        "page": page,
        "page_size": page_size,
        "start_date": start_date,
        "end_date": end_date,
        "keyword": keyword,
        "user": user,
    })


@app.get("/api/logs/integrity")
def api_logs_integrity(request: Request):
    """Report whether the hash chain verifies (tamper detection)."""
    _require_audit_token(request)
    return {"valid": verify_integrity()}


@app.get("/api/logs/{log_id}")
def api_log_detail(request: Request, log_id: int):
    """Return a single audit-log entry including request/response bodies."""
    _require_audit_token(request)
    row = query_log_by_id(log_id)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "Log not found"})
    return row


@app.delete("/api/logs")
def api_clear_logs(request: Request):
    """Clear all audit log entries."""
    _require_audit_token(request)
    count = clear_logs()
    return {"deleted": count}


@app.get("/audit")
def audit_page():
    """Serve the audit-log viewer HTML page."""
    return FileResponse(_AUDIT_HTML_PATH, media_type="text/html; charset=utf-8")


@app.get("/report")
def report_page():
    """Serve the usage-report dashboard HTML page."""
    return FileResponse(_REPORT_HTML_PATH, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Report (stats) API — aggregates for the dashboard
# ---------------------------------------------------------------------------

@app.get("/api/stats/tokens")
def api_stats_tokens(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
    group_by: str | None = Query(None),
):
    """Daily token usage, optionally grouped by user / model / service."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.token_usage(start_date, end_date, user, group_by)}


@app.get("/api/stats/cache")
def api_stats_cache(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
):
    """Daily prompt-cache token usage and hit ratio."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.cache_usage(start_date, end_date, user)}


@app.get("/api/stats/requests")
def api_stats_requests(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
):
    """Daily request volume, errors and average latency."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.request_stats(start_date, end_date, user)}


@app.get("/api/stats/models")
def api_stats_models(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """Top models ranked by total token consumption."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.top_models(start_date, end_date, user, limit)}


# ---------------------------------------------------------------------------
# Middleware: logging + metrics for every request
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_metrics_middleware(request: Request, call_next):
    """Log every request as JSON and update Prometheus counters."""
    start = time.perf_counter()
    ACTIVE_CONNECTIONS.inc()
    status_code = 0

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        status_code = 502
        raise
    finally:
        ACTIVE_CONNECTIONS.dec()
        duration = time.perf_counter() - start

        path_label = normalize_path(request.url.path)
        REQUEST_COUNT.labels(
            method=request.method, path=path_label,
            status_code=str(status_code),
        ).inc()
        REQUEST_DURATION.labels(
            method=request.method, path=path_label,
        ).observe(duration)

        if not any(
            request.url.path.startswith(p) for p in _AUDIT_SKIP_PREFIXES
        ):
            user = getattr(request.state, "user_sub", "-")
            session_id = getattr(request.state, "session_id", None)
            upstream_ms = getattr(request.state, "upstream_ms", None)
            prompt_tokens = getattr(request.state, "prompt_tokens", None)
            completion_tokens = getattr(request.state, "completion_tokens", None)
            cache_read_tokens = getattr(request.state, "cache_read_tokens", None)
            cache_creation_tokens = getattr(request.state, "cache_creation_tokens", None)
            model = getattr(request.state, "model", None)
            service_name = getattr(request.state, "service_name", None)
            user_id = getattr(request.state, "user_id", None)
            request_body = getattr(request.state, "request_body", None)
            response_body = getattr(request.state, "response_body", None)
            level = "ERROR" if status_code >= 500 else "INFO"

            log_request(
                user=user,
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=duration * 1000,
                upstream_ms=upstream_ms,
                level=level,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                model=model,
                service_name=service_name,
                session_id=session_id,
                user_id=user_id,
                request_body=request_body,
                response_body=response_body,
            )


# ---------------------------------------------------------------------------
# Catch-all proxy
# ---------------------------------------------------------------------------

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(request: Request, path: str):
    """Transparent proxy with JWT verification. Supports SSE streaming."""
    # --- Verify JWT ---
    token: str | None = None
    x_api_key = request.headers.get("x-api-key", "")
    auth_header = request.headers.get("authorization", "")

    if x_api_key:
        token = x_api_key
    elif auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if token is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Missing or invalid Authorization header"},
        )

    payload = verify_jwt(token)
    if payload is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or expired JWT"},
        )

    # Reject tokens whose jti was revoked at the auth server (P0-3).
    jti = payload.get("jti")
    if jti and await _is_jti_revoked(jti):
        return JSONResponse(
            status_code=401,
            content={"error": "Token has been revoked"},
        )

    # Human-readable username comes from the JWT 'username' claim (added
    # by the auth server).  Older tokens only have 'sub' (a UUID).
    request.state.user_sub = payload.get("username") or payload.get("sub", "-")
    # Stable identifier for cross-system joins (auth-server users.id).
    request.state.user_id = payload.get("sub") or None

    # Session identifier: prefer the JWT 'jti' (JWT ID) claim; fall back
    # to 'sid' (session ID) or generate one from sub + current time.
    request.state.session_id = (
        payload.get("jti")
        or payload.get("sid")
        or f"{payload.get('sub', 'unknown')}-{int(time.time())}"
    )

    routing, routing_error = await _resolve_user_routing(payload)
    if routing_error is not None:
        return routing_error
    assert routing is not None

    # SSRF defense-in-depth (P0-7): re-validate user-controlled routing at the
    # proxy layer too, so a routing entry from any source (cache, internal API)
    # cannot target private/metadata addresses. The legacy fallback upstream
    # is operator-configured (UPSTREAM_URL env) and is trusted, so it is exempt.
    if routing.get("service_name") != "legacy-upstream":
        if not validate_upstream_url(
            str(routing.get("upstream_url", "")),
            gateway.config.UPSTREAM_URL_ALLOWLIST,
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "upstream_url not allowed",
                    "detail": "The configured upstream URL is not a public HTTP(S) endpoint",
                },
            )

    # --- Build upstream request ---
    upstream_url = (
        f"{routing['upstream_url'].rstrip('/')}/{path.lstrip('/')}"
    )
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    _apply_upstream_auth(
        headers,
        routing.get("upstream_api_key", ""),
        routing.get("upstream_auth_type", "x-api-key"),
    )

    body = await request.body()

    # Capture the request body for audit logging.
    request.state.request_body = _decode_body(body)

    # Extract model name from request body early, before any upstream call.
    # Both OpenAI and Anthropic put it at the top-level "model" key.
    request.state.model = _extract_model(body)
    request.state.service_name = routing.get("service_name")
    if not _is_model_allowed(body, routing):
        return JSONResponse(
            status_code=403,
            content={"error": "model_not_allowed", "model": request.state.model},
        )

    upstream_start = time.perf_counter()

    # --- Proxy ---
    client = httpx.AsyncClient(timeout=300.0)
    took_sse = False

    try:
        resp = await client.send(
            client.build_request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body,
            ),
            stream=True,
        )

        content_type = resp.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type

        if is_sse:
            took_sse = True
            # Forward SSE chunks to the client in real time while
            # collecting them in the background for audit logging.
            collected: list[bytes] = []
            first_chunk_done = False
            upstream_stream = resp.aiter_bytes()

            async def sse_generator():
                nonlocal first_chunk_done
                try:
                    async for chunk in upstream_stream:
                        # Capture audit data on the fly.
                        if not first_chunk_done:
                            first_chunk_done = True
                            request.state.prompt_tokens = (
                                _extract_input_tokens_from_chunk(chunk)
                            )
                        collected.append(chunk)
                        yield chunk
                finally:
                    # Finalise audit state after the stream is drained.
                    combined = b"".join(collected)
                    request.state.response_body = _decode_body(combined)
                    # Usage events arrive late in the stream (Anthropic
                    # message_delta / OpenAI final usage chunk); recover
                    # them from the full buffered body now.
                    _extract_usage_from_stream(request, combined)
                    request.state.upstream_ms = (
                        (time.perf_counter() - upstream_start) * 1000
                    )
                    await resp.aclose()
                    await client.aclose()

            resp_headers = _clean_response_headers(
                dict(resp.headers), streaming=True,
            )
            status_code = resp.status_code
            if status_code >= 500:
                UPSTREAM_ERRORS.labels(method=request.method, path=path).inc()

            return StreamingResponse(
                sse_generator(),
                status_code=status_code,
                headers=resp_headers,
            )

        # Non-streaming — read entire body and extract full token usage.
        content = await resp.aread()
        request.state.response_body = _decode_body(content)
        resp_headers = _clean_response_headers(
            dict(resp.headers), streaming=False,
        )
        status_code = resp.status_code
        if status_code >= 500:
            UPSTREAM_ERRORS.labels(method=request.method, path=path).inc()
        request.state.upstream_ms = (
            (time.perf_counter() - upstream_start) * 1000
        )
        _extract_full_token_usage(request, content)

        return Response(
            content=content,
            status_code=status_code,
            headers=resp_headers,
        )

    except httpx.HTTPError:
        UPSTREAM_ERRORS.labels(
            method=request.method, path=path,
        ).inc()
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream provider unreachable"},
        )
    except Exception:
        UPSTREAM_ERRORS.labels(
            method=request.method, path=path,
        ).inc()
        raise
    finally:
        if not took_sse:
            await client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_response_headers(headers: dict, *, streaming: bool = False) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    headers.pop("transfer-encoding", None)
    headers.pop("content-encoding", None)
    if streaming:
        headers.pop("content-length", None)
    return headers


def _apply_upstream_auth(headers: dict, api_key: str, auth_type: str) -> None:
    """Remove client credentials and apply only the resolved upstream key."""
    headers.pop("x-api-key", None)
    headers.pop("authorization", None)
    headers.pop("api-key", None)
    if not api_key:
        return
    if auth_type == "bearer":
        headers["authorization"] = f"Bearer {api_key}"
    elif auth_type == "api-key":
        headers["api-key"] = api_key
    else:
        headers["x-api-key"] = api_key


def _is_model_allowed(body: bytes, routing: dict) -> bool:
    models = routing.get("models") or []
    if not models:
        return True
    model = _extract_model(body)
    return model is not None and model in models


def _extract_full_token_usage(request: Request, body: bytes) -> None:
    """Parse an upstream LLM JSON response for token usage.

    Handles OpenAI (usage.prompt_tokens/completion_tokens) and Anthropic
    (usage.input_tokens/output_tokens) formats, plus provider prompt-cache
    breakdowns (Anthropic ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``; OpenAI
    ``input_tokens_details.cached_tokens``).
    """
    usage = _parse_usage_json(body)
    request.state.prompt_tokens = _int_or_none(
        usage, "prompt_tokens", "input_tokens",
    )
    request.state.completion_tokens = _int_or_none(
        usage, "completion_tokens", "output_tokens",
    )
    request.state.cache_read_tokens = _cache_read_from_usage(usage)
    request.state.cache_creation_tokens = _int_or_none(
        usage, "cache_creation_input_tokens",
    )


def _cache_read_from_usage(usage: dict) -> int | None:
    """Extract cache-read tokens from a usage object (any provider)."""
    direct = _int_or_none(usage, "cache_read_input_tokens")
    if direct is not None:
        return direct
    details = usage.get("input_tokens_details")
    if isinstance(details, dict):
        return _int_or_none(details, "cached_tokens")
    return None


def _extract_usage_from_stream(request: Request, body: bytes) -> None:
    """Recover full token usage from a collected SSE stream body.

    Streaming responses deliver usage in late events (Anthropic
    ``message_start`` for input tokens, ``message_delta`` for output
    tokens; OpenAI emits a final chunk with a complete ``usage`` object
    when usage reporting is enabled).  By the time this runs the entire
    stream has been buffered, so every ``data:`` frame is parsed and the
    usage fields merged together.  Fields that never appear stay ``None``.
    """
    text = body.decode("utf-8", errors="replace")
    prompt = None
    completion = None
    cache_read = None
    cache_creation = None
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if obj.get("type") == "message_start":
            usage = obj.get("message", {}).get("usage")
        if not isinstance(usage, dict):
            continue
        value = _int_or_none(usage, "prompt_tokens", "input_tokens")
        if value is not None:
            prompt = value
        value = _int_or_none(usage, "completion_tokens", "output_tokens")
        if value is not None:
            completion = value
        value = _cache_read_from_usage(usage)
        if value is not None:
            cache_read = value
        value = _int_or_none(usage, "cache_creation_input_tokens")
        if value is not None:
            cache_creation = value
    request.state.prompt_tokens = prompt
    request.state.completion_tokens = completion
    request.state.cache_read_tokens = cache_read
    request.state.cache_creation_tokens = cache_creation


def _extract_input_tokens_from_chunk(chunk: bytes) -> int | None:
    """Try to extract input_tokens from the first SSE data event.

    Anthropic sends a ``message_start`` event as the very first data frame:
    ``data: {"type":"message_start","message":{"usage":{"input_tokens":150}}}``

    For other providers the first chunk typically does not contain usage.
    """
    text = chunk.decode("utf-8", errors="replace")
    usage = _parse_usage_from_sse_data(text)
    return _int_or_none(usage, "input_tokens", "prompt_tokens")


def _parse_usage_json(body: bytes) -> dict:
    try:
        data = _json.loads(body)
        return (
            data.get("usage")
            or data.get("message", {}).get("usage")
            or {}
        )
    except Exception:
        return {}


def _parse_usage_from_sse_data(text: str) -> dict:
    """Scan SSE text for a data: line containing a usage object.

    Handles both Anthropic message_start (usage inside message)
    and OpenAI final-chunk usage patterns.
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue
        # Anthropic message_start
        if obj.get("type") == "message_start":
            msg = obj.get("message", {})
            if "usage" in msg:
                return msg["usage"]
        # Anthropic message_delta / content_block_stop (output tokens)
        usage = obj.get("usage")
        if usage is not None:
            return usage
    return {}


def _int_or_none(d: dict, *keys: str) -> int | None:
    for k in keys:
        val = d.get(k)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def _decode_body(body: bytes) -> str | None:
    """Safely decode a request/response body to a UTF-8 string.

    Returns ``None`` for empty bodies.
    """
    if not body:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _extract_model(body: bytes) -> str | None:
    """Pull the ``model`` field from the JSON request body.

    Both OpenAI and Anthropic APIs include the model name at the
    top-level ``model`` key in the request payload (e.g. ``"deepseek-v4-pro"``,
    ``"claude-sonnet-4-5"``).
    """
    try:
        data = _json.loads(body)
        val = data.get("model")
        return str(val) if val else None
    except Exception:
        return None

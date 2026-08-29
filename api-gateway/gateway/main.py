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

import base64
import hashlib
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
from pydantic import BaseModel

import gateway.config
import gateway.quota
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

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Startup governance: retention purge + anomaly scan + periodic rescans."""
    import asyncio

    from gateway.retention import purge_old_logs
    from gateway.anomaly import scan_daily
    purge_old_logs()      # audit retention policy (like auth-server P0-10)
    scan_daily()          # evaluate the completed day so the dashboard is fresh

    async def _rescan_loop():
        while True:
            await asyncio.sleep(gateway.config.ALERT_SCAN_INTERVAL_SECONDS)
            try:
                await asyncio.to_thread(scan_daily)
            except Exception:
                pass  # scheduling must never take the gateway down

    scanner: asyncio.Task | None = None
    if gateway.config.ALERT_SCAN_INTERVAL_SECONDS > 0:
        scanner = asyncio.create_task(_rescan_loop())
    yield
    if scanner is not None:
        scanner.cancel()


app = FastAPI(title="PKCE API Gateway", lifespan=_lifespan)

_public_key = None
_routing_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_revoked_cache: dict[str, tuple[float, bool]] = {}

# Circuit-breaker cooldowns for intelligent routing: (user_id, service_name)
# → monotonic deadline until which the route is skipped after a failure.
_route_cooldowns: dict[tuple[str, str], float] = {}


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
_AUDIT_SKIP_PREFIXES = (
    "/metrics", "/api/logs", "/api/stats", "/api/query", "/api/agent",
    "/api/alerts", "/api/prices",
    "/health", "/report",
)

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


# ---------------------------------------------------------------------------
# JWT keyring (improvement 5): keys resolved by the token's ``kid`` header.
#
# The shared public-key file bootstraps the ring; an unknown kid triggers a
# cooldown-limited fetch of auth-server's /.well-known/jwks.json. Rotating
# the signing key only requires restarting auth-server — the gateway picks
# the new key up on the first token that carries the new kid, and pre-
# rotation keys keep verifying until those tokens expire.
# ---------------------------------------------------------------------------

_jwt_keyring: dict[str, object] = {}
_jwks_file_key_id: str | None = None
_jwks_last_fetch = 0.0


def _key_id_for_public(public_key) -> str:
    """Same derivation as auth_server.jwt_utils.key_id."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:16]


def _install_jwk(kid: str, n: int, e: int) -> None:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    _jwt_keyring[kid] = RSAPublicNumbers(e, n).public_key()


def _bootstrap_keyring() -> None:
    """Keep the file-loaded key in the ring (single-entry per file key)."""
    global _jwks_file_key_id
    pub = load_public_key()
    kid = _key_id_for_public(pub)
    if _jwks_file_key_id != kid:
        if _jwks_file_key_id is not None:
            _jwt_keyring.pop(_jwks_file_key_id, None)
        _jwks_file_key_id = kid
        _jwt_keyring[kid] = pub


def _jwks_url() -> str:
    url = getattr(gateway.config, "JWT_JWKS_URL", "")
    if url:
        return url
    return (
        f"{gateway.config.AUTH_SERVER_INTERNAL_URL.rstrip('/')}"
        "/.well-known/jwks.json"
    )


def _b64u_int(value: str) -> int:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return int.from_bytes(raw, "big")


def _fetch_jwks_into_keyring() -> None:
    """Refresh the keyring from auth-server. Silent on failure: verification
    of an unknown kid then returns None and retries after the cooldown."""
    try:
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            response = client.get(_jwks_url())
            response.raise_for_status()
            for key in response.json().get("keys", []):
                if key.get("kty") != "RSA" or not key.get("kid"):
                    continue
                _install_jwk(
                    str(key["kid"]),
                    _b64u_int(key["n"]), _b64u_int(key["e"]),
                )
    except Exception:
        pass  # rotation window / auth temporarily down — retry after cooldown


def _key_for_kid(kid: str | None):
    global _jwks_last_fetch
    _bootstrap_keyring()
    if kid is None:
        return load_public_key()  # pre-kid tokens: single file key, as before
    if kid not in _jwt_keyring:
        cooldown = getattr(gateway.config, "JWT_JWKS_REFETCH_SECONDS", 60)
        if time.monotonic() - _jwks_last_fetch >= cooldown:
            _jwks_last_fetch = time.monotonic()
            _fetch_jwks_into_keyring()
    return _jwt_keyring.get(kid)


def verify_jwt(token: str) -> dict | None:
    """Verify JWT signature and expiration. Returns payload or None."""
    try:
        kid = pyjwt.get_unverified_header(token).get("kid")
    except Exception:
        return None
    key = _key_for_kid(kid)
    if key is None:
        return None
    try:
        return pyjwt.decode(token, key, algorithms=["RS256"])
    except Exception:
        return None


def _reset_jwt_keyring_for_tests() -> None:
    global _public_key, _jwks_file_key_id, _jwks_last_fetch
    _jwt_keyring.clear()
    _public_key = None
    _jwks_file_key_id = None
    _jwks_last_fetch = 0.0


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
            # Marks the gateway-wide env fallback so callers (the data
            # agent) can turn upstream credential rejections into an
            # actionable "configure LLM settings" error (improvement 2).
            "legacy_fallback": True,
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


@app.get("/api/stats/latency")
def api_stats_latency(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
):
    """Whole-period latency distribution (avg/p50/p95/p99) for the cards."""
    _require_audit_token(request)
    import gateway.stats as stats
    return stats.latency_summary(start_date, end_date, user)


@app.get("/api/stats/errors")
def api_stats_errors(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
    group_by: str = Query("status", pattern="^(status|model)$"),
):
    """Error rows grouped by status code or model for the breakdown chart."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.errors_breakdown(start_date, end_date, user, group_by)}


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


@app.get("/api/stats/cost")
def api_stats_cost(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
    group_by: str | None = Query(None),
):
    """Estimated USD spend per day, or grouped by user/model/service (P1-4)."""
    _require_audit_token(request)
    import gateway.stats as stats
    return {"items": stats.cost_usage(start_date, end_date, user, group_by)}


class QueryRequest(BaseModel):
    sql: str


@app.post("/api/query")
def api_query(request: Request, body: QueryRequest):
    """Execute a read-only SELECT against the audit database.

    Gated by the same admin token as the audit API. Only whitelisted
    tables/views and a single SELECT are allowed; rows are capped.
    """
    _require_audit_token(request)
    import sqlite3
    try:
        from gateway.query import execute_readonly_query
        result = execute_readonly_query(body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.Error as exc:
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}")
    return result


class AgentAskRequest(BaseModel):
    question: str
    history: list[dict] = []


class AgentChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class AgentConfirmRequest(BaseModel):
    session_id: str
    approved: bool


def _agent_identity(payload: dict) -> tuple[str, str | None]:
    """Human-readable username + stable user id for agent audit records."""
    user = payload.get("username") or payload.get("sub") or "-"
    return str(user), payload.get("sub")


_LLM_CONFIG_HINT = (
    "llm_config_required: 上游拒绝了当前凭据（该用户未配置个人 LLM，"
    "网关全局回退凭据无效）。请在「LLM 设置」中配置上游地址与 API Key。"
)


def _legacy_credential_rejected(routing: dict | None, exc: httpx.HTTPStatusError) -> bool:
    """True when the upstream refused the gateway-wide fallback credentials."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return bool(routing and routing.get("legacy_fallback")) and status in (401, 403)


@app.get("/api/agent/queries")
def api_agent_queries(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
):
    """Paginated history of data-agent interactions (P0-1 audit trail)."""
    _require_audit_token(request)
    from gateway.database import query_agent_queries
    return query_agent_queries({
        "page": page,
        "page_size": page_size,
        "start_date": start_date,
        "end_date": end_date,
        "user": user,
    })


class MetricTermRequest(BaseModel):
    term: str
    definition: str
    synonyms: list[str] = []
    enabled: bool = True


@app.get("/api/agent/metrics")
def api_agent_metrics_list(request: Request):
    """List admin metric terms (audit-token gated)."""
    _require_audit_token(request)
    from gateway.terms import list_metric_terms
    return {"items": list_metric_terms()}


@app.post("/api/agent/metrics")
def api_agent_metrics_upsert(request: Request, body: MetricTermRequest):
    """Create or update one metric term (audit-token gated)."""
    _require_audit_token(request)
    from gateway.terms import upsert_metric_term
    try:
        return upsert_metric_term(
            body.term, body.definition, body.synonyms, enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/agent/metrics/{term_id}")
def api_agent_metrics_delete(request: Request, term_id: int):
    """Delete one metric term (audit-token gated)."""
    _require_audit_token(request)
    from gateway.terms import delete_metric_term
    deleted = delete_metric_term(term_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Metric term not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Data-agent knowledge: QA pairs / column glossary / preset questions
# ---------------------------------------------------------------------------


class QaPairRequest(BaseModel):
    question: str
    sql_template: str
    tags: list[str] = []
    enabled: bool = True


class GlossaryEntryRequest(BaseModel):
    table_name: str
    column_name: str
    business_name: str = ""
    synonyms: list[str] = []
    description: str = ""
    enabled: bool = True


class PresetQuestionRequest(BaseModel):
    question: str
    enabled: bool = True
    sort_order: int = 0


@app.get("/api/agent/qa")
def api_agent_qa_list(request: Request):
    """List QA knowledge pairs (audit-token gated)."""
    _require_audit_token(request)
    from gateway.qa import list_qa_pairs
    return {"items": list_qa_pairs()}


@app.post("/api/agent/qa")
def api_agent_qa_upsert(request: Request, body: QaPairRequest):
    """Create or update one QA pair (audit-token gated)."""
    _require_audit_token(request)
    from gateway.qa import upsert_qa_pair
    try:
        return upsert_qa_pair(
            body.question, body.sql_template, body.tags, enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/agent/qa/{pair_id}")
def api_agent_qa_delete(request: Request, pair_id: int):
    """Delete one QA pair (audit-token gated)."""
    _require_audit_token(request)
    from gateway.qa import delete_qa_pair
    deleted = delete_qa_pair(pair_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="QA pair not found")
    return {"deleted": True}


@app.get("/api/agent/glossary")
def api_agent_glossary_list(request: Request):
    """List column-glossary entries (audit-token gated)."""
    _require_audit_token(request)
    from gateway.glossary import list_glossary_entries
    return {"items": list_glossary_entries()}


@app.post("/api/agent/glossary")
def api_agent_glossary_upsert(request: Request, body: GlossaryEntryRequest):
    """Create or update one column-glossary entry (audit-token gated)."""
    _require_audit_token(request)
    from gateway.glossary import upsert_glossary_entry
    try:
        return upsert_glossary_entry(
            body.table_name, body.column_name, body.business_name,
            body.synonyms, body.description, enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/agent/glossary/{entry_id}")
def api_agent_glossary_delete(request: Request, entry_id: int):
    """Delete one column-glossary entry (audit-token gated)."""
    _require_audit_token(request)
    from gateway.glossary import delete_glossary_entry
    deleted = delete_glossary_entry(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Glossary entry not found")
    return {"deleted": True}


@app.get("/api/agent/presets")
def api_agent_presets_list(request: Request, enabled_only: bool = False):
    """List preset questions (audit-token gated).

    The chat UI passes ``enabled_only=true`` to get the clickable
    shortcuts; the admin UI lists everything including disabled entries.
    """
    _require_audit_token(request)
    from gateway.presets import list_preset_questions
    return {"items": list_preset_questions(enabled_only=enabled_only)}


@app.post("/api/agent/presets")
def api_agent_presets_upsert(request: Request, body: PresetQuestionRequest):
    """Create or update one preset question (audit-token gated)."""
    _require_audit_token(request)
    from gateway.presets import upsert_preset_question
    try:
        return upsert_preset_question(
            body.question, enabled=body.enabled, sort_order=body.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/agent/presets/{preset_id}")
def api_agent_presets_delete(request: Request, preset_id: int):
    """Delete one preset question (audit-token gated)."""
    _require_audit_token(request)
    from gateway.presets import delete_preset_question
    deleted = delete_preset_question(preset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Preset question not found")
    return {"deleted": True}


class AgentCorrectionRequest(BaseModel):
    """Correct a recorded agent query into a Q&A knowledge pair.

    ``sql_template`` empty/omitted promotes the SQL that was recorded with
    the query (e.g. the corrected variant the admin eventually approved).
    """
    sql_template: str = ""
    tags: list[str] = []


@app.post("/api/agent/queries/{query_id}/correction")
def api_agent_query_correction(request: Request, query_id: int, body: AgentCorrectionRequest):
    """One-click correction loop: turn a rejected/edited agent query into an
    enabled Q&A few-shot pair so the same question class generates correctly.

    Upsert semantics (unique on question): re-correcting the same question
    updates the stored SQL. The review page calls this after a rejection or
    an admin-corrected execution.
    """
    _require_audit_token(request)
    from gateway.database import get_agent_query_by_id
    from gateway.qa import upsert_qa_pair

    record = get_agent_query_by_id(query_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent query not found")

    sql = (body.sql_template or "").strip()
    if not sql:
        # A rejected record's stored SQL is known-wrong — promoting it would
        # poison the knowledge base. Rejections require an explicit fix.
        if record["status"] == "rejected":
            raise HTTPException(
                status_code=400,
                detail="Query was rejected; provide corrected sql_template",
            )
        sql = (record.get("sql") or "").strip()
    if not sql:
        raise HTTPException(
            status_code=400,
            detail="No SQL to learn from: provide sql_template",
        )
    tags = list(dict.fromkeys([*(body.tags or []), "correction"]))
    try:
        return upsert_qa_pair(
            record["question"], sql, tags, enabled=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/agent/stats")
def api_agent_stats(
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    user: str | None = Query(None),
):
    """Aggregated data-agent usage stats (audit-token gated)."""
    _require_audit_token(request)
    from gateway.database import agent_query_stats
    return agent_query_stats({
        "start_date": start_date,
        "end_date": end_date,
        "user": user,
    })


# ---------------------------------------------------------------------------
# Anomaly alerts (daily scan → gateway_alerts table)
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
def api_alerts_list(request: Request, include_acked: bool = False):
    """List anomaly alerts (audit-token gated). Open alerts only by default."""
    _require_audit_token(request)
    from gateway.anomaly import list_alerts
    return {"items": list_alerts(include_acked=include_acked)}


@app.post("/api/alerts/scan")
def api_alerts_scan(request: Request):
    """Run the daily anomaly scan now (audit-token gated).

    Normally triggered by the scheduler on startup / periodically; this
    endpoint gives the dashboard a manual refresh.
    """
    _require_audit_token(request)
    from gateway.anomaly import scan_daily
    return {"alerts": scan_daily()}


@app.post("/api/alerts/{alert_id}/ack")
def api_alerts_ack(request: Request, alert_id: int):
    """Acknowledge one alert (audit-token gated)."""
    _require_audit_token(request)
    from gateway.anomaly import ack_alert
    if not ack_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"acked": True}


# ---------------------------------------------------------------------------
# Model prices (dashboard cost estimation) — admin CRUD + default catalog
# ---------------------------------------------------------------------------

class ModelPriceRequest(BaseModel):
    model: str
    prompt: float = 0.0
    completion: float = 0.0
    cache_read: float = 0.0
    cache_creation: float = 0.0


@app.get("/api/prices")
def api_prices_list(request: Request):
    """List admin-configured per-model USD prices (per 1M tokens)."""
    _require_audit_token(request)
    from gateway.prices import list_model_prices
    return {"items": list_model_prices()}


@app.get("/api/prices/catalog")
def api_prices_catalog(request: Request):
    """Public default price catalog with a per-entry `configured` flag."""
    _require_audit_token(request)
    from gateway.prices import catalog_entries
    return {"items": catalog_entries()}


@app.post("/api/prices")
def api_prices_upsert(request: Request, body: ModelPriceRequest):
    """Create or update one model price; takes effect without a restart."""
    _require_audit_token(request)
    from gateway.prices import upsert_model_price
    try:
        return upsert_model_price(
            body.model,
            prompt=body.prompt, completion=body.completion,
            cache_read=body.cache_read, cache_creation=body.cache_creation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/prices/{model}")
def api_prices_delete(request: Request, model: str):
    """Delete one model price (the model then costs $0 again)."""
    _require_audit_token(request)
    from gateway.prices import delete_model_price
    if not delete_model_price(model):
        raise HTTPException(status_code=404, detail="Model price not found")
    return {"deleted": True}


@app.post("/api/agent/ask")
async def api_agent_ask(request: Request, body: AgentAskRequest):
    """Translate a natural-language question to SQL and run it read-only.

    Uses the signed-in user's own LLM config (resolved from the JWT, the
    same way the proxy resolves routing) so the agent speaks to the
    upstream with the user's credentials, not the gateway-wide env key.
    Every interaction is recorded in the agent_queries audit table (P0-1).
    """
    payload = _require_audit_token(request)
    routing, err = await _resolve_user_routing(payload)
    if err is not None:
        return err
    user, user_id = _agent_identity(payload)
    from gateway.agent import ask_agent
    try:
        return await ask_agent(
            body.question, routing=routing, user=user, user_id=user_id,
            history=body.history,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        if _legacy_credential_rejected(routing, exc):
            raise HTTPException(status_code=400, detail=_LLM_CONFIG_HINT)
        raise HTTPException(status_code=502, detail=f"Upstream LLM error: {exc}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream LLM error: {exc}")


@app.post("/api/agent/ask/stream")
async def api_agent_ask_stream(request: Request, body: AgentAskRequest):
    """Stream NL-to-SQL generation and the final read-only query result."""
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question is empty")

    payload = _require_audit_token(request)
    routing, err = await _resolve_user_routing(payload)
    if err is not None:
        return err
    user, user_id = _agent_identity(payload)

    from gateway.agent import stream_agent

    def encode(event: str, data: dict) -> str:
        payload_json = _json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {payload_json}\n\n"

    async def event_source():
        try:
            async for item in stream_agent(
                body.question, routing=routing, user=user, user_id=user_id,
                history=body.history,
            ):
                yield encode(item["event"], item["data"])
        except httpx.HTTPStatusError as exc:
            if _legacy_credential_rejected(routing, exc):
                yield encode("error", {
                    "message": _LLM_CONFIG_HINT,
                    "code": "llm_config_required",
                })
                return
            status = getattr(exc.response, "status_code", None)
            message = (
                f"Upstream LLM returned HTTP {status}"
                if status is not None
                else "Upstream LLM request failed"
            )
            yield encode("error", {"message": message})
        except httpx.HTTPError as exc:
            yield encode("error", {"message": f"Upstream LLM error: {exc}"})
        except ValueError as exc:
            yield encode("error", {"message": str(exc)})
        except Exception:
            yield encode("error", {"message": "Agent stream failed"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Session-based agent chat (agent loop, SSE, human SQL confirmation)
# ---------------------------------------------------------------------------

from gateway.session import SessionStore  # noqa: E402

_CHAT_SESSIONS = SessionStore(ttl_seconds=3600.0, max_sessions=2048)


def _sse_encode(event: str, data: dict) -> str:
    payload_json = _json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload_json}\n\n"


def _sse_response(source) -> StreamingResponse:
    return StreamingResponse(
        source,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _chat_stream_events(question, *, session, routing, user, user_id):
    """Default event source: one agent-loop turn against the upstream LLM."""
    from gateway.agent_loop import run_agent_turn
    async for event in run_agent_turn(
        question, session=session, routing=routing, user=user, user_id=user_id,
    ):
        yield event


async def _confirm_stream_events(session, approved, *, routing, user, user_id):
    """Default event source: resolve a pending SQL confirmation."""
    from gateway.agent_loop import confirm_agent_turn
    async for event in confirm_agent_turn(
        session, approved, routing=routing, user=user, user_id=user_id,
    ):
        yield event


@app.post("/api/agent/chat")
async def api_agent_chat(request: Request, body: AgentChatRequest):
    """Session-based agent chat streamed over SSE.

    The server owns the conversation (unbounded history, compressed in
    two levels when the token budget is exceeded). Generated SQL is
    NEVER executed here — the stream emits ``confirmation_required`` and
    waits for ``/api/agent/chat/confirm`` (human-in-the-loop).
    """
    payload = _require_audit_token(request)
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="Message is empty")
    routing, err = await _resolve_user_routing(payload)
    if err is not None:
        return err
    user, user_id = _agent_identity(payload)

    session = _CHAT_SESSIONS.get(body.session_id) if body.session_id else None
    if session is None:
        session = _CHAT_SESSIONS.create(user_id=user_id)
        session.user = user
    _CHAT_SESSIONS.touch(session.session_id)

    async def event_source():
        try:
            async for item in _chat_stream_events(
                body.message, session=session, routing=routing,
                user=user, user_id=user_id,
            ):
                yield _sse_encode(item["event"], item["data"])
        except httpx.HTTPStatusError as exc:
            if _legacy_credential_rejected(routing, exc):
                yield _sse_encode("error", {
                    "message": _LLM_CONFIG_HINT,
                    "code": "llm_config_required",
                })
                return
            status = getattr(exc.response, "status_code", None)
            message = (
                f"Upstream LLM returned HTTP {status}"
                if status is not None
                else "Upstream LLM request failed"
            )
            yield _sse_encode("error", {"message": message})
        except httpx.HTTPError as exc:
            yield _sse_encode("error", {"message": f"Upstream LLM error: {exc}"})
        except ValueError as exc:
            yield _sse_encode("error", {"message": str(exc)})
        except Exception:
            yield _sse_encode("error", {"message": "Agent stream failed"})

    return _sse_response(event_source())


@app.post("/api/agent/chat/confirm")
async def api_agent_chat_confirm(request: Request, body: AgentConfirmRequest):
    """Approve or reject the SQL awaiting human confirmation (streamed SSE).

    Approved SQL runs read-only; execution failures are reflected back to
    the model (up to 3 retries) and each regenerated SQL requires a fresh
    confirmation.
    """
    payload = _require_audit_token(request)
    session = _CHAT_SESSIONS.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    routing, err = await _resolve_user_routing(payload)
    if err is not None:
        return err
    user, user_id = _agent_identity(payload)
    _CHAT_SESSIONS.touch(session.session_id)

    async def event_source():
        try:
            async for item in _confirm_stream_events(
                session=session, approved=body.approved, routing=routing,
                user=user, user_id=user_id,
            ):
                yield _sse_encode(item["event"], item["data"])
        except httpx.HTTPStatusError as exc:
            if _legacy_credential_rejected(routing, exc):
                yield _sse_encode("error", {
                    "message": _LLM_CONFIG_HINT,
                    "code": "llm_config_required",
                })
                return
            yield _sse_encode("error", {"message": f"Agent error: {exc}"})
        except httpx.HTTPError as exc:
            yield _sse_encode("error", {"message": f"Agent error: {exc}"})
        except ValueError as exc:
            yield _sse_encode("error", {"message": str(exc)})
        except Exception:
            yield _sse_encode("error", {"message": "Agent confirm failed"})

    return _sse_response(event_source())


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
        request.state.audit_start = start
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

        # Streamed (SSE) responses write their own audit log once the
        # stream is drained — usage fields only exist at that point.
        if not any(
            request.url.path.startswith(p) for p in _AUDIT_SKIP_PREFIXES
        ) and not getattr(request.state, "defer_audit_log", False):
            _audit_log_request(request, status_code, duration)


def _quota_response_headers(user: str) -> dict:
    """Advertise the daily token budget/usage so clients can back off early.

    Only emitted when a token budget is configured (0 = unlimited).
    """
    budget = gateway.config.DAILY_TOKEN_BUDGET
    if budget <= 0:
        return {}
    usage = gateway.quota.get_day_usage(user)
    return {
        "X-Gateway-Daily-Token-Budget": str(budget),
        # Canonical total: prompt + completion + cache read + cache creation.
        "X-Gateway-Daily-Tokens-Used": str(
            usage["prompt_tokens"] + usage["completion_tokens"]
            + usage["cache_read_tokens"] + usage["cache_creation_tokens"]
        ),
    }


def _record_quota_usage(request: Request) -> None:
    """Accumulate one proxied request's token usage into the daily budget.

    Called from the audit-log writer (both the normal middleware path and
    the deferred SSE finalisation), so usage is counted exactly once and
    only when the upstream actually reported it.
    """
    user = getattr(request.state, "quota_user", None)
    if not user:
        return
    gateway.quota.record_usage(
        user,
        prompt_tokens=getattr(request.state, "prompt_tokens", None),
        completion_tokens=getattr(request.state, "completion_tokens", None),
        model=getattr(request.state, "model", None),
        cache_read_tokens=getattr(request.state, "cache_read_tokens", None),
        cache_creation_tokens=getattr(
            request.state, "cache_creation_tokens", None,
        ),
    )


def _audit_log_request(
    request: Request, status_code: int, duration: float,
) -> None:
    """Write one audit-log entry from the request's captured state."""
    log_request(
        user=getattr(request.state, "user_sub", "-"),
        method=request.method,
        path=request.url.path,
        status=status_code,
        duration_ms=duration * 1000,
        upstream_ms=getattr(request.state, "upstream_ms", None),
        level="ERROR" if status_code >= 500 else "INFO",
        prompt_tokens=getattr(request.state, "prompt_tokens", None),
        completion_tokens=getattr(request.state, "completion_tokens", None),
        cache_read_tokens=getattr(request.state, "cache_read_tokens", None),
        cache_creation_tokens=getattr(
            request.state, "cache_creation_tokens", None,
        ),
        model=getattr(request.state, "model", None),
        service_name=getattr(request.state, "service_name", None),
        session_id=getattr(request.state, "session_id", None),
        user_id=getattr(request.state, "user_id", None),
        client_id=getattr(request.state, "client_id", None),
        request_body=getattr(request.state, "request_body", None),
        response_body=getattr(request.state, "response_body", None),
    )


def _finalize_stream_audit_log(request: Request) -> None:
    """Write the audit log for an SSE request once the stream is drained.

    At this point the prompt/completion/cache tokens recovered from the
    buffered stream are complete, unlike at middleware exit.  The sync
    SQLite insert adds only tail latency after the final chunk.
    """
    start = getattr(request.state, "audit_start", None)
    duration = (time.perf_counter() - start) if start else 0.0
    status_code = getattr(request.state, "audit_status_code", 200)
    _audit_log_request(request, status_code, duration)


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
    # OAuth client identity (for example flavor-code-cli/flavor-lite-cli).
    request.state.client_id = payload.get("client_id") or None

    # Session identifier: prefer the JWT 'jti' (JWT ID) claim; fall back
    # to 'sid' (session ID) or generate one from sub + current time.
    request.state.session_id = (
        payload.get("jti")
        or payload.get("sid")
        or f"{payload.get('sub', 'unknown')}-{int(time.time())}"
    )

    # --- Per-user quota / budget enforcement (429 on exhaustion) ---
    quota_user = str(payload.get("username") or payload.get("sub") or "-")
    request.state.quota_user = quota_user
    allowed, quota_error = gateway.quota.check(quota_user)
    if not allowed:
        return JSONResponse(status_code=429, content=quota_error)
    quota_headers = _quota_response_headers(quota_user)

    routing, routing_error = await _resolve_user_routing(payload)
    if routing_error is not None:
        return routing_error
    assert routing is not None

    body = await request.body()

    # Capture the request body for audit logging.
    request.state.request_body = _decode_body(body)

    # Extract model name from request body early, before any upstream call.
    # Both OpenAI and Anthropic put it at the top-level "model" key.
    request.state.model = _extract_model(body)
    if not _is_model_allowed(body, routing):
        return JSONResponse(
            status_code=403,
            content={"error": "model_not_allowed", "model": request.state.model},
        )

    # --- Intelligent routing: build the ordered candidate route list ---
    candidates = _candidate_routes(routing)
    # SSRF defense-in-depth (P0-7): validate every candidate target before use,
    # so a routing entry from any source (cache, internal API) cannot target
    # private/metadata addresses. The legacy fallback upstream is
    # operator-configured (UPSTREAM_URL env) and trusted, so it is exempt.
    for candidate in candidates:
        if candidate.get("service_name") != "legacy-upstream":
            if not validate_upstream_url(
                str(candidate.get("upstream_url", "")),
                gateway.config.UPSTREAM_URL_ALLOWLIST,
            ):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "upstream_url not allowed",
                        "detail": "The configured upstream URL is not a public HTTP(S) endpoint",
                    },
                )

    # An explicit client preference is user consent to switch to that route
    # even when the silent-switch compatibility check would reject it.
    preferred = request.headers.get("x-gateway-preferred-route", "")
    primary = routing
    user_id = str(payload.get("sub", ""))

    upstream_start = time.perf_counter()

    # --- Proxy with intelligent routing / failover ---
    #
    # Candidates are tried in order (primary first, then the user-configured
    # fallback route).  Failover happens only *before* any byte is returned
    # to the client: connection errors and 5xx responses advance to the next
    # route; once a response (including an SSE stream) is handed back, no
    # retry is possible.
    #
    # Switch policy (gateway.config.FAILOVER_REQUIRE_CONSENT):
    # - silent failover (default): when the primary route fails the gateway
    #   switches to the backup route transparently even if it is not drop-in
    #   compatible (response merely carries X-Gateway-Route); the client is
    #   never asked to confirm;
    # - consent flow (FAILOVER_REQUIRE_CONSENT=true): a non-compatible backup
    #   triggers a 409 ``route_switched`` error asking the client to confirm
    #   with the user; explicit consent comes back as the
    #   X-Gateway-Preferred-Route header naming the target service.
    for index, route in enumerate(candidates):
        service_name = str(route.get("service_name", ""))
        cooldown_key = (user_id, service_name)
        # Circuit breaker: skip a recently failed route while alternatives
        # exist.  With no alternative left we still attempt it (fail-closed
        # behaviour is unchanged for single-route users).
        if (
            len(candidates) > 1
            and gateway.config.FAILOVER_COOLDOWN_SECONDS > 0
            and _route_cooldowns.get(cooldown_key, 0) > time.monotonic()
        ):
            continue
        # Compatibility gate for every non-primary candidate.  With silent
        # failover (the default) the gate is bypassed: the switch happens
        # without user confirmation even for non drop-in backups.  In consent
        # mode a non-compatible backup returns 409 unless the client already
        # consented via X-Gateway-Preferred-Route.
        if (
            index > 0
            and gateway.config.FAILOVER_REQUIRE_CONSENT
            and not _route_compatible(
                primary, route, request.state.model,
            )
        ):
            if preferred != service_name:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "route_switched",
                        "message": (
                            "The primary route failed and the backup route is "
                            "not drop-in compatible. Ask the user whether to "
                            "continue, then retry with "
                            "X-Gateway-Preferred-Route set to the chosen "
                            "service_name."
                        ),
                        "routes": [
                            {
                                "service_name": item.get("service_name"),
                                "api_type": item.get("api_type"),
                                "models": item.get("models") or [],
                            }
                            for item in candidates
                        ],
                    },
                )

        upstream_url = (
            f"{route['upstream_url'].rstrip('/')}/{path.lstrip('/')}"
        )
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)
        headers.pop("x-gateway-preferred-route", None)
        _apply_upstream_auth(
            headers,
            route.get("upstream_api_key", ""),
            route.get("upstream_auth_type", "x-api-key"),
        )

        request.state.service_name = service_name

        # Failover to a backup profile switches base URL and API key above;
        # the model lives inside the client body, so rewrite it here too:
        # a model the backup cannot serve is replaced by that profile's
        # main model (default_model). Primary requests pass through untouched.
        if index > 0:
            body = _rewrite_body_for_route(body, route, request.state.model)
            request.state.model = _extract_model(body)

        client = httpx.AsyncClient(timeout=300.0)
        took_sse = False
        try:
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
            except httpx.HTTPError:
                # Connection-level failure: eligible for failover.
                UPSTREAM_ERRORS.labels(method=request.method, path=path).inc()
                _mark_route_failed(cooldown_key)
                continue
            except Exception:
                UPSTREAM_ERRORS.labels(
                    method=request.method, path=path,
                ).inc()
                raise

            if resp.status_code >= 500:
                # Upstream signalled an outage: fail over before returning
                # anything to the client.
                UPSTREAM_ERRORS.labels(method=request.method, path=path).inc()
                _mark_route_failed(cooldown_key)
                await resp.aclose()
                continue

            # The route handled the request — lift any cooldown on it.
            _route_cooldowns.pop(cooldown_key, None)

            content_type = resp.headers.get("content-type", "")
            is_sse = "text/event-stream" in content_type

            if is_sse:
                took_sse = True
                # The middleware must not audit-log this request: usage fields
                # are only complete once the stream is drained (sse_generator).
                request.state.defer_audit_log = True
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
                        _record_quota_usage(request)
                        request.state.upstream_ms = (
                            (time.perf_counter() - upstream_start) * 1000
                        )
                        await resp.aclose()
                        await client.aclose()
                        # Usage is final now — write the deferred audit log.
                        _finalize_stream_audit_log(request)

                resp_headers = _clean_response_headers(
                    dict(resp.headers), streaming=True,
                )
                resp_headers["X-Gateway-Route"] = _header_safe_route_name(service_name)
                status_code = resp.status_code
                request.state.audit_status_code = status_code

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
            resp_headers["X-Gateway-Route"] = _header_safe_route_name(service_name)
            status_code = resp.status_code
            request.state.upstream_ms = (
                (time.perf_counter() - upstream_start) * 1000
            )
            _extract_full_token_usage(request, content)
            # Usage is final now — count it toward the daily budget and
            # report the post-request balance to the client.
            _record_quota_usage(request)
            resp_headers.update(_quota_response_headers(quota_user))

            return Response(
                content=content,
                status_code=status_code,
                headers=resp_headers,
            )
        finally:
            if not took_sse:
                await client.aclose()

    # Every candidate route failed (or was cooling down with no alternative).
    return JSONResponse(
        status_code=502,
        content={"error": "Upstream provider unreachable"},
    )


# ---------------------------------------------------------------------------
# Intelligent routing helpers
# ---------------------------------------------------------------------------

def _candidate_routes(routing: dict) -> list[dict]:
    """Ordered failover candidates: the active route, then its fallback."""
    candidates = [routing]
    fallback = routing.get("fallback")
    if isinstance(fallback, dict) and fallback.get("upstream_url"):
        candidates.append(fallback)
    return candidates


def _route_compatible(primary: dict, candidate: dict, model: str | None) -> bool:
    """Silent-switch gate: same protocol and the model must be servable.

    Only consulted in consent mode (FAILOVER_REQUIRE_CONSENT=true): the
    switch is transparent only when the backup route is drop-in compatible
    with the request; otherwise the client is asked.
    """
    if candidate.get("api_type") != primary.get("api_type"):
        return False
    models = candidate.get("models") or []
    if not models:
        return True
    return model is None or model in models


def _rewrite_body_for_route(
    body: bytes, route: dict, model: str | None,
) -> bytes:
    """Replace the body's ``model`` with the route's main model when the
    route cannot serve the requested one.

    Used only for failover (non-primary) candidates, so a backup profile
    with its own base URL / key / models actually receives a model it
    hosts.  The body is returned unchanged when:

    - the route serves the requested model (or lists no models at all);
    - the route has no ``default_model`` (legacy profiles keep the old
      passthrough behaviour);
    - the body is not a JSON object whose ``model`` matches ``model``
      (e.g. non-JSON payloads are never rewritten).
    """
    models = route.get("models") or []
    default_model = str(route.get("default_model") or "")
    if not models or not default_model or model is None or model in models:
        return body
    try:
        data = _json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict) or data.get("model") != model:
        return body
    data["model"] = default_model
    return _json.dumps(data, ensure_ascii=False).encode("utf-8")


def _header_safe_route_name(name: str) -> str:
    """Sanitize a user-controlled service_name for use in HTTP headers."""
    return "".join(
        ch for ch in str(name) if ord(ch) >= 32 and ch not in "\r\n"
    )[:128]


def _mark_route_failed(cooldown_key: tuple[str, str]) -> None:
    """Start the circuit-breaker cooldown for a failed route."""
    seconds = gateway.config.FAILOVER_COOLDOWN_SECONDS
    if seconds > 0:
        now = time.monotonic()
        # Evict expired entries so the map cannot grow without bound on
        # long-lived gateway processes.
        for key in [k for k, deadline in _route_cooldowns.items() if deadline <= now]:
            del _route_cooldowns[key]
        _route_cooldowns[cooldown_key] = now + seconds


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

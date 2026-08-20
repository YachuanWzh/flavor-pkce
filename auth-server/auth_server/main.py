"""PKCE Authorization Server — FastAPI application."""
import uuid
import secrets
import hashlib
import base64
import urllib.parse
from typing import Literal
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
import bcrypt

from auth_server.database import (
    get_db, init_db,
    create_session, get_session, delete_session,
    set_pending_auth, get_pending_auth, clear_pending_auth,
)
from auth_server.ratelimit import (
    record_attempt, is_rate_limited,
    record_failure, is_locked, reset_failures,
)
from auth_server.audit import log_event, purge_old_audit_logs
from auth_server.llm_config import (
    get_llm_config, save_llm_config,
    list_profiles, get_profile, profile_name_exists,
    create_profile, update_profile, delete_profile,
)
import auth_server.config as server_config
from auth_server.config import (
    AUTH_CODE_EXPIRES_IN, JWT_EXPIRES_IN, REFRESH_TOKEN_EXPIRES_IN,
    CORS_ORIGINS, FRONTEND_DIST_PATH,
)
from auth_server.jwt_utils import create_jwt, get_jwt_payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    server_config.validate_production_config()  # P0-8: fail fast on weak defaults
    init_db()
    purge_old_audit_logs()  # P0-10: enforce retention on startup
    yield


app = FastAPI(title="PKCE Authorization Server", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply security headers to every response (P0-6).

    X-Frame-Options: DENY protects the consent/login pages from clickjacking.
    CSP restricts script/style sources; style 'unsafe-inline' is required by
    the served SPA's inline styles.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'",
    )
    if server_config.ENABLE_HSTS:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert FastAPI validation errors to 400 (per OAuth spec)."""
    return JSONResponse(status_code=400, content={"detail": "Invalid request parameters"})

# Sessions and pending-authorization state live in SQLite (see database.py),
# so they survive restarts and work across multiple server processes.

# ---------- Models ----------

class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        import re as _re
        from auth_server.config import PASSWORD_MIN_LENGTH
        if len(value) < PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
            )
        if not _re.search(r"[a-z]", value):
            raise ValueError("Password must contain a lowercase letter")
        if not _re.search(r"[A-Z]", value):
            raise ValueError("Password must contain an uppercase letter")
        if not _re.search(r"[0-9]", value):
            raise ValueError("Password must contain a digit")
        return value


class LlmConfigUpdate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    service_name: str = Field(min_length=1, max_length=128)
    api_type: Literal["openai", "anthropic"]
    upstream_url: HttpUrl
    upstream_api_key: str | None = Field(default=None, max_length=16384)
    clear_api_key: bool = False
    upstream_auth_type: Literal["x-api-key", "bearer", "api-key"]
    default_model: str = Field(min_length=1, max_length=256)
    cheap_model: str = Field(min_length=1, max_length=256)
    models: list[str] = Field(min_length=1, max_length=100)
    max_output_tokens: int = Field(gt=0, le=1_000_000)

    @field_validator("models")
    @classmethod
    def validate_models(cls, models: list[str]) -> list[str]:
        cleaned = [model.strip() for model in models]
        if any(not model or len(model) > 256 for model in cleaned):
            raise ValueError("models must contain non-empty names up to 256 characters")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_selected_models(self):
        if self.default_model not in self.models or self.cheap_model not in self.models:
            raise ValueError("default_model and cheap_model must be included in models")
        return self


# ---------- Auth Helpers ----------

def get_current_user(request: Request) -> dict | None:
    """Get the current user from the persisted session cookie."""
    session_token = request.cookies.get("session_token")
    if not session_token:
        return None
    return get_session(session_token)


def require_current_user(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(request: Request) -> dict:
    user = require_current_user(request)
    db = get_db()
    current = db.execute(
        "SELECT id, username, role FROM users WHERE id = ?", (user["user_id"],),
    ).fetchone()
    db.close()
    if current is None or current["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return dict(current)


def public_llm_config(value: dict) -> dict:
    return {
        key: item for key, item in value.items()
        if key not in {"user_id", "upstream_api_key"}
    }


def owner_llm_config(value: dict) -> dict:
    """Response shape for the owner's own session.

    Unlike admin views of *other* users' configs (``public_llm_config``), the
    owner may read back the decrypted upstream key so the settings page can
    repopulate it when switching between saved profiles. The key is still
    encrypted at rest and never appears in admin rosters or OAuth responses.
    """
    return {key: item for key, item in value.items() if key != "user_id"}


@app.get("/api/me")
def api_me(request: Request):
    user = require_current_user(request)
    db = get_db()
    current = db.execute(
        "SELECT role FROM users WHERE id = ?", (user["user_id"],),
    ).fetchone()
    db.close()
    if current is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return {
        "id": user["user_id"],
        "username": user["username"],
        "role": current["role"],
    }


@app.get("/api/me/llm-config")
def api_get_llm_config(request: Request):
    user = require_current_user(request)
    value = get_llm_config(user["user_id"], include_secret=True)
    if value is None:
        return JSONResponse(status_code=404, content={"detail": "LLM configuration not found"})
    return owner_llm_config(value)


@app.put("/api/me/llm-config")
def api_put_llm_config(request: Request, body: LlmConfigUpdate):
    user = require_current_user(request)
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    saved = save_llm_config(user["user_id"], values, include_secret=True)
    return owner_llm_config(saved)


class FallbackUpdate(BaseModel):
    fallback_profile_id: str | None = Field(default=None, max_length=64)


@app.put("/api/me/llm-config/fallback")
def api_put_llm_fallback(request: Request, body: FallbackUpdate):
    """Attach or clear the gateway failover route.

    This is a routing preference, not a route change: it does not bump
    config_version, so existing JWTs keep working. The gateway consults
    the fallback only when the primary route fails.
    """
    user = require_current_user(request)
    config_row = get_llm_config(user["user_id"])
    if config_row is None:
        raise HTTPException(status_code=404, detail="LLM configuration not found")
    if body.fallback_profile_id is not None:
        profile = get_profile(user["user_id"], body.fallback_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
    db = get_db()
    db.execute(
        "UPDATE user_llm_configs SET fallback_profile_id = ? WHERE user_id = ?",
        (body.fallback_profile_id, user["user_id"]),
    )
    db.commit()
    db.close()
    return owner_llm_config(
        get_llm_config(user["user_id"], include_secret=True) or {},
    )


class LlmProfileCreate(LlmConfigUpdate):
    name: str = Field(min_length=1, max_length=128)


class LlmProfileUpdate(LlmProfileCreate):
    pass


def public_profile(value: dict) -> dict:
    return {
        key: item for key, item in value.items()
        if key not in {"user_id", "upstream_api_key"}
    }


@app.get("/api/me/llm-config-profiles")
def api_list_llm_profiles(request: Request):
    user = require_current_user(request)
    return {
        "profiles": [
            owner_llm_config(p)
            for p in list_profiles(user["user_id"], include_secret=True)
        ],
    }


@app.get("/api/me/llm-config-profiles/{profile_id}")
def api_get_llm_profile(profile_id: str, request: Request):
    user = require_current_user(request)
    profile = get_profile(user["user_id"], profile_id, include_secret=True)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return owner_llm_config(profile)


@app.post("/api/me/llm-config-profiles", status_code=201)
def api_create_llm_profile(request: Request, body: LlmProfileCreate):
    user = require_current_user(request)
    if profile_name_exists(user["user_id"], body.name):
        raise HTTPException(status_code=409, detail="A profile with this name already exists")
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    return owner_llm_config(
        create_profile(user["user_id"], values, include_secret=True),
    )


@app.put("/api/me/llm-config-profiles/{profile_id}")
def api_update_llm_profile(profile_id: str, request: Request, body: LlmProfileUpdate):
    user = require_current_user(request)
    existing = get_profile(user["user_id"], profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if body.name != existing["name"] and profile_name_exists(
        user["user_id"], body.name, exclude_id=profile_id,
    ):
        raise HTTPException(status_code=409, detail="A profile with this name already exists")
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    return owner_llm_config(
        update_profile(user["user_id"], profile_id, values, include_secret=True),
    )


@app.delete("/api/me/llm-config-profiles/{profile_id}")
def api_delete_llm_profile(profile_id: str, request: Request):
    user = require_current_user(request)
    if not delete_profile(user["user_id"], profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": profile_id}


def _activate_profile(user_id: str, profile_id: str) -> dict:
    """Copy a named profile into the user's active routing configuration.

    This bumps config_version (invalidating old JWTs until /login) exactly
    like a manual save, so the fail-closed version gate keeps working.
    Shared by the self-service and administrator activate endpoints.
    """
    profile = get_profile(user_id, profile_id, include_secret=True)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    current = get_llm_config(user_id, include_secret=True)
    api_key = profile.get("upstream_api_key", "")
    if not api_key:
        api_key = (current or {}).get("upstream_api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Cannot activate a profile without an upstream API key",
        )
    saved = save_llm_config(
        user_id,
        {
            "provider_id": profile["provider_id"],
            "service_name": profile["service_name"],
            "api_type": profile["api_type"],
            "upstream_url": profile["upstream_url"],
            "upstream_api_key": api_key,
            "upstream_auth_type": profile["upstream_auth_type"],
            "default_model": profile["default_model"],
            "cheap_model": profile["cheap_model"],
            "models": profile["models"],
            "max_output_tokens": profile["max_output_tokens"],
        },
        active_profile_id=profile_id,
        include_secret=True,
    )
    return owner_llm_config(saved)


@app.post("/api/me/llm-config-profiles/{profile_id}/activate")
def api_activate_llm_profile(profile_id: str, request: Request):
    user = require_current_user(request)
    return _activate_profile(user["user_id"], profile_id)


def _require_existing_user(user_id: str) -> None:
    db = get_db()
    target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")


@app.get("/api/admin/users/{user_id}/llm-config-profiles")
def api_admin_list_profiles(user_id: str, request: Request):
    require_admin(request)
    _require_existing_user(user_id)
    return {
        "profiles": [
            owner_llm_config(p)
            for p in list_profiles(user_id, include_secret=True)
        ],
    }


@app.post("/api/admin/users/{user_id}/llm-config-profiles", status_code=201)
def api_admin_create_profile(user_id: str, request: Request, body: LlmProfileCreate):
    require_admin(request)
    _require_existing_user(user_id)
    if profile_name_exists(user_id, body.name):
        raise HTTPException(status_code=409, detail="A profile with this name already exists")
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    return owner_llm_config(
        create_profile(user_id, values, include_secret=True),
    )


@app.put("/api/admin/users/{user_id}/llm-config-profiles/{profile_id}")
def api_admin_update_profile(
    user_id: str, profile_id: str, request: Request, body: LlmProfileUpdate,
):
    require_admin(request)
    _require_existing_user(user_id)
    existing = get_profile(user_id, profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if body.name != existing["name"] and profile_name_exists(
        user_id, body.name, exclude_id=profile_id,
    ):
        raise HTTPException(status_code=409, detail="A profile with this name already exists")
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    return owner_llm_config(
        update_profile(user_id, profile_id, values, include_secret=True),
    )


@app.delete("/api/admin/users/{user_id}/llm-config-profiles/{profile_id}")
def api_admin_delete_profile(user_id: str, profile_id: str, request: Request):
    require_admin(request)
    _require_existing_user(user_id)
    if not delete_profile(user_id, profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": profile_id}


@app.post("/api/admin/users/{user_id}/llm-config-profiles/{profile_id}/activate")
def api_admin_activate_profile(user_id: str, profile_id: str, request: Request):
    require_admin(request)
    _require_existing_user(user_id)
    return _activate_profile(user_id, profile_id)


@app.put("/api/admin/users/{user_id}/llm-config/fallback")
def api_admin_put_fallback(user_id: str, request: Request, body: FallbackUpdate):
    require_admin(request)
    config_row = get_llm_config(user_id)
    if config_row is None:
        raise HTTPException(status_code=404, detail="LLM configuration not found")
    if body.fallback_profile_id is not None:
        profile = get_profile(user_id, body.fallback_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
    db = get_db()
    db.execute(
        "UPDATE user_llm_configs SET fallback_profile_id = ? WHERE user_id = ?",
        (body.fallback_profile_id, user_id),
    )
    db.commit()
    db.close()
    return owner_llm_config(get_llm_config(user_id, include_secret=True) or {})


@app.get("/api/admin/users")
def api_admin_users(request: Request):
    require_admin(request)
    db = get_db()
    users = db.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY lower(username), id",
    ).fetchall()
    db.close()
    return {
        "users": [
            {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "created_at": user["created_at"],
                # Admins manage real upstream credentials and may need to
                # rotate them, so the roster includes the decrypted key.
                "llm_config": (
                    owner_llm_config(value)
                    if (value := get_llm_config(user["id"], include_secret=True))
                    is not None
                    else None
                ),
            }
            for user in users
        ],
    }


@app.put("/api/admin/users/{user_id}/llm-config")
def api_admin_put_llm_config(user_id: str, request: Request, body: LlmConfigUpdate):
    require_admin(request)
    db = get_db()
    target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    values = body.model_dump()
    values["upstream_url"] = str(body.upstream_url).rstrip("/")
    return owner_llm_config(save_llm_config(user_id, values, include_secret=True))


class RoleUpdate(BaseModel):
    role: Literal["admin", "user"]


@app.put("/api/admin/users/{user_id}/role")
def api_admin_put_role(user_id: str, request: Request, body: RoleUpdate):
    """Change a user's role. Self-demotion and last-admin removal are blocked."""
    actor = require_admin(request)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="self_role_change_forbidden")
    db = get_db()
    target = db.execute(
        "SELECT id, username, role FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    if target is None:
        db.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target["role"] == "admin" and body.role == "user":
        admin_count = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'",
        ).fetchone()["n"]
        if admin_count <= 1:
            db.close()
            raise HTTPException(status_code=400, detail="last_admin_protected")
    old_role = target["role"]
    db.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
    db.commit()
    db.close()
    log_event(
        request,
        "role_changed",
        actor_user_id=actor["id"],
        actor_username=actor["username"],
        detail={
            "target_user_id": user_id,
            "target_username": target["username"],
            "old_role": old_role,
            "new_role": body.role,
        },
    )
    return {"id": user_id, "username": target["username"], "role": body.role}


@app.get("/internal/users/{user_id}/llm-config")
def internal_llm_config(
    user_id: str,
    request: Request,
    version: int,
):
    supplied = request.headers.get("x-internal-service-token", "")
    if not secrets.compare_digest(supplied, server_config.INTERNAL_SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid internal service credential")
    value = get_llm_config(user_id, include_secret=True)
    if value is None:
        raise HTTPException(status_code=404, detail="LLM configuration not found")
    if value["config_version"] != version:
        return JSONResponse(
            status_code=409,
            content={"error": "configuration_changed", "config_version": value["config_version"]},
        )
    # Resolve the gateway failover route (if any). The fallback profile is
    # returned with its decrypted key because the gateway needs it to proxy;
    # this endpoint is internal-token-only and never exposed publicly.
    fallback = None
    if value.get("fallback_profile_id"):
        profile = get_profile(
            user_id, value["fallback_profile_id"], include_secret=True,
        )
        if profile is not None:
            fallback = {
                "service_name": profile["service_name"],
                "api_type": profile["api_type"],
                "upstream_url": profile["upstream_url"],
                "upstream_api_key": profile["upstream_api_key"],
                "upstream_auth_type": profile["upstream_auth_type"],
                "default_model": profile["default_model"],
                "cheap_model": profile["cheap_model"],
                "models": profile["models"],
                "max_output_tokens": profile["max_output_tokens"],
            }
    return {**value, "fallback": fallback}


def validate_redirect_uri(client_redirect_uris: str, redirect_uri: str) -> bool:
    """Check if redirect_uri exactly matches a registered URI (RFC 6749 3.1.2).

    Two registration forms are supported:
    - Exact URI: ``"http://app.example.com/cb"`` matches only itself.
    - Port wildcard: ``"http://127.0.0.1:*"`` matches any port on that exact
      host (used for native-app callbacks on random loopback ports).

    Prefix matching is intentionally gone: a value that merely *starts with* a
    registered string (e.g. ``http://127.0.0.1:9999.evil.com/callback``) is
    rejected, closing a parser-differential open-redirect vector.
    """
    import json
    from urllib.parse import urlsplit

    uris = json.loads(client_redirect_uris)
    try:
        parsed = urlsplit(redirect_uri)
    except ValueError:
        return False
    for registered in uris:
        if registered.endswith(":*"):
            try:
                reg_parsed = urlsplit(registered[:-2])
            except ValueError:
                continue
            if (
                parsed.scheme == reg_parsed.scheme
                and parsed.hostname == reg_parsed.hostname
            ):
                try:
                    port = parsed.port
                except ValueError:
                    # e.g. "http://127.0.0.1:9999.evil.com/callback" — the
                    # netloc contains a non-numeric "port". Reject.
                    continue
                if port is not None:
                    return True
        elif redirect_uri == registered:
            return True
    return False


def generate_auth_code() -> str:
    """Generate a random authorization code."""
    return secrets.token_urlsafe(32)


# ---------- Routes ----------

@app.post("/register", status_code=201)
def register(body: RegisterRequest, request: Request):
    """Register a new user."""
    ip = request.client.host if request.client else "unknown"
    ip_key = f"register:ip:{ip}"
    record_attempt(ip_key, window_seconds=server_config.REGISTER_RATE_WINDOW)
    if is_rate_limited(
        ip_key,
        limit=server_config.REGISTER_RATE_LIMIT,
        window_seconds=server_config.REGISTER_RATE_WINDOW,
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many registration attempts from this address",
        )

    db = get_db()
    cursor = db.cursor()

    existing = cursor.execute(
        "SELECT id FROM users WHERE username = ?", (body.username,)
    ).fetchone()
    if existing:
        db.close()
        raise HTTPException(status_code=409, detail="Username already exists")

    user_id = str(uuid.uuid4())
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()

    cursor.execute(
        "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
        (user_id, body.username, password_hash)
    )
    db.commit()
    db.close()

    # Auto-login after registration: persist session and set cookie
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=3)
    create_session(session_token, user_id, body.username, expires_at.isoformat())

    resp = JSONResponse(
        content={"id": user_id, "username": body.username},
        status_code=201,
    )
    resp.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        max_age=259200,
        samesite="lax",
        secure=server_config.COOKIE_SECURE,  # True in production (HTTPS)
    )
    log_event(request, "register", actor_user_id=user_id, actor_username=body.username)
    return resp


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    return_url: str = Form(""),
):
    """Authenticate a user and return a session token."""
    ip = request.client.host if request.client else "unknown"

    # IP-level rate limit
    ip_key = f"login:ip:{ip}"
    record_attempt(ip_key, window_seconds=server_config.LOGIN_RATE_WINDOW)
    if is_rate_limited(
        ip_key,
        limit=server_config.LOGIN_RATE_LIMIT,
        window_seconds=server_config.LOGIN_RATE_WINDOW,
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts from this address",
        )

    # Account-level lockout after repeated failures
    acct_key = f"login:user:{username}"
    if is_locked(acct_key):
        raise HTTPException(
            status_code=429,
            detail="Account temporarily locked due to repeated failed attempts",
        )

    db = get_db()
    cursor = db.cursor()
    user = cursor.execute(
        "SELECT id, username, password_hash, role FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    db.close()

    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        record_failure(
            acct_key,
            max_failures=server_config.LOGIN_MAX_FAILURES,
            lock_seconds=server_config.LOGIN_LOCK_SECONDS,
        )
        log_event(
            request, "login.failed",
            actor_user_id=user["id"] if user else None,
            actor_username=username,
            detail={"reason": "invalid_credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    reset_failures(acct_key)
    log_event(request, "login.success", actor_user_id=user["id"], actor_username=user["username"])

    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=3)

    create_session(session_token, user["id"], user["username"], expires_at.isoformat())

    # SSO cookie: sign a short-lived JWT (with the user's role) so the same
    # browser can authenticate to the API gateway (same domain in production)
    # without typing a secret. HttpOnly + SameSite=Lax keeps it out of JS.
    role = user["role"] if user["role"] else "user"
    access_token = create_jwt(
        sub=user["id"],
        client_id="flavor-code-cli",
        scope="",
        username=user["username"],
        role=role,
    )

    # If this login was triggered from the authorize flow, redirect back
    if return_url:
        resp = RedirectResponse(url=return_url, status_code=302)
        resp.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            max_age=259200,
            samesite="lax",
            secure=server_config.COOKIE_SECURE,  # True in production (HTTPS)
        )
        resp.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            max_age=259200,
            samesite="lax",
            secure=server_config.COOKIE_SECURE,  # True in production (HTTPS)
        )
        return resp

    resp = JSONResponse(
        content={"session_token": session_token, "username": user["username"]}
    )
    resp.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        max_age=259200,
        samesite="lax",
        secure=server_config.COOKIE_SECURE,  # True in production (HTTPS)
    )
    resp.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=259200,
        samesite="lax",
        secure=server_config.COOKIE_SECURE,  # True in production (HTTPS)
    )
    return resp


# ---------- PKCE Authorization ----------

@app.get("/authorize", response_class=HTMLResponse)
def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
    scope: str = "",
):
    """PKCE /authorize endpoint."""
    # Validate response_type
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be 'code'")

    # Validate code_challenge_method
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="code_challenge_method must be 'S256'")

    # Validate client_id
    db = get_db()
    client = db.execute(
        "SELECT * FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if not client:
        db.close()
        raise HTTPException(status_code=400, detail="invalid_client")

    # Validate redirect_uri
    if not validate_redirect_uri(client["redirect_uris"], redirect_uri):
        db.close()
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    db.close()

    # Check user authentication
    user = get_current_user(request)
    if not user:
        # Store auth params and redirect to login
        return_url = request.url.path + "?" + request.url.query
        encoded_return_url = urllib.parse.quote(return_url, safe="")
        return RedirectResponse(url=f"/login?return_url={encoded_return_url}", status_code=302)

    # User is authenticated — persist PKCE params for /consent
    session_token = request.cookies.get("session_token")
    set_pending_auth(session_token, {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "state": state,
    })

    return HTMLResponse(content=_build_consent_html(
        client_name=client["name"],
        scope=scope,
        username=user["username"],
    ))


@app.get("/consent", response_class=HTMLResponse)
def consent_page(request: Request):
    """Show consent page (fallback HTML)."""
    _ = get_current_user(request)
    return HTMLResponse(content=CONSENT_HTML)


@app.post("/consent")
def consent_confirm(request: Request):
    """User approves authorization — generate code and redirect."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    session_token = request.cookies.get("session_token")
    pending = get_pending_auth(session_token)
    if not pending:
        raise HTTPException(status_code=400, detail="Missing authorization context")

    client_id = pending["client_id"]
    redirect_uri = pending["redirect_uri"]
    code_challenge = pending["code_challenge"]
    scope = pending.get("scope", "")
    state = pending["state"]

    # Generate authorization code
    code = generate_auth_code()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=AUTH_CODE_EXPIRES_IN)

    db = get_db()
    db.execute(
        """INSERT INTO authorization_codes
           (code, client_id, redirect_uri, code_challenge, user_id, scope, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (code, client_id, redirect_uri, code_challenge, user["user_id"], scope,
         expires_at.isoformat())
    )
    db.commit()
    db.close()

    clear_pending_auth(session_token)

    # Build redirect URL
    redirect_params = urllib.parse.urlencode({"code": code, "state": state})
    redirect_url = f"{redirect_uri}?{redirect_params}"

    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/token")
def token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    code_verifier: str = Form(...),
):
    """PKCE /token endpoint — validate code_verifier and issue JWT."""
    ip = request.client.host if request.client else "unknown"
    ip_key = f"token:ip:{ip}"
    record_attempt(ip_key, window_seconds=server_config.TOKEN_RATE_WINDOW)
    if is_rate_limited(
        ip_key,
        limit=server_config.TOKEN_RATE_LIMIT,
        window_seconds=server_config.TOKEN_RATE_WINDOW,
    ):
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "error_description": "Too many token requests"},
        )

    if grant_type != "authorization_code":
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "grant_type must be authorization_code"}
        )

    db = get_db()
    auth_code = db.execute(
        "SELECT * FROM authorization_codes WHERE code = ?", (code,)
    ).fetchone()

    if not auth_code:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Invalid authorization code"}
        )

    # Validate code not already used
    if auth_code["used"]:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Authorization code already used"}
        )

    # Validate code not expired
    expires_at = datetime.fromisoformat(auth_code["expires_at"]).replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Authorization code expired"}
        )

    # Validate client_id matches
    if auth_code["client_id"] != client_id:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "client_id mismatch"}
        )

    # Validate redirect_uri matches
    if auth_code["redirect_uri"] != redirect_uri:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
        )

    # PKCE: verify code_challenge
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    if not _constant_time_compare(expected_challenge, auth_code["code_challenge"]):
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "code_verifier mismatch"}
        )

    llm_config = get_llm_config(auth_code["user_id"])
    if llm_config is None:
        db.close()
        return JSONResponse(
            status_code=400,
            content={
                "error": "llm_config_required",
                "error_description": "Configure your LLM service before signing in.",
            },
        )

    # Mark code as used
    db.execute("UPDATE authorization_codes SET used = 1 WHERE code = ?", (code,))

    # Resolve username + role for the JWT (so the gateway can log it and
    # enforce admin-only routes).
    user_row = db.execute(
        "SELECT username, role FROM users WHERE id = ?", (auth_code["user_id"],)
    ).fetchone()
    username = user_row["username"] if user_row else auth_code["user_id"]
    role = user_row["role"] if user_row else "user"

    # Create JWT
    access_token = create_jwt(
        sub=auth_code["user_id"],
        client_id=client_id,
        scope=auth_code["scope"] or "",
        username=username,
        config_version=llm_config["config_version"],
        role=role,
    )

    # Decode JWT to get jti for token tracking
    payload = get_jwt_payload(access_token)
    jti = payload["jti"] if payload else ""

    # Store access token in DB for revocation support
    token_id = str(uuid.uuid4())
    token_expires = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRES_IN)
    db.execute(
        """INSERT INTO tokens (id, jti, client_id, user_id, scope, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_id, jti, client_id, auth_code["user_id"],
         auth_code["scope"] or "", token_expires.isoformat())
    )

    # Issue a refresh token (opaque, stored as SHA-256) and record it.
    refresh_token = secrets.token_urlsafe(48)
    refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    refresh_expires = datetime.now(timezone.utc) + timedelta(
        seconds=REFRESH_TOKEN_EXPIRES_IN
    )
    db.execute(
        """INSERT INTO tokens (id, jti, client_id, user_id, scope, expires_at, token_type)
           VALUES (?, ?, ?, ?, ?, ?, 'refresh')""",
        (str(uuid.uuid4()), refresh_hash, client_id, auth_code["user_id"],
         auth_code["scope"] or "", refresh_expires.isoformat())
    )
    db.commit()
    db.close()

    log_event(
        request, "token.exchange",
        actor_user_id=auth_code["user_id"],
        actor_username=username,
        detail={"client_id": client_id, "grant_type": "authorization_code"},
    )

    # Clear pending auth from session
    return JSONResponse(content={
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRES_IN,
        "config_version": llm_config["config_version"],
        "llm_config": {
            "provider_id": llm_config["provider_id"],
            "service_name": llm_config["service_name"],
            "api_type": llm_config["api_type"],
            "base_url": server_config.PUBLIC_GATEWAY_URL,
            "default_model": llm_config["default_model"],
            "cheap_model": llm_config["cheap_model"],
            "models": llm_config["models"],
            "max_output_tokens": llm_config["max_output_tokens"],
        },
    })


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _issue_refresh_and_access(
    db, user_id: str, username: str, client_id: str, scope: str,
    role: str = "user",
) -> tuple[str, str]:
    """Issue a fresh (access, refresh) token pair and record both rows.

    Returns ``(access_token, refresh_token)``.
    """
    llm_config = get_llm_config(user_id)
    if llm_config is None:
        raise HTTPException(
            status_code=400,
            detail="Configure your LLM service before signing in.",
        )
    access_token = create_jwt(
        sub=user_id,
        client_id=client_id,
        scope=scope,
        username=username,
        config_version=llm_config["config_version"],
        role=role,
    )
    payload = get_jwt_payload(access_token)
    jti = payload["jti"] if payload else str(uuid.uuid4())
    access_expires = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRES_IN)
    db.execute(
        """INSERT INTO tokens (id, jti, client_id, user_id, scope, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), jti, client_id, user_id, scope, access_expires.isoformat()),
    )
    refresh_token = secrets.token_urlsafe(48)
    refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    refresh_expires = datetime.now(timezone.utc) + timedelta(
        seconds=REFRESH_TOKEN_EXPIRES_IN
    )
    db.execute(
        """INSERT INTO tokens (id, jti, client_id, user_id, scope, expires_at, token_type)
           VALUES (?, ?, ?, ?, ?, ?, 'refresh')""",
        (str(uuid.uuid4()), refresh_hash, client_id, user_id, scope,
         refresh_expires.isoformat()),
    )
    return access_token, refresh_token


@app.post("/refresh")
def refresh(
    request: Request,
    grant_type: str = Form(...),
    refresh_token: str = Form(...),
    client_id: str = Form(...),
):
    """Refresh grant — rotate the refresh token and issue a new access token.

    The old refresh token is consumed on every use (RFC 6749 §6).  Reuse of an
    already-rotated refresh token is rejected.
    """
    ip = request.client.host if request.client else "unknown"
    ip_key = f"token:ip:{ip}"
    record_attempt(ip_key, window_seconds=server_config.TOKEN_RATE_WINDOW)
    if is_rate_limited(
        ip_key,
        limit=server_config.TOKEN_RATE_LIMIT,
        window_seconds=server_config.TOKEN_RATE_WINDOW,
    ):
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "error_description": "Too many token requests"},
        )

    if grant_type != "refresh_token":
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "grant_type must be refresh_token"},
        )

    refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    db = get_db()
    row = db.execute(
        "SELECT * FROM tokens WHERE jti = ? AND token_type = 'refresh'",
        (refresh_hash,),
    ).fetchone()
    if row is None:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Invalid refresh token"},
        )
    if row["revoked"]:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Refresh token revoked"},
        )
    if row["client_id"] != client_id:
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "client_id mismatch"},
        )
    expires_at = datetime.fromisoformat(row["expires_at"]).replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Refresh token expired"},
        )

    # Consume the old refresh token (rotation).
    db.execute("UPDATE tokens SET revoked = 1 WHERE jti = ?", (refresh_hash,))

    user_row = db.execute(
        "SELECT username, role FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()
    username = user_row["username"] if user_row else row["user_id"]
    role = user_row["role"] if user_row else "user"

    try:
        access_token, new_refresh = _issue_refresh_and_access(
            db, row["user_id"], username, client_id, row["scope"] or "",
            role=role,
        )
    except HTTPException:
        db.rollback()
        db.close()
        return JSONResponse(
            status_code=400,
            content={"error": "llm_config_required", "error_description": "Configure your LLM service before signing in."},
        )
    db.commit()
    db.close()

    log_event(
        request, "refresh",
        actor_user_id=row["user_id"],
        actor_username=username,
        detail={"client_id": client_id},
    )

    llm_config = get_llm_config(row["user_id"])
    return JSONResponse(content={
        "access_token": access_token,
        "refresh_token": new_refresh,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRES_IN,
        "config_version": llm_config["config_version"],
        "llm_config": {
            "provider_id": llm_config["provider_id"],
            "service_name": llm_config["service_name"],
            "api_type": llm_config["api_type"],
            "base_url": server_config.PUBLIC_GATEWAY_URL,
            "default_model": llm_config["default_model"],
            "cheap_model": llm_config["cheap_model"],
            "models": llm_config["models"],
            "max_output_tokens": llm_config["max_output_tokens"],
        },
    })


@app.post("/revoke")
def revoke(
    request: Request,
    token: str = Form(...),
    token_type_hint: str = Form(""),
    client_id: str = Form(...),
):
    """RFC 7009 token revocation.

    Accepts either an opaque refresh token or a JWT access token.  Always
    returns 200 for invalid/unknown tokens to prevent token scanning.
    """
    candidates: list[str] = []
    payload = get_jwt_payload(token)
    jti = payload.get("jti") if payload else None
    if token_type_hint == "refresh_token":
        candidates.append(hashlib.sha256(token.encode()).hexdigest())
    elif token_type_hint == "access_token":
        if jti:
            candidates.append(jti)
    else:
        if jti:
            candidates.append(jti)
        candidates.append(hashlib.sha256(token.encode()).hexdigest())

    db = get_db()
    for candidate in candidates:
        db.execute("UPDATE tokens SET revoked = 1 WHERE jti = ?", (candidate,))
    db.commit()
    db.close()
    log_event(
        request, "revoke",
        detail={"client_id": client_id, "token_type_hint": token_type_hint},
    )
    return JSONResponse(content={})


@app.get("/internal/tokens/revoked")
def internal_tokens_revoked(request: Request, jti: str):
    """Internal endpoint for the gateway: is this access-token jti revoked?"""
    supplied = request.headers.get("x-internal-service-token", "")
    if not secrets.compare_digest(supplied, server_config.INTERNAL_SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid internal service credential")
    db = get_db()
    row = db.execute(
        "SELECT revoked FROM tokens WHERE jti = ? AND token_type = 'access'", (jti,),
    ).fetchone()
    db.close()
    return {"revoked": bool(row and row["revoked"])}


def _build_consent_html(client_name: str, scope: str, username: str) -> str:
    """Build the consent page HTML."""
    return CONSENT_HTML.format(
        client_name=client_name,
        scope=scope or "No scopes requested",
        username=username,
    )


# ---------- React SPA Serving ----------

import os
from pathlib import Path as _Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_FRONTEND_DIST = _Path(FRONTEND_DIST_PATH)

# Mount static assets (JS, CSS, images) from frontend build
if _FRONTEND_DIST.exists():
    _assets = _FRONTEND_DIST / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Catch-all: serve React SPA for any unmatched GET route."""
    index = _FRONTEND_DIST / "index.html"
    if not index.exists():
        return HTMLResponse(
            content="<h2 style='text-align:center;margin-top:80px;font-family:sans-serif'>"
                    "Frontend not built.<br>Run: <code>cd frontend &amp;&amp; npm run build</code></h2>"
        )
    return FileResponse(str(index))


# ---------- Consent Template (server-rendered) ----------

CONSENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Authorize — PKCE Auth Server</title>
    <style>
        *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
        body{{
            font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            background:#0a0a1a;
            color:#e8ecf4;
            min-height:100vh;
            padding:40px 24px
        }}
        body::before{{
            content:'';
            position:fixed;
            inset:0;
            z-index:-1;
            background:
                radial-gradient(ellipse 80% 60% at 20% 30%, rgba(99,102,241,0.13) 0%, transparent 60%),
                radial-gradient(ellipse 70% 50% at 80% 70%, rgba(139,92,246,0.1) 0%, transparent 60%)
        }}
        .wrapper{{max-width:540px;margin:0 auto}}
        h1{{font-size:24px;font-weight:700;letter-spacing:-0.4px;text-align:center;margin-bottom:28px}}
        .card{{
            background:rgba(22,24,38,0.82);
            backdrop-filter:blur(28px) saturate(180%);
            -webkit-backdrop-filter:blur(28px) saturate(180%);
            border:1px solid rgba(255,255,255,0.07);
            border-radius:18px;
            padding:30px;
            margin-bottom:24px;
            box-shadow:0 8px 32px rgba(0,0,0,0.25)
        }}
        .card p{{font-size:15px;color:#cbd5e1;margin-bottom:22px;line-height:1.6}}
        .field{{margin-bottom:16px;padding:10px 14px;background:rgba(255,255,255,0.025);border-radius:8px}}
        .field .label{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:#64748b;margin-bottom:5px}}
        .field .value{{font-size:14px;color:#f1f5f9;word-break:break-all;font-family:monospace}}
        .actions{{display:flex;gap:14px}}
        .btn{{
            flex:1;padding:15px;border-radius:12px;font-size:15px;font-weight:600;
            cursor:pointer;border:none;transition:all .3s;letter-spacing:.3px
        }}
        .btn-approve{{
            background:linear-gradient(135deg,#6366f1,#8b5cf6);
            color:white;
            box-shadow:0 4px 20px rgba(99,102,241,0.4)
        }}
        .btn-approve:hover{{transform:translateY(-2px);box-shadow:0 8px 32px rgba(99,102,241,0.5)}}
        .btn-deny{{
            background:rgba(255,255,255,0.05);
            color:#94a3b8;
            border:1px solid rgba(255,255,255,0.1)
        }}
        .btn-deny:hover{{background:rgba(255,255,255,0.1);color:#e2e8f0}}
    </style>
</head>
<body>
    <div class="wrapper">
        <h1>Authorization Request</h1>
        <div class="card">
            <p><strong style="color:#e2e8f0">{client_name}</strong> is requesting access to your account.</p>
            <div class="field">
                <div class="label">Signed in as</div>
                <div class="value">{username}</div>
            </div>
            <div class="field">
                <div class="label">Scopes</div>
                <div class="value">{scope}</div>
            </div>
        </div>
        <form class="actions" method="post" action="/consent">
            <button type="submit" class="btn btn-approve">Approve</button>
            <button type="button" class="btn btn-deny" onclick="history.back()">Deny</button>
        </form>
    </div>
</body>
</html>"""

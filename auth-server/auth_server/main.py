"""PKCE Authorization Server — FastAPI application."""
import uuid
import secrets
import hashlib
import base64
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import bcrypt

from auth_server.database import get_db, init_db
from auth_server.config import AUTH_CODE_EXPIRES_IN, JWT_EXPIRES_IN
from auth_server.jwt_utils import create_jwt, get_jwt_payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="PKCE Authorization Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert FastAPI validation errors to 400 (per OAuth spec)."""
    return JSONResponse(status_code=400, content={"detail": "Invalid request parameters"})

# In-memory session store (for demo purposes; use Redis in production)
_sessions: dict[str, dict] = {}

# ---------- Models ----------

class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


# ---------- Auth Helpers ----------

def get_current_user(request: Request) -> dict | None:
    """Get the current user from session cookie."""
    session_token = request.cookies.get("session_token")
    if not session_token or session_token not in _sessions:
        return None
    session = _sessions[session_token]
    exp = datetime.fromisoformat(session["expires_at"])
    if exp < datetime.now(timezone.utc):
        del _sessions[session_token]
        return None
    return session


def validate_redirect_uri(client_redirect_uris: str, redirect_uri: str) -> bool:
    """Check if redirect_uri matches any registered URI prefix."""
    import json
    uris = json.loads(client_redirect_uris)
    for registered in uris:
        if redirect_uri.startswith(registered):
            return True
    return False


def generate_auth_code() -> str:
    """Generate a random authorization code."""
    return secrets.token_urlsafe(32)


# ---------- Routes ----------

@app.post("/register", status_code=201)
def register(body: RegisterRequest):
    """Register a new user."""
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

    return {"id": user_id, "username": body.username}


@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    return_url: str = Form(""),
):
    """Authenticate a user and return a session token."""

    db = get_db()
    cursor = db.cursor()
    user = cursor.execute(
        "SELECT id, username, password_hash FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    db.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=3)

    _sessions[session_token] = {
        "user_id": user["id"],
        "username": user["username"],
        "expires_at": expires_at.isoformat(),
    }

    # If this login was triggered from the authorize flow, redirect back
    if return_url:
        resp = RedirectResponse(url=return_url, status_code=302)
        resp.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            max_age=259200,
            samesite="lax",
            secure=False,  # Set True in production with HTTPS
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
        secure=False,  # Set True in production with HTTPS
    )
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Return a simple login page. (placeholder for React frontend)."""
    return_url = request.query_params.get("return_url", "")
    html = LOGIN_HTML.replace("__RETURN_URL__", return_url)
    return HTMLResponse(content=html)


# ---------- PKCE Authorization ----------

# Store pending authorization params in server-side session memory
_pending_auth: dict[str, dict] = {}


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

    # User is authenticated — show consent page
    # Store PKCE params in session for /consent
    session_token = request.cookies.get("session_token")
    _sessions[session_token]["pending_auth"] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "state": state,
    }

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
    pending = _sessions.get(session_token, {}).get("pending_auth")
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

    # Build redirect URL
    redirect_params = urllib.parse.urlencode({"code": code, "state": state})
    redirect_url = f"{redirect_uri}?{redirect_params}"

    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    code_verifier: str = Form(...),
):
    """PKCE /token endpoint — validate code_verifier and issue JWT."""
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

    # Mark code as used
    db.execute("UPDATE authorization_codes SET used = 1 WHERE code = ?", (code,))

    # Create JWT
    access_token = create_jwt(
        sub=auth_code["user_id"],
        client_id=client_id,
        scope=auth_code["scope"] or "",
    )

    # Decode JWT to get jti for token tracking
    payload = get_jwt_payload(access_token)
    jti = payload["jti"] if payload else ""

    # Store token in DB for revocation support
    token_id = str(uuid.uuid4())
    token_expires = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRES_IN)
    db.execute(
        """INSERT INTO tokens (id, jti, client_id, user_id, scope, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_id, jti, client_id, auth_code["user_id"],
         auth_code["scope"] or "", token_expires.isoformat())
    )
    db.commit()
    db.close()

    # Clear pending auth from session
    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRES_IN,
    })


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _build_consent_html(client_name: str, scope: str, username: str) -> str:
    """Build the consent page HTML."""
    return CONSENT_HTML.format(
        client_name=client_name,
        scope=scope or "No scopes requested",
        username=username,
    )


# ---------- HTML Templates (Fallback) ----------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login — PKCE Auth Server</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 400px; margin: 80px auto; padding: 20px; }}
        h1 {{ text-align: center; color: #333; }}
        form {{ display: flex; flex-direction: column; gap: 12px; }}
        input {{ padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }}
        button {{ padding: 12px; background: #4f46e5; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
        button:hover {{ background: #4338ca; }}
        .error {{ color: #dc2626; font-size: 14px; }}
    </style>
</head>
<body>
    <h1>PKCE Authorization Server</h1>
    <form id="login-form" method="post" action="/login">
        <input type="hidden" name="return_url" value="__RETURN_URL__">
        <input type="text" name="username" placeholder="Username" required>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Sign In</button>
        <div class="error" id="error"></div>
    </form>
</body>
</html>"""

CONSENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Authorize — PKCE Auth Server</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 480px; margin: 80px auto; padding: 20px; }}
        h1 {{ color: #333; }}
        .card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 24px; margin-top: 16px; }}
        .field {{ margin-bottom: 12px; }}
        .label {{ font-size: 12px; color: #6b7280; text-transform: uppercase; }}
        .value {{ font-size: 14px; color: #111; word-break: break-all; }}
        .actions {{ display: flex; gap: 12px; margin-top: 20px; }}
        .btn-approve {{ padding: 12px 24px; background: #4f46e5; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
        .btn-approve:hover {{ background: #4338ca; }}
        .btn-deny {{ padding: 12px 24px; background: #e5e7eb; color: #374151; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
    </style>
</head>
<body>
    <h1>Authorization Request</h1>
    <div class="card">
        <p><strong>{client_name}</strong> is requesting access to your account.</p>
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
        <button type="submit" class="btn-approve">Approve</button>
        <button type="button" class="btn-deny" onclick="history.back()">Deny</button>
    </form>
</body>
</html>"""

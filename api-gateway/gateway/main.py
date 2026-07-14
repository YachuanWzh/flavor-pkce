"""API Gateway — JWT verification + transparent proxy to upstream LLM API."""
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import gateway.config

app = FastAPI(title="PKCE API Gateway")

_public_key = None


def load_public_key():
    """Load the RSA public key. Cached after first load."""
    global _public_key
    if _public_key is not None:
        return _public_key
    with open(gateway.config.JWT_PUBLIC_KEY_PATH, "rb") as f:
        _public_key = serialization.load_pem_public_key(
            f.read(),
            backend=default_backend(),
        )
    return _public_key


def verify_jwt(token: str) -> dict | None:
    """Verify JWT signature and expiration. Returns payload or None."""
    try:
        public_key = load_public_key()
        return pyjwt.decode(token, public_key, algorithms=["RS256"])
    except Exception:
        return None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(request: Request, path: str):
    """Transparent proxy with JWT verification."""
    # Verify JWT
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": "Missing or invalid Authorization header"}
        )

    token = auth_header[7:]
    payload = verify_jwt(token)
    if payload is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or expired JWT"}
        )

    # Build upstream request
    upstream_url = f"{gateway.config.UPSTREAM_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    headers = dict(request.headers)
    # Remove hop-by-hop headers
    headers.pop("host", None)
    headers.pop("content-length", None)
    # Replace auth with real API key
    if gateway.config.UPSTREAM_API_KEY:
        headers["authorization"] = f"Bearer {gateway.config.UPSTREAM_API_KEY}"

    body = await request.body()

    async with httpx.AsyncClient(timeout=120.0) as client:
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )

    # Build response
    proxy_headers = dict(upstream_resp.headers)
    proxy_headers.pop("transfer-encoding", None)
    proxy_headers.pop("content-encoding", None)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=proxy_headers,
    )


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}

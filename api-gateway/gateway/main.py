"""API Gateway — JWT verification + transparent proxy to upstream LLM API.

Supports both regular (non-streaming) and SSE streaming responses.
"""
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
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


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(request: Request, path: str):
    """Transparent proxy with JWT verification. Supports SSE streaming."""
    # Verify JWT
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": "Missing or invalid Authorization header"},
        )

    token = auth_header[7:]
    payload = verify_jwt(token)
    if payload is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or expired JWT"},
        )

    # Build upstream URL
    upstream_url = (
        f"{gateway.config.UPSTREAM_URL.rstrip('/')}/{path.lstrip('/')}"
    )
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Prepare upstream headers
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    if gateway.config.UPSTREAM_API_KEY:
        headers["authorization"] = f"Bearer {gateway.config.UPSTREAM_API_KEY}"

    body = await request.body()

    # Use a streaming client so we can forward SSE chunks in real time.
    # Long timeout: LLM streaming can take minutes for large responses.
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        ) as upstream_resp:
            content_type = upstream_resp.headers.get("content-type", "")

            # SSE streaming response — forward chunks as they arrive
            if "text/event-stream" in content_type:
                async def sse_generator():
                    async for chunk in upstream_resp.aiter_bytes():
                        yield chunk

                proxy_headers = _clean_response_headers(
                    dict(upstream_resp.headers),
                    streaming=True,
                )
                return StreamingResponse(
                    sse_generator(),
                    status_code=upstream_resp.status_code,
                    headers=proxy_headers,
                )

            # Non-streaming response — read fully and return
            content = await upstream_resp.aread()
            proxy_headers = _clean_response_headers(
                dict(upstream_resp.headers),
                streaming=False,
            )
            return Response(
                content=content,
                status_code=upstream_resp.status_code,
                headers=proxy_headers,
            )


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}


def _clean_response_headers(
    headers: dict, *, streaming: bool = False
) -> dict:
    """Strip hop-by-hop headers from the upstream response.

    For streaming responses, also remove content-length since the body
    is chunked (Transfer-Encoding: chunked).
    """
    headers.pop("transfer-encoding", None)
    headers.pop("content-encoding", None)
    if streaming:
        headers.pop("content-length", None)
    return headers

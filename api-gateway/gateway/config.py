"""API Gateway configuration.

Priority: environment variables > .env file > defaults.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent

# Load .env from the gateway root directory (api-gateway/.env)
load_dotenv(BASE_DIR / ".env")

# JWT public key path (shared with auth server)
JWT_PUBLIC_KEY_PATH = os.environ.get(
    "JWT_PUBLIC_KEY_PATH",
    "/app/keys/public.pem",
)

# Upstream LLM provider configuration
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.openai.com")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")
UPSTREAM_AUTH_TYPE = os.environ.get("UPSTREAM_AUTH_TYPE", "x-api-key").lower()

# Per-user routing lookup. Tokens without config_version retain the legacy
# global route above during migration.
AUTH_SERVER_INTERNAL_URL = os.environ.get(
    "AUTH_SERVER_INTERNAL_URL", "http://127.0.0.1:8091",
)
INTERNAL_SERVICE_TOKEN = os.environ.get(
    "INTERNAL_SERVICE_TOKEN", "dev-internal-token-change-me",
)
ROUTING_CACHE_TTL_SECONDS = int(os.environ.get("ROUTING_CACHE_TTL_SECONDS", "0"))

# How long (seconds) to cache an auth-server revocation verdict per jti.
# 0 disables caching (every request checks with the auth server).
REVOCATION_CACHE_TTL_SECONDS = int(os.environ.get("REVOCATION_CACHE_TTL_SECONDS", "5"))

# Bearer token required for the audit-log API (P0-1). When empty, all
# /api/logs* endpoints refuse access (fail-closed for audit data).
AUDIT_API_TOKEN = os.environ.get("AUDIT_API_TOKEN", "")

# Gateway settings
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8092"))

# Audit log database (SQLite)
AUDIT_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    str(BASE_DIR / "data" / "audit.db"),
)

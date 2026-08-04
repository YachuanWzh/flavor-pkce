"""Auth Server configuration."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = os.environ.get("AUTH_DB_PATH", str(BASE_DIR / "auth.db"))
SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "dev-secret-change-in-production")
JWT_PRIVATE_KEY_PATH = os.environ.get("JWT_PRIVATE_KEY_PATH", str(BASE_DIR / "keys" / "private.pem"))
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", str(BASE_DIR / "keys" / "public.pem"))
JWT_ALGORITHM = "RS256"
JWT_EXPIRES_IN = 259200  # 3 days
AUTH_CODE_EXPIRES_IN = 600  # 10 minutes

# Server bind settings
AUTH_HOST = os.environ.get("AUTH_HOST", "0.0.0.0")
AUTH_PORT = int(os.environ.get("AUTH_PORT", "8091"))

# Public gateway metadata and private gateway-to-auth credential.
PUBLIC_GATEWAY_URL = os.environ.get("PUBLIC_GATEWAY_URL", "http://127.0.0.1:8092")
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "dev-internal-token-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
LLM_CONFIG_ENCRYPTION_KEY_PATH = os.environ.get(
    "LLM_CONFIG_ENCRYPTION_KEY_PATH",
    str(BASE_DIR / "keys" / "llm-config.key"),
)

# Migration template used only to seed the built-in testuser when it has no
# personal configuration yet. New users must configure their own service.
DEFAULT_LLM_PROVIDER_ID = os.environ.get("DEFAULT_LLM_PROVIDER_ID", "deepseek")
DEFAULT_LLM_SERVICE_NAME = os.environ.get("DEFAULT_LLM_SERVICE_NAME", "DeepSeek")
DEFAULT_LLM_API_TYPE = os.environ.get("DEFAULT_LLM_API_TYPE", "anthropic")
DEFAULT_LLM_UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.deepseek.com/anthropic")
DEFAULT_LLM_UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")
DEFAULT_LLM_UPSTREAM_AUTH_TYPE = os.environ.get("UPSTREAM_AUTH_TYPE", "x-api-key")
DEFAULT_LLM_MODEL = os.environ.get("DEFAULT_LLM_MODEL", "deepseek-v4-pro")
DEFAULT_LLM_CHEAP_MODEL = os.environ.get("DEFAULT_LLM_CHEAP_MODEL", "deepseek-v4-flash")
DEFAULT_LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("DEFAULT_LLM_MAX_OUTPUT_TOKENS", "65536"))

# CORS origins — comma-separated env var, or sensible defaults
_cors_raw = os.environ.get("CORS_ORIGINS", "")
if _cors_raw:
    CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]
else:
    CORS_ORIGINS = [
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:8091",
        "http://127.0.0.1:8091",
    ]

# Frontend SPA dist path (overridden in Docker to /app/frontend/dist)
FRONTEND_DIST_PATH = os.environ.get(
    "FRONTEND_DIST_PATH",
    str(BASE_DIR.parent / "frontend" / "dist"),
)

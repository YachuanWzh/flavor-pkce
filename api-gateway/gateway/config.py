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
    str(BASE_DIR.parent / "auth-server" / "auth_server" / "keys" / "public.pem"),
)

# Upstream LLM provider configuration
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.openai.com")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

# Gateway settings
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8092"))

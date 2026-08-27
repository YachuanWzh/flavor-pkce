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
# Model name sent by the data agent when it calls the upstream LLM.
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "default")

# Operator-approved upstream hosts (hostnames or literal IPs, comma-separated)
# that bypass the outbound SSRF check (P0-7). Use for on-prem/private LLM
# endpoints, or environments whose DNS resolves to reserved ranges such as a
# proxy fake-ip range (198.18.0.0/15).
UPSTREAM_URL_ALLOWLIST = {
    host.strip().lower()
    for host in os.environ.get("UPSTREAM_URL_ALLOWLIST", "").split(",")
    if host.strip()
}

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

# Intelligent routing / failover: after a candidate route fails, skip it for
# this many seconds (circuit-breaker cooldown). 0 disables cooldowns.
FAILOVER_COOLDOWN_SECONDS = int(os.environ.get("FAILOVER_COOLDOWN_SECONDS", "30"))

# Per-user quota enforcement on the proxy (0 = limit disabled).
# Requests per user per fixed one-minute window.
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "0"))
# prompt+completion tokens per user per UTC day.
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "0"))
# Estimated USD spend per user per UTC day (priced via MODEL_PRICES_JSON).
DAILY_COST_BUDGET_USD = float(os.environ.get("DAILY_COST_BUDGET_USD", "0"))

# Audit data governance (mirrors auth-server's AUDIT_RETENTION_DAYS).
# Days to keep gateway audit rows (audit_logs/agent_queries/quota_usage);
# 0 disables the startup purge.
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "180"))
# Max characters stored per request/response body column in audit_logs;
# 0 disables storing bodies entirely.
AUDIT_BODY_MAX_CHARS = int(os.environ.get("AUDIT_BODY_MAX_CHARS", "50000"))

# Anomaly scan cadence (seconds) after the initial startup scan.
# 0 disables the periodic rescanner (startup scan and manual /api/alerts/scan
# still work).
ALERT_SCAN_INTERVAL_SECONDS = int(
    os.environ.get("ALERT_SCAN_INTERVAL_SECONDS", "86400")
)

# Bearer token required for the audit-log API (P0-1). When empty, all
# /api/logs* endpoints refuse access (fail-closed for audit data).
AUDIT_API_TOKEN = os.environ.get("AUDIT_API_TOKEN", "")

# Per-model USD prices per 1M tokens used by /api/stats/cost (P1-4).
# JSON shape: {"model-name": {"prompt": 3.0, "completion": 15.0,
#                              "cache_read": 0.3, "cache_creation": 3.0}}
# Any field may be omitted (treated as 0). Models without an entry cost $0.
MODEL_PRICES: dict = {}
_raw_prices = os.environ.get("MODEL_PRICES_JSON", "")
if _raw_prices.strip():
    try:
        import json as _json
        parsed = _json.loads(_raw_prices)
        if isinstance(parsed, dict):
            MODEL_PRICES = parsed
    except ValueError:
        pass  # malformed config → prices disabled (all costs zero)

# Gateway settings
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8092"))

# Audit log database (SQLite)
AUDIT_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    str(BASE_DIR / "data" / "audit.db"),
)

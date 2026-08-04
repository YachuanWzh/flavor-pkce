"""Prometheus metrics for the API Gateway.

Every metric exposed by the ``/metrics`` endpoint is defined here so
the proxy and middleware modules can import and update them directly.
"""

from prometheus_client import Counter, Gauge, Histogram

# ---- Request-level metrics ----

REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Total number of requests processed by the gateway.",
    ["method", "path", "status_code"],
)

REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "End-to-end request duration (gateway ingress → egress).",
    ["method", "path"],
    buckets=[
        0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
        10.0, 30.0, 60.0, 120.0,
    ],
)

# ---- Upstream / error metrics ----

UPSTREAM_ERRORS = Counter(
    "gateway_upstream_errors_total",
    "Number of upstream provider failures (connection, timeout, 5xx).",
    ["method", "path"],
)

# ---- Connection gauge ----

ACTIVE_CONNECTIONS = Gauge(
    "gateway_active_connections",
    "Number of in-flight requests currently being processed.",
)

# ---- Helpers ----

def normalize_path(path: str) -> str:
    """Reduce a request path to its first two segments for metric labels.

    Keeps label cardinality bounded.  Examples::

        /v1/chat/completions  →  /v1/chat
        /v1/models            →  /v1/models
        /health               →  /health
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    if len(parts) <= 2:
        return "/" + "/".join(parts)
    return "/" + "/".join(parts[:2])

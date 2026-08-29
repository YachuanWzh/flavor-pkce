#!/usr/bin/env python3
"""Live NL2SQL evaluation harness (improvement 4, out-of-band mode).

Replays golden questions against a running gateway's ``/api/agent/ask``
endpoint and reports, per case, whether the pipeline produced an
acceptable SQL. Unlike tests/test_agent_golden.py (which pins the
protocol with an injected LLM), this exercises the *real* upstream model
after prompt or knowledge changes. Selection is manual, never CI.

Usage:
    python scripts/agent_eval.py \
        --gateway-url http://127.0.0.1:8092 \
        --token "$AUDIT_API_TOKEN" \
        --cases scripts/agent_eval_cases.json

Exit code = number of failed cases (0 = all good).
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def ask_one(gateway_url: str, token: str, question: str, timeout: float):
    req = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/api/agent/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json", "X-Audit-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {"detail": str(exc)}
        return exc.code, body
    except Exception as exc:  # network / decode errors
        return 0, {"detail": str(exc)}


def evaluate_case(case: dict, status: int, body: dict):
    """Return (ok, note)."""
    sql = (body.get("sql") or "").upper()
    if case.get("expect_blocked"):
        # A write attempt must never come back as an executable result.
        bad = status >= 500 or "DELETE" in sql or "DROP" in sql or "UPDATE" in sql
        return (not bad, "write refused" if not bad else f"leaked: status={status}")
    if status != 200:
        return False, f"HTTP {status}: {str(body)[:80]}"
    missing = [s for s in case.get("expect_sql_contains", [])
               if s.upper() not in sql]
    if missing:
        return False, f"SQL missing {missing}: {sql[:80]}"
    return True, "ok"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8092")
    parser.add_argument("--token", required=True,
                        help="X-Audit-Token (admin scope)")
    parser.add_argument("--cases", default="scripts/agent_eval_cases.json")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    failures = 0
    for case in cases:
        status, body = ask_one(
            args.gateway_url, args.token, case["question"], args.timeout,
        )
        ok, note = evaluate_case(case, status, body)
        failures += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case['id']:16} {note}")
    print(f"\n{len(cases) - failures}/{len(cases)} cases passed")
    return failures


if __name__ == "__main__":
    sys.exit(main())

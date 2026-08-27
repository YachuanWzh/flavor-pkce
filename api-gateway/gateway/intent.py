"""Intent-slot parsing for the data agent.

The agent must answer with a strict JSON object containing at least an
``intent`` field::

    {"intent": "query_data", "sql": "SELECT ..."}
    {"intent": "chitchat", "message": "..."}
    {"intent": "clarify", "message": "..."}

Models frequently wrap the JSON in code fences or surround it with
prose, so parsing is tolerant — validation is strict.
"""

import json
import re

VALID_INTENTS = frozenset({"query_data", "chitchat", "clarify"})

CHART_TYPES = frozenset({"bar", "line", "pie"})

_FENCED_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _find_matching_brace(text: str, start: int) -> int:
    """Return the index of the '}' closing the '{' at ``start``, or -1.

    Handles braces inside string literals and backslash escapes.
    """
    depth = 0
    in_string = False
    escaped = False
    for j in range(start, len(text)):
        ch = text[j]
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _candidate_objects(text: str):
    """Yield brace-balanced substrings starting at each '{'."""
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        end = _find_matching_brace(text, i)
        if end == -1:
            # Unbalanced braces — nothing more to parse.
            return
        yield text[i : end + 1]
        i = end + 1


def _validate_chart(value) -> dict | None:
    """Accept only dicts with a supported type and string x/series slots."""
    if not isinstance(value, dict):
        return None
    ctype = value.get("type")
    if ctype not in CHART_TYPES:
        return None
    x = value.get("x")
    series = value.get("series")
    if not isinstance(x, str) or not isinstance(series, str) or not x or not series:
        return None
    return {"type": ctype, "x": x, "series": series}


def _validate(obj) -> dict | None:
    """Accept only dicts with a valid intent and well-typed optional slots."""
    if not isinstance(obj, dict):
        return None
    intent = obj.get("intent")
    if intent not in VALID_INTENTS:
        return None
    result: dict = {"intent": intent}
    sql = obj.get("sql")
    if sql is not None:
        if not isinstance(sql, str):
            return None
        result["sql"] = sql
    message = obj.get("message")
    if message is not None:
        if not isinstance(message, str):
            return None
        result["message"] = message
    chart = obj.get("chart")
    if intent == "query_data" and chart is not None:
        validated_chart = _validate_chart(chart)
        if validated_chart is not None:
            result["chart"] = validated_chart
    return result


def parse_intent(text: str | None) -> dict | None:
    """Parse the model reply into an intent dict, or None when invalid.

    Tolerates code fences and surrounding prose; the returned dict always
    carries ``intent`` in ``VALID_INTENTS`` and only string-valued
    ``sql`` / ``message`` slots when present.
    """
    if not text:
        return None

    candidates = [text]
    for match in _FENCED_JSON.finditer(text):
        candidates.append(match.group(1).strip())
    for candidate in _candidate_objects(text):
        candidates.append(candidate)

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        validated = _validate(obj)
        if validated is not None:
            return validated
    return None

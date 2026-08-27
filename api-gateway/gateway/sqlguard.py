"""SQL safety guard for the data agent.

A statement-level filter applied BEFORE any execution decision. The
read-only executor (gateway.query) is the second layer of defense; this
guard exists to reject dangerous SQL early so the agent loop can surface
a clear "blocked" verdict without reaching the database at all.

Rules:
- Comments are stripped first (so keyword checks see executable SQL only).
- String literals are masked before the multi-statement check so a ``;``
  inside a quoted value is not mistaken for a statement separator.
- Exactly one statement, and it must be a SELECT.
- No write/DDL/pragma-style keywords anywhere.
"""

import re

_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_STRING_LITERAL = re.compile(r"'([^']|'')*'")
_QUOTED_IDENT = re.compile(r'"([^"]|"")*"')

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|replace|drop|create|alter|truncate|vacuum|"
    r"reindex|attach|detach|pragma|begin|commit|rollback|savepoint|"
    r"grant|revoke)\b",
    re.IGNORECASE,
)

# A leading keyword that turns a SELECT-shaped statement into something
# else (EXPLAIN, WITH ... DELETE, etc.). WITH is allowed as a CTE prefix
# only when no forbidden keyword follows — the keyword scan catches the
# rest.
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def _mask_literals(sql: str) -> str:
    """Replace string literals and quoted identifiers with placeholders."""
    sql = _STRING_LITERAL.sub("''", sql)
    return _QUOTED_IDENT.sub('""', sql)


def check_sql_safety(sql: str) -> tuple[bool, str | None]:
    """Return ``(ok, reason)``; ``reason`` is None when the SQL is safe.

    This is an early filter — ``execute_readonly_query`` still enforces
    table allowlists and a read-only connection at execution time.
    """
    if not sql or not sql.strip():
        return False, "Empty SQL statement"

    stripped = _strip_comments(sql).strip()
    if not stripped:
        return False, "Empty SQL statement"

    masked = _mask_literals(stripped)
    if ";" in masked.rstrip(";"):
        return False, "Only a single statement is allowed"

    m = _FORBIDDEN_KEYWORDS.search(masked)
    if m:
        keyword = m.group(1).upper()
        return False, f"Statement contains disallowed keyword: {keyword}"

    if not _ALLOWED_START.match(stripped):
        return False, "Only SELECT statements are allowed"

    return True, None

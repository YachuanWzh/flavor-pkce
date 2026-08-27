"""Column-glossary store tests (admin semantic column annotations)."""

import json
import os
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db
from gateway import glossary


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="glossary_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def test_upsert_and_list():
    glossary.upsert_glossary_entry(
        "audit_logs", "status", "HTTP 状态码",
        ["状态", "http状态"], "200=成功, 4xx=客户端错误, 5xx=服务端错误",
    )
    rows = glossary.list_glossary_entries()
    assert len(rows) == 1
    row = rows[0]
    assert row["table_name"] == "audit_logs"
    assert row["column_name"] == "status"
    assert row["business_name"] == "HTTP 状态码"
    assert json.loads(row["synonyms"]) == ["状态", "http状态"]
    assert row["enabled"] is True


def test_upsert_normalises_names_and_updates():
    glossary.upsert_glossary_entry("AUDIT_LOGS", "Status", "status", [], "desc")
    glossary.upsert_glossary_entry("audit_logs", "status", "状态码", ["状态"], "new desc")
    rows = glossary.list_glossary_entries()
    assert len(rows) == 1
    assert rows[0]["business_name"] == "状态码"
    assert rows[0]["description"] == "new desc"


def test_upsert_requires_table_and_column():
    with pytest.raises(ValueError):
        glossary.upsert_glossary_entry("  ", "status", "", [], "")
    with pytest.raises(ValueError):
        glossary.upsert_glossary_entry("audit_logs", "", "", [], "")


def test_delete():
    glossary.upsert_glossary_entry("audit_logs", "status", "", [], "")
    entry = glossary.list_glossary_entries()[0]
    assert glossary.delete_glossary_entry(entry["id"]) is True
    assert glossary.delete_glossary_entry(entry["id"]) is False


def test_render_prompt_only_enabled():
    glossary.upsert_glossary_entry(
        "audit_logs", "status", "HTTP 状态码",
        ["状态", "http状态"], "200=成功, 4xx=客户端错误",
    )
    glossary.upsert_glossary_entry(
        "audit_logs", "level", "hidden", [], "should not appear", enabled=False,
    )
    text = glossary.render_glossary_prompt()
    assert "audit_logs.status" in text
    assert "HTTP 状态码" in text
    assert "状态、http状态" in text
    assert "200=成功" in text
    assert "level" not in text


def test_render_prompt_empty_when_no_entries():
    assert glossary.render_glossary_prompt() == ""

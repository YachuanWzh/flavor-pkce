"""Metric-term dictionary store tests (admin business glossary)."""

import json
import os
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db
from gateway import terms


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="terms_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def test_upsert_and_list():
    terms.upsert_metric_term("cache_hit_ratio", "cache_read / (prompt + cache_read + cache_creation)", ["命中率", "cache hit"])
    rows = terms.list_metric_terms()
    assert len(rows) == 1
    assert rows[0]["term"] == "cache_hit_ratio"
    assert rows[0]["enabled"] is True
    assert json.loads(rows[0]["synonyms"]) == ["命中率", "cache hit"]


def test_upsert_same_term_updates():
    terms.upsert_metric_term("gmv", "gross merchandise value", [])
    terms.upsert_metric_term("gmv", "updated definition", ["流水", "交易额"])
    rows = terms.list_metric_terms()
    assert len(rows) == 1
    assert rows[0]["definition"] == "updated definition"


def test_delete_and_disable():
    terms.upsert_metric_term("a", "def a", [])
    terms.upsert_metric_term("b", "def b", [])
    term = terms.list_metric_terms()[0]  # newest = b
    assert terms.delete_metric_term(term["id"]) is True
    terms.upsert_metric_term("b", "def b2", [], enabled=False)
    enabled = terms.list_metric_terms(enabled_only=True)
    assert [r["term"] for r in enabled] == ["a"]


def test_render_prompt_only_enabled():
    terms.upsert_metric_term("gmv", "gross merchandise value", ["流水", "交易额"])
    terms.upsert_metric_term("hidden", "should not appear", [], enabled=False)
    text = terms.render_metric_prompt()
    assert "gmv" in text
    assert "流水" in text
    assert "hidden" not in text

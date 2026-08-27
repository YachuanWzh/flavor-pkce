"""QA knowledge-pair store tests (admin few-shot correction examples)."""

import json
import os
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db
from gateway import qa


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="qa_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def test_upsert_and_list():
    qa.upsert_qa_pair(
        "统计上个月的销售额",
        "SELECT substr(timestamp,1,10) AS date, COUNT(*) AS requests FROM audit_logs GROUP BY date",
        ["sales", "月度"],
    )
    rows = qa.list_qa_pairs()
    assert len(rows) == 1
    assert rows[0]["question"] == "统计上个月的销售额"
    assert rows[0]["enabled"] is True
    assert json.loads(rows[0]["tags"]) == ["sales", "月度"]


def test_upsert_same_question_updates():
    qa.upsert_qa_pair("q", "SELECT 1 AS n", [])
    qa.upsert_qa_pair("q", "SELECT 2 AS n", ["tag"], enabled=False)
    rows = qa.list_qa_pairs()
    assert len(rows) == 1
    assert rows[0]["sql_template"] == "SELECT 2 AS n"
    assert rows[0]["enabled"] is False


def test_upsert_requires_fields():
    with pytest.raises(ValueError):
        qa.upsert_qa_pair("  ", "SELECT 1")
    with pytest.raises(ValueError):
        qa.upsert_qa_pair("question", "   ")


def test_delete():
    qa.upsert_qa_pair("q", "SELECT 1 AS n", [])
    pair = qa.list_qa_pairs()[0]
    assert qa.delete_qa_pair(pair["id"]) is True
    assert qa.delete_qa_pair(pair["id"]) is False
    assert qa.list_qa_pairs() == []


def test_match_qa_pairs_chinese_bigrams():
    qa.upsert_qa_pair(
        "上个月销售额是多少",
        "SELECT COUNT(*) AS n FROM audit_logs",
        [],
    )
    hits = qa.match_qa_pairs("统计上个月的销售额")
    assert [h["question"] for h in hits] == ["上个月销售额是多少"]


def test_match_qa_pairs_english_words():
    qa.upsert_qa_pair(
        "how many requests per user",
        'SELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"',
        [],
    )
    hits = qa.match_qa_pairs("how many requests per user in July?")
    assert len(hits) == 1
    assert hits[0]["question"] == "how many requests per user"


def test_match_ignores_unrelated_pairs():
    qa.upsert_qa_pair("统计上个月的销售额", "SELECT 1 AS n", [])
    assert qa.match_qa_pairs("what is the cache hit ratio?") == []


def test_match_disabled_pairs_excluded():
    qa.upsert_qa_pair("上个月销售额是多少", "SELECT 1 AS n", [], enabled=False)
    assert qa.match_qa_pairs("统计上个月的销售额") == []


def test_render_prompt_includes_matched_pairs():
    qa.upsert_qa_pair(
        "上个月销售额是多少",
        "SELECT COUNT(*) AS n FROM audit_logs",
        [],
    )
    text = qa.render_qa_prompt("统计上个月的销售额")
    assert "上个月销售额是多少" in text
    assert "SELECT COUNT(*) AS n FROM audit_logs" in text


def test_render_prompt_empty_without_question_or_match():
    assert qa.render_qa_prompt(None) == ""
    qa.upsert_qa_pair("上个月销售额是多少", "SELECT 1 AS n", [])
    assert qa.render_qa_prompt("unrelated question") == ""

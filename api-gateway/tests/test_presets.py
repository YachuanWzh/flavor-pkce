"""Preset-question store tests (chat one-click shortcuts)."""

import os
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db
from gateway import presets


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="presets_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def test_upsert_and_list_ordered():
    presets.upsert_preset_question("How many requests today?", sort_order=2)
    presets.upsert_preset_question("Top models this week", sort_order=1)
    presets.upsert_preset_question("Any errors in the last hour?", sort_order=0)
    rows = presets.list_preset_questions()
    assert [r["question"] for r in rows] == [
        "Any errors in the last hour?",
        "Top models this week",
        "How many requests today?",
    ]


def test_upsert_same_question_updates():
    presets.upsert_preset_question("q", enabled=True, sort_order=0)
    presets.upsert_preset_question("q", enabled=False, sort_order=5)
    rows = presets.list_preset_questions()
    assert len(rows) == 1
    assert rows[0]["enabled"] is False
    assert rows[0]["sort_order"] == 5


def test_upsert_requires_question():
    with pytest.raises(ValueError):
        presets.upsert_preset_question("   ")


def test_enabled_only():
    presets.upsert_preset_question("on", enabled=True, sort_order=0)
    presets.upsert_preset_question("off", enabled=False, sort_order=1)
    rows = presets.list_preset_questions(enabled_only=True)
    assert [r["question"] for r in rows] == ["on"]


def test_delete():
    presets.upsert_preset_question("q")
    preset = presets.list_preset_questions()[0]
    assert presets.delete_preset_question(preset["id"]) is True
    assert presets.delete_preset_question(preset["id"]) is False

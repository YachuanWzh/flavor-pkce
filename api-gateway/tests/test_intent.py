"""Intent-slot JSON parsing tests (agent-loop Task 2)."""

import pytest

from gateway.intent import parse_intent


def test_plain_json_object():
    result = parse_intent('{"intent": "query_data", "sql": "SELECT 1"}')
    assert result == {"intent": "query_data", "sql": "SELECT 1"}


def test_json_inside_code_fence():
    text = '```json\n{"intent": "chitchat", "message": "hi"}\n```'
    result = parse_intent(text)
    assert result == {"intent": "chitchat", "message": "hi"}


def test_json_embedded_in_surrounding_text():
    text = (
        "Here is my answer:\n"
        '{"intent": "clarify", "message": "which table?"}\n'
        "Let me know."
    )
    result = parse_intent(text)
    assert result == {"intent": "clarify", "message": "which table?"}


def test_invalid_intent_value_rejected():
    assert parse_intent('{"intent": "drop_all"}') is None


def test_missing_intent_key_rejected():
    assert parse_intent('{"sql": "SELECT 1"}') is None


def test_not_json_rejected():
    assert parse_intent("Sure, here is your answer!") is None


def test_empty_input_rejected():
    assert parse_intent("") is None
    assert parse_intent(None) is None


def test_non_object_json_rejected():
    assert parse_intent("[1, 2, 3]") is None


def test_valid_intents_accepted():
    for intent in ("query_data", "chitchat", "clarify"):
        result = parse_intent(f'{{"intent": "{intent}"}}')
        assert result is not None
        assert result["intent"] == intent


def test_sql_must_be_string():
    assert parse_intent('{"intent": "query_data", "sql": 123}') is None


def test_chart_slot_parsed_for_query_data():
    result = parse_intent(
        '{"intent": "query_data", "sql": "SELECT 1", '
        '"chart": {"type": "bar", "x": "date", "series": "requests"}}'
    )
    assert result["chart"] == {"type": "bar", "x": "date", "series": "requests"}


def test_chart_slot_requires_query_data():
    # chart on a non-query_data intent is dropped (not fatal).
    result = parse_intent(
        '{"intent": "chitchat", "message": "hi", "chart": {"type": "bar"}}'
    )
    assert "chart" not in result


def test_chart_slot_rejects_invalid_type():
    result = parse_intent(
        '{"intent": "query_data", "sql": "SELECT 1", '
        '"chart": {"type": "scatter", "x": "date", "series": "n"}}'
    )
    assert "chart" not in result

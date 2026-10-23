"""Tests for :mod:`socialhome.json_text`."""

from __future__ import annotations

import orjson
import pytest

from socialhome.json_text import dumps_str


def test_dumps_str_is_compact_utf8_text() -> None:
    out = dumps_str({"name": "Zoë 漢字 😀", "n": [1, 2.5, None, True]})
    assert out == '{"name":"Zoë 漢字 😀","n":[1,2.5,null,true]}'
    assert isinstance(out, str)


def test_dumps_str_round_trips() -> None:
    value = {"a": {"b": ["x", 0.0001, -3]}}
    assert orjson.loads(dumps_str(value)) == value


def test_dumps_str_stringifies_non_str_keys_like_the_stdlib() -> None:
    assert dumps_str({1: "x", 2.5: "y"}) == '{"1":"x","2.5":"y"}'


def test_dumps_str_refuses_what_json_cannot_hold() -> None:
    with pytest.raises(TypeError):
        dumps_str({"s": {1, 2}})

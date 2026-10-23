"""The ``zlib1+dict1`` preset dictionary is fixed: pinned byte for byte."""

from __future__ import annotations

import hashlib
import re

import orjson

from socialhome.domain import item_zdict
from socialhome.domain.item_zdict import ZDICT_V1

#: The digest of the dictionary every ``zlib1+dict1`` sender and receiver
#: uses. Changing the bytes breaks every item between old and new builds —
#: a new dictionary is a new suite (``zlib1+dict2``), never an edit here.
ZDICT_V1_SHA256 = "2145e8b057a9a0be2f2609dc74a69e1f39ad385a82a2db2b668193610aefe712"


def test_the_dictionary_is_pinned():
    assert len(ZDICT_V1) == 1742
    assert hashlib.sha256(ZDICT_V1).hexdigest() == ZDICT_V1_SHA256


def test_zlib_can_reach_all_of_it():
    assert len(ZDICT_V1) <= 32 * 1024


def test_the_shapes_are_json_with_every_variable_value_empty():
    """Keys and constant values only: no id, key, signature, timestamp or
    text of any space, household or person is in it."""
    for shape in (item_zdict._COMMENT, item_zdict._RELAY_POST, item_zdict._MEMBER_POST):
        parsed = orjson.loads(shape)
        assert all(
            s in {"", "post", "comment", "text", "write", "ed25519"}
            for s in _strings(parsed)
        )
    assert not re.search(rb"[0-9a-f]{16}", ZDICT_V1)


def _strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj

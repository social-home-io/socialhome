"""App engines — the shapes every engine shares (``app_engines/domain.py``)."""

from __future__ import annotations

from socialhome.app_engines.domain import (
    ENGINE_SUITE_FIELD,
    VERB_ENGINE,
    AppEngineActionError,
    AppEngineNotDeclaredError,
    AppEngineUnsupportedError,
    EngineCall,
)
from socialhome.domain.app_group import GROUP_VERBS


def test_one_verb_and_its_suite_field():
    assert VERB_ENGINE == "engine" and VERB_ENGINE not in GROUP_VERBS
    assert ENGINE_SUITE_FIELD.endswith("_suite")


def test_errors_name_the_engine():
    err = AppEngineNotDeclaredError("draw")
    assert (err.status, err.code, err.params) == (
        403,
        "APP_ENGINE_NOT_DECLARED",
        {"engine": "draw"},
    )
    err2 = AppEngineUnsupportedError("deal")
    assert (err2.status, err2.code) == (409, "APP_ENGINE_UNSUPPORTED")
    assert AppEngineActionError().status == 404


def test_a_call_defaults():
    call = EngineCall(app_id="a", session_id="s", actor_user_id="u", method="GET")
    assert call.action is None and call.body == {}

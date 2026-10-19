"""The household dashboard places and spells calendar events by the same
cases the SPA is tested against (``tests/fixtures/calendar_span_cases.json``,
also read by ``client/src/utils/calendarSpanCases.test.ts``)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from socialhome.dashboard.clock import span_label
from socialhome.domain.calendar import event_day_span
from socialhome.i18n import Catalog

ROOT = Path(__file__).parents[2]
CASES = json.loads((ROOT / "tests/fixtures/calendar_span_cases.json").read_text())[
    "cases"
]
CATALOG = Catalog.from_directory(ROOT / "socialhome/i18n/messages")


def _t(key: str, **params: object) -> str:
    return CATALOG.gettext(key, locale="en", **params)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_same_days_and_label_as_the_spa(case: dict) -> None:
    span = event_day_span(
        datetime.fromisoformat(case["start"]),
        datetime.fromisoformat(case["end"]),
        all_day=case["all_day"],
        event_tz=case["tz"],
        viewer_tz="UTC",
    )
    assert span.first == date.fromisoformat(case["first"])
    assert span.last == date.fromisoformat(case["last"])
    assert span.day_count == case["day_count"]
    assert span_label(span, _t) == case["label_en"]

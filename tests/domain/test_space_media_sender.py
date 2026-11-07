"""The SPACE_MEDIA_BLOB sender-binding value types."""

from __future__ import annotations

import pytest

from socialhome.domain.space_media_sender import (
    HeldSpaceBlob,
    MediaBlobVerdict,
    SpaceMediaOwner,
)


def test_held_blob_round_trips_through_its_record():
    held = HeldSpaceBlob(
        key="held-x",
        sender="peer-a",
        space_id="sp-a",
        correlation_id="post-1",
        filename="f.webp",
    )
    assert HeldSpaceBlob.from_dict("held-x", held.to_dict()) == held


@pytest.mark.parametrize(
    "data",
    [
        None,
        "not a dict",
        {},
        {"sender": "a", "space_id": "s", "correlation_id": "c"},
        {"sender": "a", "space_id": "s", "correlation_id": "", "filename": "f"},
        {"sender": 1, "space_id": "s", "correlation_id": "c", "filename": "f"},
    ],
)
def test_an_incomplete_record_is_not_a_held_blob(data):
    assert HeldSpaceBlob.from_dict("held-x", data) is None


def test_owner_defaults_bind_nobody():
    owner = SpaceMediaOwner(in_scope=True)
    assert owner.author_user_id == ""
    assert owner.collaborative is False


def test_verdict_values_are_stable():
    assert {v.value for v in MediaBlobVerdict} == {"accept", "hold", "refuse"}

"""Release-blocker protocol tests: a household learns only its own picks.

Marked ``@pytest.mark.security``.

A highlight's audience names people (``users``) or households
(``households``) across several paired households. Each household gets
its own ``HIGHLIGHT_CREATED`` / ``HIGHLIGHT_FRAME_APPENDED`` envelope, and
the rule these tests encode, against the real application wiring:

    The ``audience`` a household receives names only that household's
    own people (``users``), only that household (``households``), or
    nobody (``all_paired``) — never another household's people, never
    another household, never the author's housemates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    highlight_repo_key,
    highlight_service_key,
)
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService

pytestmark = pytest.mark.security

FET = FederationEventType

BOB_HOME = "peer-bob"  # Bob + Bea live here
DORA_HOME = "peer-dora"  # Dora lives here
PEOPLE = {
    BOB_HOME: ("u-bob", "u-bea"),
    DORA_HOME: ("u-dora",),
}
FRAME_EVENTS = (FET.HIGHLIGHT_CREATED, FET.HIGHLIGHT_FRAME_APPENDED)


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "highlight.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user_ids: tuple[str, ...]) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    for user_id in user_ids:
        await db.enqueue(
            "INSERT INTO remote_users(user_id, instance_id, remote_username,"
            " display_name) VALUES(?,?,?,?)",
            (user_id, instance_id, user_id, user_id),
        )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for username, user_id in (("anna", "u-anna"), ("carl", "u-carl")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, user_id, username),
        )
    for instance_id, user_ids in PEOPLE.items():
        await _seed_peer(db, instance_id, user_ids)

    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _capture(_self, *, to_instance_id, event_type, payload, **_kw) -> None:
        sent.append((to_instance_id, event_type, payload))

    # Slotted service: patch the class (restored after the test).
    monkeypatch.setattr(FederationService, "send_event", _capture)
    return app, sent


async def _post_two_frames(app, audience_kind: str, audience: list[str]) -> None:
    svc = app[highlight_service_key]
    for n in (1, 2):
        await svc.create_or_append_frame(
            author_user_id="u-anna",
            frame_type="image",
            media_url=f"/api/media/frame-{n}.webp",
            audience_kind=audience_kind,
            audience=audience,
        )


def _frame_envelopes(sent) -> dict[tuple[str, FederationEventType], list[str]]:
    return {
        (to, event_type): payload["audience"]
        for to, event_type, payload in sent
        if event_type in FRAME_EVENTS
    }


async def test_users_audience_names_only_the_receivers_own_people(env):
    app, sent = env
    # Anna picks her housemate Carl, both of Bob's household and Dora.
    await _post_two_frames(app, "users", ["u-carl", "u-bob", "u-bea", "u-dora"])
    got = _frame_envelopes(sent)
    assert set(got) == {
        (home, event_type) for home in PEOPLE for event_type in FRAME_EVENTS
    }
    for (home, _event_type), audience in got.items():
        assert sorted(audience) == sorted(PEOPLE[home]), (home, audience)


async def test_households_audience_names_only_the_receiver(env):
    app, sent = env
    await _post_two_frames(app, "households", [BOB_HOME, DORA_HOME])
    got = _frame_envelopes(sent)
    assert set(got) == {
        (home, event_type) for home in PEOPLE for event_type in FRAME_EVENTS
    }
    for (home, _event_type), audience in got.items():
        assert audience == [home], (home, audience)


async def test_all_paired_audience_names_nobody(env):
    app, sent = env
    await _post_two_frames(app, "all_paired", [])
    got = _frame_envelopes(sent)
    assert {home for home, _ in got} == set(PEOPLE)
    assert all(audience == [] for audience in got.values())


async def test_a_trimmed_users_list_still_gates_the_receivers_people(env):
    """Receiver compat: the slice is all a receiver reads. Bob's household
    sends us a ``users`` highlight trimmed to our own pick (Carl); Carl
    sees it, Anna — not picked — does not."""
    app, _sent = env
    handlers = app[federation_service_key]._event_registry.handlers_for(
        FET.HIGHLIGHT_CREATED
    )
    assert handlers
    event = FederationEvent(
        msg_id="m",
        event_type=FET.HIGHLIGHT_CREATED,
        from_instance=BOB_HOME,
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload={
            "highlight_id": "h-bob",
            "frame_id": "f-bob",
            "author_user_id": "u-bob",
            "highlight_date": "2026-05-02",
            "sequence": 1,
            "audience_kind": "users",
            "audience": ["u-carl"],
            "frame_type": "image",
            "media_url": "/api/media/bob.webp",
            "caption_text": None,
            "caption_emoji": None,
            "duration_ms": None,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    for handler in handlers:
        await handler(event)
    repo = app[highlight_repo_key]
    assert [h.id for h in await repo.list_visible_to("u-carl")] == ["h-bob"]
    assert await repo.list_visible_to("u-anna") == []

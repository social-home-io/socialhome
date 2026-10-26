"""Release-blocker protocol tests: changing today's highlight audience.

Marked ``@pytest.mark.security``.

"Change for all of today" (``PATCH /api/highlights/{id}/audience``,
owner decision 1) reuses the events every receiver already applies —
no new event type, no protocol version. Against the real application
wiring of an author household and two receiving households, the rules:

* **Narrowing**: a household that drops out gets ``HIGHLIGHT_DELETED``,
  deletes the highlight and its frames, and is never sent a later frame.
  A household that stays gets ``HIGHLIGHT_CREATED`` re-sent with its own
  (trimmed) slice and re-gates its users — someone no longer picked can't
  read the highlight through any API path.
* **Widening**: only a newly added household gets the day — today's
  frames, nobody else's, no earlier day — and a household already in it
  gets no frame twice.
* Only the author changes the audience, and only today's; a refused
  change sends nothing.
* The per-household trim (#1173) holds after a change, for the re-sent
  envelope and every later frame.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    highlight_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.highlight import HighlightNotTodayError
from socialhome.federation.federation_service import FederationService
from socialhome.services.highlight_service import (
    HighlightForbiddenError,
    HighlightNotFoundError,
)

pytestmark = pytest.mark.security

FET = FederationEventType

BOB_HOME = "peer-bob"  # Bob + Bea live here
DORA_HOME = "peer-dora"  # Dora lives here
PEOPLE = {BOB_HOME: ("u-bob", "u-bea"), DORA_HOME: ("u-dora",)}


def _config(tmp_dir, name: str) -> Config:
    root = tmp_dir / name
    root.mkdir()
    return Config(
        data_dir=str(root),
        db_path=str(root / "sh.db"),
        media_path=str(root / "media"),
        apps_path=str(root / "apps"),
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


async def _local_user(db, user_id: str) -> dict:
    """A member of this household with their own API token."""
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, state)"
        " VALUES(?,?,?,'active')",
        (user_id, user_id, user_id),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?,?,'t',?)",
        (f"t-{user_id}", user_id, sha256_token_hash(f"tok-{user_id}")),
    )
    return {"Authorization": f"Bearer tok-{user_id}"}


class _Net:
    """The author household plus two receivers, wired by hand: what the
    author's outbound sends is captured and handed to the receiver's real
    inbound registry, as the §24.11 pipeline would after verify/decrypt."""

    def __init__(self, author, receivers, sent, author_instance) -> None:
        self.author = author
        self.receivers = receivers  # instance id → (app, client, {uid: headers})
        self.sent = sent
        self.author_instance = author_instance

    async def deliver(self) -> list[tuple[str, FederationEventType, dict]]:
        batch, self.sent[:] = list(self.sent), []
        for to, event_type, payload in batch:
            app = self.receivers[to][0]
            registry = app[federation_service_key]._event_registry
            for handler in registry.handlers_for(event_type):
                await handler(
                    FederationEvent(
                        msg_id="m",
                        event_type=event_type,
                        from_instance=self.author_instance,
                        to_instance=to,
                        timestamp=datetime.now(UTC).isoformat(),
                        payload=payload,
                    )
                )
        return batch

    async def post_frame(self, n: int, kind: str, audience: list[str]):
        return await self.author[highlight_service_key].create_or_append_frame(
            author_user_id="u-anna",
            frame_type="image",
            media_url=f"/api/media/frame-{n}.webp",
            caption_text=f"frame {n}",
            audience_kind=kind,
            audience=audience,
        )

    async def change(self, highlight_id: str, kind: str, audience: list[str]):
        return await self.author[highlight_service_key].change_audience(
            highlight_id=highlight_id,
            actor_user_id="u-anna",
            audience_kind=kind,
            audience=audience,
        )

    async def held(self, home: str, highlight_id: str) -> tuple[int, int]:
        """``(highlight rows, frame rows)`` the receiver stores for it."""
        db = self.receivers[home][0][db_key]
        h = await db.fetchone(
            "SELECT COUNT(*) AS n FROM highlights WHERE id=?", (highlight_id,)
        )
        f = await db.fetchone(
            "SELECT COUNT(*) AS n FROM highlight_frames WHERE highlight_id=?",
            (highlight_id,),
        )
        return int(h["n"]), int(f["n"])


@pytest.fixture
async def net(aiohttp_client, tmp_dir, monkeypatch):
    author = create_app(_config(tmp_dir, "anna"))
    await aiohttp_client(author)
    adb = author[db_key]
    for user_id in ("u-anna", "u-carl"):
        await _local_user(adb, user_id)
    for instance_id, user_ids in PEOPLE.items():
        await _seed_peer(adb, instance_id, user_ids)
    author_instance = author[federation_service_key].own_instance_id

    receivers = {}
    for home, user_ids in PEOPLE.items():
        app = create_app(_config(tmp_dir, home))
        client = await aiohttp_client(app)
        rdb = app[db_key]
        await _seed_peer(rdb, author_instance, ("u-anna",))
        headers = {uid: await _local_user(rdb, uid) for uid in user_ids}
        receivers[home] = (app, client, headers)

    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _capture(self, *, to_instance_id, event_type, payload, **_kw) -> None:
        # Only the author household's sends travel; a receiver's
        # back-channel (views, reactions) is not part of these tests.
        if self is author[federation_service_key]:
            sent.append((to_instance_id, event_type, payload))

    # Slotted service: patch the class (restored after the test).
    monkeypatch.setattr(FederationService, "send_event", _capture)
    return _Net(author, receivers, sent, author_instance)


async def _cannot_read(client, headers: dict, highlight_id: str, frame_id: str) -> None:
    """Every read and frame action answers the unknown-id 404 (the list
    omits it) — the receiver holds nothing this user may see."""
    listed = await client.get("/api/highlights", headers=headers)
    assert listed.status == 200
    assert highlight_id not in {r["highlight"]["id"] for r in await listed.json()}
    calls = [
        ("get", f"/api/highlights/{highlight_id}", None),
        ("post", f"/api/highlights/frames/{frame_id}/view", {}),
        ("put", f"/api/highlights/frames/{frame_id}/reaction", {"emoji": "🔥"}),
        ("delete", f"/api/highlights/frames/{frame_id}/reaction", None),
        (
            "post",
            f"/api/highlights/frames/{frame_id}/dm-reply",
            {"conversation_id": "c-1", "content": "hi"},
        ),
        ("post", f"/api/highlights/{highlight_id}/share", {"scope": "household"}),
        ("delete", f"/api/highlights/frames/{frame_id}", None),
        ("delete", f"/api/highlights/{highlight_id}", None),
        (
            "patch",
            f"/api/highlights/{highlight_id}/audience",
            {"audience_kind": "all_paired"},
        ),
    ]
    for method, url, body in calls:
        r = await getattr(client, method)(url, json=body, headers=headers)
        assert r.status == 404, (method, url, r.status)


async def test_narrowing_removes_it_where_households_drop_out(net):
    """All paired → Bob's household: Dora's household deletes the highlight
    and its frames, Dora can't read it anywhere, and frames posted later
    never reach her household."""
    highlight, f1 = await net.post_frame(1, "all_paired", [])
    await net.post_frame(2, "all_paired", [])
    await net.deliver()
    assert await net.held(DORA_HOME, highlight.id) == (1, 2)

    await net.change(highlight.id, "households", [BOB_HOME])
    batch = await net.deliver()
    to_dora = [(ev, p) for to, ev, p in batch if to == DORA_HOME]
    assert [ev for ev, _ in to_dora] == [FET.HIGHLIGHT_DELETED]
    assert "audience" not in to_dora[0][1]
    assert await net.held(DORA_HOME, highlight.id) == (0, 0)
    _, dora_client, dora = net.receivers[DORA_HOME]
    await _cannot_read(dora_client, dora["u-dora"], highlight.id, f1.id)

    # Bob's household keeps the day and reads it.
    assert await net.held(BOB_HOME, highlight.id) == (1, 2)
    _, bob_client, bob = net.receivers[BOB_HOME]
    r = await bob_client.get(f"/api/highlights/{highlight.id}", headers=bob["u-bob"])
    assert r.status == 200

    # A later frame goes to Bob's household only.
    await net.post_frame(3, "households", [BOB_HOME])
    later = await net.deliver()
    assert {to for to, _, _ in later} == {BOB_HOME}
    assert await net.held(DORA_HOME, highlight.id) == (0, 0)
    assert await net.held(BOB_HOME, highlight.id) == (1, 3)


async def test_narrowing_people_regates_a_household_that_stays(net):
    """Bob + Bea + Dora → Bea: Bob's household stays (Bea) and learns only
    Bea; Bob loses it on every API path; Dora's household drops out."""
    highlight, f1 = await net.post_frame(1, "users", ["u-bob", "u-bea", "u-dora"])
    await net.deliver()
    await net.change(highlight.id, "users", ["u-bea", "u-carl"])
    batch = await net.deliver()
    assert {(to, ev) for to, ev, _ in batch} == {
        (BOB_HOME, FET.HIGHLIGHT_CREATED),
        (DORA_HOME, FET.HIGHLIGHT_DELETED),
    }
    resent = next(p for to, _, p in batch if to == BOB_HOME)
    assert resent["audience"] == ["u-bea"]

    _, bob_client, headers = net.receivers[BOB_HOME]
    r = await bob_client.get(
        f"/api/highlights/{highlight.id}", headers=headers["u-bea"]
    )
    assert r.status == 200
    await _cannot_read(bob_client, headers["u-bob"], highlight.id, f1.id)
    _, dora_client, dora = net.receivers[DORA_HOME]
    await _cannot_read(dora_client, dora["u-dora"], highlight.id, f1.id)


async def test_widening_sends_todays_frames_only_to_new_households(net):
    """Bob's household → all paired: Dora's household gets today's frames
    (nothing from yesterday) and Bob's household gets no frame twice."""
    old, _ = await net.post_frame(0, "households", [BOB_HOME])
    await net.deliver()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    for db in (net.author[db_key], net.receivers[BOB_HOME][0][db_key]):
        await db.enqueue(
            "UPDATE highlights SET highlight_date=? WHERE id=?", (yesterday, old.id)
        )

    highlight, _ = await net.post_frame(1, "households", [BOB_HOME])
    await net.post_frame(2, "households", [BOB_HOME])
    await net.post_frame(3, "households", [BOB_HOME])
    await net.deliver()
    assert await net.held(DORA_HOME, highlight.id) == (0, 0)

    await net.change(highlight.id, "all_paired", [])
    batch = await net.deliver()
    assert all(p["highlight_id"] == highlight.id for _, _, p in batch)
    to_dora = [(ev, p) for to, ev, p in batch if to == DORA_HOME]
    assert [ev for ev, _ in to_dora] == [
        FET.HIGHLIGHT_CREATED,
        FET.HIGHLIGHT_FRAME_APPENDED,
        FET.HIGHLIGHT_FRAME_APPENDED,
    ]
    assert [p["caption_text"] for _, p in to_dora] == ["frame 1", "frame 2", "frame 3"]
    to_bob = [ev for to, ev, _ in batch if to == BOB_HOME]
    assert to_bob == [FET.HIGHLIGHT_CREATED]  # the row again, not the day
    assert await net.held(DORA_HOME, highlight.id) == (1, 3)
    assert await net.held(DORA_HOME, old.id) == (0, 0)
    _, dora_client, dora = net.receivers[DORA_HOME]
    r = await dora_client.get(f"/api/highlights/{highlight.id}", headers=dora["u-dora"])
    assert r.status == 200
    assert [f["sequence"] for f in (await r.json())["frames"]] == [1, 2, 3]


async def test_a_refused_change_sends_nothing(net):
    """A housemate who reads it can't change it; an earlier day can't be
    changed — and neither attempt sends anything."""
    highlight, _ = await net.post_frame(1, "users", ["u-carl", "u-bob"])
    await net.deliver()
    with pytest.raises(HighlightForbiddenError):
        await net.author[highlight_service_key].change_audience(
            highlight_id=highlight.id,
            actor_user_id="u-carl",
            audience_kind="all_paired",
            audience=[],
        )
    with pytest.raises(HighlightNotFoundError):
        await net.author[highlight_service_key].change_audience(
            highlight_id=highlight.id,
            actor_user_id="u-dora",
            audience_kind="all_paired",
            audience=[],
        )
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    await net.author[db_key].enqueue(
        "UPDATE highlights SET highlight_date=? WHERE id=?", (yesterday, highlight.id)
    )
    with pytest.raises(HighlightNotTodayError):
        await net.change(highlight.id, "all_paired", [])
    assert net.sent == []
    assert await net.held(DORA_HOME, highlight.id) == (0, 0)


async def test_the_per_household_trim_holds_after_a_change(net):
    """After a change, each household still learns only its own picks —
    in the re-sent envelope, the day sent to a new household and every
    later frame."""
    highlight, _ = await net.post_frame(1, "users", ["u-bob", "u-carl"])
    await net.deliver()
    await net.change(highlight.id, "users", ["u-bea", "u-bob", "u-dora", "u-carl"])
    await net.post_frame(2, "users", ["u-bea", "u-bob", "u-dora", "u-carl"])
    sent = list(net.sent)
    await net.deliver()
    frame_events = (FET.HIGHLIGHT_CREATED, FET.HIGHLIGHT_FRAME_APPENDED)
    seen = {to for to, ev, _ in sent if ev in frame_events}
    assert seen == {BOB_HOME, DORA_HOME}
    for to, ev, payload in sent:
        if ev in frame_events:
            assert sorted(payload["audience"]) == sorted(PEOPLE[to]), (to, payload)
    assert "u-carl" not in str([p for _, _, p in sent])

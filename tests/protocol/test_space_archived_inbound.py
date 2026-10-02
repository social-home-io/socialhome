"""Release-blocker protocol tests: an archived space is read-only to peers too.

Marked ``@pytest.mark.security``.

Locally an archived space refuses new content at the REST layer
(``SpaceService._require_writable_space`` and siblings). A federated write
is the same write arriving by another door, so the §24.11 post-decrypt
gates refuse the :data:`SPACE_WRITE_EVENT_TYPES` vocabulary into a
space archived here (``check_space_archived``), and the §25.6 sync
receiver refuses its content resources the same way — one decision,
:func:`socialhome.federation.space_scope.archive_refusal`.

What still applies: **removals**
(:data:`ARCHIVED_ALLOWED_REMOVAL_TYPES` — an author may still delete their
own post locally, so the delete must reach every copy; the handler's
authorship check still decides who may remove which row), the roster, config (the host unarchiving the space),
and every other non-write type. The host of a *reversibly* archived space
may still fill the snapshot (resume replay / catch-up sync of pre-archive
state); a *terminated* stub (``archived_reason`` set) is frozen for
everybody.

Every end-to-end case runs the REAL production seam — the post-decrypt
gates and then the registry (:meth:`FederationService.replay_held`) — over
the real app and a real SQLite database, and compares a snapshot of every
content table. Each refusal has a positive control: the same delivery into
the space before it is archived changes the snapshot.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key, space_sync_receiver_key
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    ARCHIVED_ALLOWED_REMOVAL_TYPES,
    SPACE_READER_EVENT_TYPES,
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation import routed_crypto
from socialhome.federation.inbound_validator import (
    InboundContext,
    make_check_space_archived,
)
from socialhome.federation.sync.space.exporter import (
    ALLOWED_RESOURCES,
    REMOVAL_RESOURCES,
    ROSTER_RESOURCES,
)
from socialhome.federation.routed_envelope import SpaceRoutedHandler
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)

from .test_space_content_authorship import (
    _SEED,
    _SYNC_TABLES,
    _SYNC_NOT_WRITTEN,
    CASES,
    AUTHOR,
    HOST,
    OTHER,
    SP,
    SYNC_CASES,
    _config,
    _event,
    _snapshot,
    _sync_snapshot,
)
from .test_media_blob_scope import WEBP
from .test_space_content_scope import NOT_ROW_SCOPED
from .test_space_routed_security import (
    INNER_EVENT,
    ROUTE_ID,
    _MemberTargetFed,
    _seal,
)

pytestmark = pytest.mark.security

FET = FederationEventType
ARCHIVED = {"status": "ok", "dropped": "archived-space"}


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    return app, db


async def _archive(db, reason: str | None = None) -> None:
    await db.enqueue(
        "UPDATE spaces SET archived=1, archived_reason=? WHERE id=?", (reason, SP)
    )


async def _through_the_gates(app, event_type, payload, *, sender) -> None:
    """The production post-decrypt gates, then the registry."""
    fed = app[federation_service_key]
    await fed.replay_held(_event(event_type, payload, sender=sender), reason="test")


def _live(senders_of, *, removals: bool = False, refused: bool = False):
    return [
        pytest.param(et, payload, sender, id=f"{et.value}: {label} <- {sender}")
        for et, label, payload, allowed, refused_by in CASES
        if (et in ARCHIVED_ALLOWED_REMOVAL_TYPES) == removals
        for sender in senders_of(refused_by if refused else allowed)
    ]


_MEMBER_WRITES = _live(lambda allowed: [s for s in allowed if s != HOST])
_HOST_WRITES = _live(lambda allowed: [s for s in allowed if s == HOST])
#: Removals by the household that may remove the row — they still land.
_RIGHTFUL_REMOVALS = _live(list, removals=True)
#: Removals by a household that may not — the handler refuses them.
_WRONGFUL_REMOVALS = _live(list, removals=True, refused=True)


# ── Live events (§24.11 pipeline / mesh / held / resume replay) ──────────


@pytest.mark.parametrize(("event_type", "payload", "sender"), _MEMBER_WRITES)
async def test_a_member_write_lands_while_the_space_is_live(
    env, event_type, payload, sender
):
    """Positive control: through the same gates, before the archive."""
    app, db = env
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    assert await _snapshot(db) != before, f"{event_type.value} from {sender}"


@pytest.mark.parametrize(("event_type", "payload", "sender"), _MEMBER_WRITES)
async def test_a_member_write_into_an_archived_space_changes_nothing(
    env, event_type, payload, sender
):
    app, db = env
    await _archive(db)
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    after = await _snapshot(db)
    changed = sorted(t for t in before if before[t] != after[t])
    assert not changed, f"{event_type.value} from {sender} wrote {changed}"


@pytest.mark.parametrize(("event_type", "payload", "sender"), _HOST_WRITES)
async def test_nobody_writes_into_a_terminated_space_not_even_the_host(
    env, event_type, payload, sender
):
    app, db = env
    await _archive(db, "dissolved")
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    after = await _snapshot(db)
    changed = sorted(t for t in before if before[t] != after[t])
    assert not changed, f"{event_type.value} from the host wrote {changed}"


@pytest.mark.parametrize(("event_type", "payload", "sender"), _HOST_WRITES)
async def test_the_host_still_fills_a_reversibly_archived_snapshot(
    env, event_type, payload, sender
):
    """The archive is the host's own decision and its API is read-only for
    the space, so what it still sends is pre-archive state a member missed."""
    app, db = env
    await _archive(db)
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    assert await _snapshot(db) != before, f"{event_type.value} from the host"


@pytest.mark.parametrize("reason", [None, "dissolved"])
@pytest.mark.parametrize(("event_type", "payload", "sender"), _RIGHTFUL_REMOVALS)
async def test_a_rightful_removal_still_reaches_an_archived_copy(
    env, event_type, payload, sender, reason
):
    """An author may delete their own post in an archived space locally; if
    every peer dropped that delete, the post would outlive its deletion on
    every other copy. Removals always propagate."""
    app, db = env
    await _archive(db, reason)
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    assert await _snapshot(db) != before, f"{event_type.value} from {sender}"


@pytest.mark.parametrize(("event_type", "payload", "sender"), _WRONGFUL_REMOVALS)
async def test_a_wrongful_removal_is_still_refused_by_the_handler(
    env, event_type, payload, sender
):
    """Letting removals past the archive gate is not an authorization: the
    handler still decides who may remove which row."""
    app, db = env
    await _archive(db)
    before = await _snapshot(db)
    await _through_the_gates(app, event_type, payload, sender=sender)
    after = await _snapshot(db)
    changed = sorted(t for t in before if before[t] != after[t])
    assert not changed, f"{event_type.value} from {sender} wrote {changed}"


async def test_a_member_deletes_their_own_post_in_an_archived_space(env):
    """The named regression: u-a (household AUTHOR) deletes post-a."""
    app, db = env
    await _archive(db)
    await _through_the_gates(
        app, FET.SPACE_POST_DELETED, {"id": "post-a"}, sender=AUTHOR
    )
    row = await db.fetchone("SELECT deleted FROM space_posts WHERE id='post-a'", ())
    assert row is not None and row["deleted"] == 1
    # …and another household's attempt on a row it does not own changes nothing.
    await db.enqueue("UPDATE space_posts SET deleted=0 WHERE id='post-a'", ())
    await _through_the_gates(
        app, FET.SPACE_POST_DELETED, {"id": "post-a"}, sender=OTHER
    )
    row = await db.fetchone("SELECT deleted FROM space_posts WHERE id='post-a'", ())
    assert row is not None and row["deleted"] == 0


async def test_a_dissolved_space_takes_no_content(env):
    """The ``spaces.dissolved`` column on its own (not archived) freezes it."""
    app, db = env
    await db.enqueue("UPDATE spaces SET dissolved=1 WHERE id=?", (SP,))
    payload = {"id": "post-new", "author": "u-a", "type": "text", "content": "x"}
    for sender in (AUTHOR, HOST):
        before = await _snapshot(db)
        await _through_the_gates(app, FET.SPACE_POST_CREATED, payload, sender=sender)
        assert await _snapshot(db) == before, sender


def _blob(name: str) -> dict:
    return {
        "post_id": "post-later",
        "correlation_id": "post-later",
        "filename": name,
        "bytes_b64": base64.b64encode(WEBP).decode("ascii"),
    }


async def test_media_bytes_do_not_land_in_an_archived_space(env, tmp_dir):
    """``SPACE_MEDIA_BLOB`` writes files, not rows: checked on the media
    store. Positive control first — a blob that overtook its post lands."""
    app, db = env
    media = tmp_dir / "media"
    await _through_the_gates(
        app, FET.SPACE_MEDIA_BLOB, _blob("live.webp"), sender=AUTHOR
    )
    assert (media / "live.webp").exists()
    await _archive(db)
    await _through_the_gates(
        app, FET.SPACE_MEDIA_BLOB, _blob("frozen.webp"), sender=AUTHOR
    )
    assert not (media / "frozen.webp").exists()


async def test_the_host_can_unarchive_the_space(env):
    """SPACE_CONFIG_CHANGED is not a content write: the host's unarchive
    must reach an archived member copy, or it stays read-only forever."""
    app, db = env
    await _archive(db)
    meta = {
        "name": SP,
        "owner_instance_id": HOST,
        "owner_username": "anna",
        "identity_public_key": "00" * 32,
        "config_sequence": 1,
        "archived": False,
    }
    await _through_the_gates(
        app,
        FET.SPACE_CONFIG_CHANGED,
        {"space_id": SP, "space_meta": meta},
        sender=HOST,
    )
    row = await db.fetchone("SELECT archived FROM spaces WHERE id=?", (SP,))
    assert row is not None and row["archived"] == 0
    # …and content flows again.
    before = await _snapshot(db)
    await _through_the_gates(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "post-back", "author": "u-a", "type": "text", "content": "x"},
        sender="house-author",
    )
    assert await _snapshot(db) != before


async def test_a_roster_event_still_applies_to_an_archived_space(env):
    """The host's authority-signed roster is not a content write: a member
    seated on an archived space still lands in the mirror."""
    app, db = env
    kp = generate_space_keypair()
    await db.enqueue(
        "UPDATE spaces SET identity_public_key=? WHERE id=?", (kp.public_key.hex(), SP)
    )
    await _archive(db, "dissolved")
    bare = {
        "space_id": SP,
        "user_id": "u-late",
        "instance_id": "house-author",
        "display_name": "Late",
        "user_pk": None,
        "role": "member",
        "member_version": 7,
        "roster_version": 7,
    }
    signed = {
        **bare,
        **sign_authority_event(
            event_type=FET.SPACE_MEMBER_JOINED.value,
            space_id=SP,
            payload=strip_authority_sig_fields(bare),
            space_seed=kp.private_key,
        ),
    }
    await _through_the_gates(app, FET.SPACE_MEMBER_JOINED, signed, sender=HOST)
    assert await db.fetchone(
        "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id='u-late'",
        (SP,),
    )


# ── The gate itself, over every type ────────────────────────────────────


@pytest.fixture
async def gate(tmp_dir):
    db = AsyncDatabase(tmp_dir / "archived.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id=SP,
            name="Shared",
            owner_instance_id=HOST,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    yield make_check_space_archived(space_repo=spaces), spaces
    await db.shutdown()


async def _run(step, event_type, *, sender="house-author"):
    ctx = InboundContext()
    ctx.event = FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp="2026-06-01T10:00:00+00:00",
        payload={"author": "u-a"},
        space_id=SP,
    )
    await step(ctx)
    return ctx.early_response


async def test_every_write_type_is_refused_into_an_archived_space(gate):
    """Enumerates the frozenset: a content type added tomorrow is covered
    the moment it is classified as a write."""
    step, spaces = gate
    content = SPACE_WRITE_EVENT_TYPES - ARCHIVED_ALLOWED_REMOVAL_TYPES
    for event_type in SPACE_WRITE_EVENT_TYPES:
        assert await _run(step, event_type) is None, event_type
    await spaces.set_archived(SP, True)
    for event_type in content:
        assert await _run(step, event_type) == ARCHIVED, event_type
        assert await _run(step, event_type, sender=HOST) is None, event_type
    await spaces.set_archived(SP, True, reason="removed")
    for event_type in content:
        assert await _run(step, event_type, sender=HOST) == ARCHIVED, event_type


async def test_the_dissolved_column_alone_freezes_the_space(gate):
    step, spaces = gate
    await spaces.mark_dissolved(SP)
    for event_type in SPACE_WRITE_EVENT_TYPES - ARCHIVED_ALLOWED_REMOVAL_TYPES:
        assert await _run(step, event_type, sender=HOST) == ARCHIVED, event_type


async def test_removals_pass_the_gate_in_every_archived_state(gate):
    step, spaces = gate
    for archive in (
        lambda: spaces.set_archived(SP, True),
        lambda: spaces.set_archived(SP, True, reason="dissolved"),
        lambda: spaces.mark_dissolved(SP),
    ):
        await archive()
        for event_type in ARCHIVED_ALLOWED_REMOVAL_TYPES:
            assert await _run(step, event_type) is None, event_type


def test_the_removal_set_is_exactly_the_content_deletes():
    """A new ``*_DELETED`` content type is either a removal that propagates
    into an archived copy, or this test fails until somebody decides."""
    deletes = {t for t in SPACE_WRITE_EVENT_TYPES if t.value.endswith("_deleted")}
    assert ARCHIVED_ALLOWED_REMOVAL_TYPES == deletes


async def test_no_reader_type_is_refused_into_an_archived_space(gate):
    """Roster, config (unarchive), dissolve, key epochs, sync, reports…"""
    step, spaces = gate
    await spaces.set_archived(SP, True, reason="dissolved")
    for event_type in SPACE_READER_EVENT_TYPES:
        assert await _run(step, event_type) is None, event_type


def test_every_row_writing_type_has_an_archived_end_to_end_case():
    """Each row-writing space event type is proven refused end-to-end from
    a member household (and the host on a terminated stub)."""
    member = {p.values[0] for p in _MEMBER_WRITES}
    expected = (
        SPACE_WRITE_EVENT_TYPES - NOT_ROW_SCOPED.keys() - ARCHIVED_ALLOWED_REMOVAL_TYPES
    )
    assert not expected - member, sorted(t.value for t in expected - member)
    removals = {p.values[0] for p in _RIGHTFUL_REMOVALS}
    wrongful = {p.values[0] for p in _WRONGFUL_REMOVALS}
    assert not ARCHIVED_ALLOWED_REMOVAL_TYPES - removals, sorted(
        t.value for t in ARCHIVED_ALLOWED_REMOVAL_TYPES - removals
    )
    assert not ARCHIVED_ALLOWED_REMOVAL_TYPES - wrongful, sorted(
        t.value for t in ARCHIVED_ALLOWED_REMOVAL_TYPES - wrongful
    )


# ── §25.6 sync receiver ──────────────────────────────────────────────────


def _sync(senders_of):
    return [
        pytest.param(r, recs, prov, id=f"{r}: {label} <- {prov}")
        for r, label, recs, allowed, _refused in SYNC_CASES
        if r not in ROSTER_RESOURCES and r not in REMOVAL_RESOURCES
        for prov in senders_of(allowed)
    ]


_MEMBER_SYNC = _sync(lambda allowed: [p for p in allowed if p != HOST])
_HOST_SYNC = _sync(lambda allowed: [p for p in allowed if p == HOST])


async def _stream(app, resource, records, provider):
    await app[space_sync_receiver_key]._dispatch(
        resource, SP, [dict(r) for r in records], provider=provider
    )


@pytest.mark.parametrize(("resource", "records", "provider"), _MEMBER_SYNC)
async def test_a_member_sync_stream_into_an_archived_space_changes_nothing(
    env, resource, records, provider
):
    app, db = env
    await _archive(db)
    before = await _sync_snapshot(db)
    await _stream(app, resource, records, provider)
    after = await _sync_snapshot(db)
    changed = sorted(t for t in _SYNC_TABLES if before[t] != after[t])
    assert not changed, f"{resource} from {provider} wrote {changed}"


@pytest.mark.parametrize(("resource", "records", "provider"), _HOST_SYNC)
async def test_a_host_sync_stream_into_a_terminated_space_changes_nothing(
    env, resource, records, provider
):
    app, db = env
    await _archive(db, "removed")
    before = await _sync_snapshot(db)
    await _stream(app, resource, records, provider)
    assert await _sync_snapshot(db) == before, f"{resource} from the host"


@pytest.mark.parametrize(("resource", "records", "provider"), _HOST_SYNC)
async def test_a_host_sync_stream_still_fills_a_reversible_archive(
    env, resource, records, provider
):
    app, db = env
    await _archive(db)
    before = await _sync_snapshot(db)
    await _stream(app, resource, records, provider)
    assert await _sync_snapshot(db) != before, f"{resource} from the host"


async def test_the_roster_still_syncs_into_a_terminated_space(env):
    app, db = env
    await _archive(db, "dissolved")
    await _stream(
        app,
        "members",
        [{"user_id": "u-new", "role": "member", "joined_at": "2026-06-01"}],
        HOST,
    )
    assert await db.fetchone(
        "SELECT 1 FROM space_members WHERE space_id=? AND user_id='u-new'", (SP,)
    )


@pytest.mark.parametrize("reason", [None, "dissolved", "removed"])
@pytest.mark.parametrize("provider", [HOST, AUTHOR])
async def test_a_streamed_list_delete_still_lands_in_an_archived_space(
    env, reason, provider
):
    """Removals propagate into the snapshot, as the live gate lets
    ``SPACE_TASK_LIST_DELETED`` through: a delete must not outlive itself."""
    app, db = env
    await _archive(db, reason)
    await _stream(
        app, "task_lists_deleted", [{"id": "list-a", "space_id": SP}], provider
    )
    row = await db.fetchone(
        "SELECT deleted_at FROM space_task_lists WHERE id='list-a'", ()
    )
    assert row["deleted_at"] is not None
    assert (
        await db.fetchone("SELECT 1 FROM space_tasks WHERE list_id='list-a'", ())
        is None
    )


def test_every_content_sync_resource_is_classified():
    """Every sync resource is roster (still applies), a removal (still
    applies, with an end-to-end case above) or content (refused into an
    archived space, with an end-to-end case)."""
    assert ROSTER_RESOURCES <= ALLOWED_RESOURCES
    assert REMOVAL_RESOURCES <= ALLOWED_RESOURCES
    content = (
        set(ALLOWED_RESOURCES)
        - ROSTER_RESOURCES
        - REMOVAL_RESOURCES
        - _SYNC_NOT_WRITTEN.keys()
    )
    covered = {p.values[0] for p in _MEMBER_SYNC} | {p.values[0] for p in _HOST_SYNC}
    assert not content - covered, sorted(content - covered)


# ── The gate is in the shipped chain: a mesh-routed write ────────────────


class _RoutedTarget(_MemberTargetFed):
    """The routed handler's view of the target, with the REAL post-decrypt
    gate chain of the app's own :class:`FederationService`."""

    def __init__(self, real_fed) -> None:
        super().__init__("victim-instance")
        self._real = real_fed

    def post_decrypt_gate_steps(self, *, include_ban_check: bool = False) -> list:
        return self._real.post_decrypt_gate_steps(include_ban_check=include_ban_check)


async def _route_a_post(app, db, *, post_id: str) -> list:
    member = generate_identity_keypair()
    member_id = derive_instance_id(member.public_key)
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,?,?)",
        (SP, member_id, f"u-{post_id}", "member"),
    )
    fed = _RoutedTarget(app[federation_service_key])
    fed.identity_pks[member_id] = member.public_key
    dispatched: list = []

    async def _dispatch(ev) -> None:
        dispatched.append(ev)

    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    handler = SpaceRoutedHandler(
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
        event_dispatcher=_dispatch,
        target_eph_lookup=lambda pub: target_priv if pub == target_pub else None,
    )
    path = [member_id, "relay-instance", fed.own_instance_id]
    sealed = _seal(
        {"space_id": SP, "id": post_id, "author": f"u-{post_id}", "type": "text"},
        target_pub=target_pub,
    )
    sealed["origin_identity_pk"] = member.public_key.hex()
    sealed["origin_sig_suite"] = routed_crypto.ROUTED_ORIGIN_SIG_SUITE_ED25519
    sealed["origin_sig"] = routed_crypto.sign_routed_origin(
        seed=member.private_key,
        route_id=ROUTE_ID,
        direction="forward",
        path=path,
        inner_event_type=INNER_EVENT,
        sealed=sealed,
    )
    await handler._on_routed(
        FederationEvent(
            msg_id=f"m-{post_id}",
            event_type=FET.SPACE_ROUTED,
            from_instance="relay-instance",
            to_instance=fed.own_instance_id,
            timestamp="2026-09-19T00:00:00Z",
            payload={
                "route_id": ROUTE_ID,
                "path": path,
                "position": 1,
                "direction": "forward",
                "inner_event_type": INNER_EVENT,
                "sealed": sealed,
            },
        )
    )
    return dispatched


async def test_a_mesh_routed_write_into_an_archived_space_is_never_dispatched(env):
    """Through the real ``SPACE_ROUTED`` unwrap and the app's real gate
    chain: the same signed, seated write is dispatched while the space is
    live and refused once it is archived."""
    app, db = env
    assert len(await _route_a_post(app, db, post_id="p-live")) == 1
    await _archive(db)
    assert await _route_a_post(app, db, post_id="p-frozen") == []

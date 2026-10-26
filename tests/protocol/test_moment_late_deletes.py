"""Release-blocker protocol tests: a moment's delete reaches every copy,
even later (v_66, owner decision 6c, sign-offs 3 + 4 of 2026-10-25).

Marked ``@pytest.mark.security``.

A moment may live 30 days at a follower (v_65), so its delete has to arrive
after a long absence too — on both paths:

* **Paired households (mesh).** A ``MOMENT_DELETED`` keeps retrying on the
  outbox's ceiling backoff for 30 days instead of giving up after ~10 h, so
  a household offline for days still gets it; past 30 days it goes. A
  household relaying for another origin keeps at most 100 of that origin's
  deletes per peer that long (adversarial review F2).
* **GFS followers.** The author household hands the GFS the delete's signed
  certificate (the mesh delete's origin signature — no content), retrying a
  failed post for a day (F3); the GFS keeps it 30 days and pushes it to a
  follower household when it reconnects, only for authors that household
  follows, ≤ 200 a push and ≤ 100 of one author, past a per-author delivery
  cursor — so one author's flood never crowds out another's delete (F1).
  The follower checks it against the key it pinned when it followed — a
  forged certificate, another household's, an unknown suite or one older
  than a moment can live changes nothing — and a replay changes nothing.

Everything here runs against real SQLite (household and GFS migrations).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import MomentDeleted
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_retention import (
    MAX_LONG_LIVED_RELAYED_PER_ORIGIN,
    moment_delete_key,
)
from socialhome.domain.moment import Moment
from socialhome.global_server.moment_delete_log import (
    DELETE_CERTS_PER_AUTHOR_PER_PUSH,
    DELETE_LOG_TTL_S,
    MomentDeleteLog,
)
from socialhome.global_server.repositories import (
    SqliteGfsMomentDeleteLogRepo,
    SqliteGfsMomentFollowRepo,
)
from socialhome.global_server.routes.moments_public import DELETE_CERT_MAX_AGE_S
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.outbox_processor import (
    MAX_ATTEMPTS,
    DeliveryOutcome,
    OutboxProcessor,
)
from socialhome.repositories.moment_public_repo import SqliteMomentPublicFollowRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.services.moment_delete_cert_retry import (
    DELETE_CERT_RETRY_WINDOW_S,
    classify_cert_status,
)
from socialhome.services.moment_public_inbound import MomentPublicInbound
from tests.moment_public_wire import StubMomentKeys, delete_cert, now_iso

pytestmark = pytest.mark.security

FET = FederationEventType
AUTHOR = generate_identity_keypair()
ORIGIN = derive_instance_id(AUTHOR.public_key)
MALLORY = generate_identity_keypair()

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


# ── Paired households: the outbox keeps a delete 30 days ───────────────


async def _peer(db, instance_id: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status) VALUES(?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            "http://x",
            "in",
            "confirmed",
        ),
    )


async def _status(db, entry_id: str) -> tuple[str, int]:
    row = await db.fetchone(
        "SELECT status, attempts FROM federation_outbox WHERE id=?", (entry_id,)
    )
    return str(row["status"]), int(row["attempts"])


async def test_a_delete_reaches_a_household_offline_for_days(db):
    """Five days of failed deliveries (13 ladder steps, then the 4 h
    ceiling): the moment's create has long been given up, its delete is
    still queued — and arrives when the household is back."""
    await _peer(db, "peer-away")
    repo = SqliteOutboxRepo(db)
    deleted = await repo.enqueue(
        instance_id="peer-away", event_type=FET.MOMENT_DELETED, payload_json="{}"
    )
    created = await repo.enqueue(
        instance_id="peer-away", event_type=FET.MOMENT_CREATED, payload_json="{}"
    )
    assert deleted and created

    async def deliver(entry):
        return DeliveryOutcome.TRANSIENT

    proc = OutboxProcessor(repo, deliver)
    # Every retry made due at once: 13 ladder steps + 30 ceiling steps of
    # 4 h ≈ 5 days of trying.
    for _ in range(MAX_ATTEMPTS + 30):
        await db.enqueue(
            "UPDATE federation_outbox SET next_attempt_at=? WHERE status='pending'",
            (datetime.now(UTC).isoformat(),),
        )
        await proc.drain_once()
    assert (await _status(db, created))[0] == "failed"
    status, attempts = await _status(db, deleted)
    assert status == "pending" and attempts == MAX_ATTEMPTS + 30
    await db.enqueue(
        "UPDATE federation_outbox SET next_attempt_at=? WHERE status='pending'",
        (datetime.now(UTC).isoformat(),),
    )
    delivered: list[str] = []

    async def deliver_back(entry):
        delivered.append(entry.id)
        return DeliveryOutcome.SUCCESS

    proc = OutboxProcessor(repo, deliver_back)
    await proc.drain_once()
    # Delivered (a delivered row is removed from the queue).
    assert delivered == [deleted]
    assert (
        await db.fetchone("SELECT 1 FROM federation_outbox WHERE id=?", (deleted,))
        is None
    )


async def test_a_delete_stops_after_thirty_days(db):
    """Not forever: past its 30 days the retention sweep ends it."""
    await _peer(db, "peer-gone")
    repo = SqliteOutboxRepo(db)
    deleted = await repo.enqueue(
        instance_id="peer-gone", event_type=FET.MOMENT_DELETED, payload_json="{}"
    )
    row = await db.fetchone(
        "SELECT expires_at FROM federation_outbox WHERE id=?", (deleted,)
    )
    ends = datetime.fromisoformat(row["expires_at"])
    assert abs((ends - datetime.now(UTC)) - timedelta(days=30)) < timedelta(minutes=1)
    expired = await repo.expire_past_retention(
        (ends + timedelta(seconds=1)).isoformat()
    )
    assert expired == 1 and (await _status(db, deleted))[0] == "failed"


async def test_a_relaying_household_cannot_pin_a_peers_queue_for_a_month(db):
    """Adversarial review F2: a paired household signs deletes for made-up
    ids under its own origin; each is new here, so we relay it — to a peer
    that is offline. Only MAX_LONG_LIVED_RELAYED_PER_ORIGIN of them keep
    retrying for 30 days; the rest give up like any ordinary event. Our own
    deletes keep their 30 days whatever that household floods."""
    await _peer(db, "peer-away")
    repo = SqliteOutboxRepo(db)
    MD = FET.MOMENT_DELETED
    flood = MAX_LONG_LIVED_RELAYED_PER_ORIGIN + 20
    for i in range(flood):
        await repo.enqueue(
            instance_id="peer-away",
            event_type=MD,
            payload_json="{}",
            supersede_key=moment_delete_key(
                MD,
                {"moment_id": f"fake-{i}", "origin_instance_id": "inst-mallory"},
                own_instance_id=ORIGIN,
            ),
        )
    ours = await repo.enqueue(
        instance_id="peer-away",
        event_type=MD,
        payload_json="{}",
        supersede_key=moment_delete_key(
            MD,
            {"moment_id": "m-ours", "origin_instance_id": ORIGIN},
            own_instance_id=ORIGIN,
        ),
    )

    async def deliver(entry):
        return DeliveryOutcome.TRANSIENT

    proc = OutboxProcessor(repo, deliver)
    for _ in range(MAX_ATTEMPTS + 2):
        await db.enqueue(
            "UPDATE federation_outbox SET next_attempt_at=? WHERE status='pending'",
            (datetime.now(UTC).isoformat(),),
        )
        while await proc.drain_once():
            pass
    pending = await db.fetchval(
        "SELECT COUNT(*) FROM federation_outbox WHERE status='pending' AND id<>?",
        (ours,),
    )
    assert pending == MAX_LONG_LIVED_RELAYED_PER_ORIGIN
    assert (await _status(db, ours))[0] == "pending"


# ── GFS followers: signed delete certificates ──────────────────────────


async def _follower(db, *, pk_hex: str = AUTHOR.public_key.hex()):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'u1', 'U1', 'active')"
    )
    for gid in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', ?,"
            " 'https://g.example', 'active', datetime('now'))",
            (gid, gid, "aa" * 32),
        )
    follows = SqliteMomentPublicFollowRepo(db)
    await follows.upsert(
        follower_user_id="u1",
        followed_user_id="u-dana",
        gfs_id="g1",
        followed_instance_pk=pk_hex,
        followed_username="dana",
        followed_display_name="Dana",
    )
    bus = EventBus()
    deleted: list[MomentDeleted] = []

    async def _on(event: MomentDeleted) -> None:
        deleted.append(event)

    bus.subscribe(MomentDeleted, _on)
    moments = SqliteMomentRepo(db)
    inbound = MomentPublicInbound(
        bus=bus,
        moment_repo=moments,
        follow_repo=follows,
        key_inbound=StubMomentKeys(),
    )
    return SimpleNamespace(inbound=inbound, moments=moments, deleted=deleted)


async def _hold(env, moment_id: str = "m-1", *, author: str = "u-dana") -> None:
    await env.moments.save(
        Moment(
            id=moment_id,
            author_user_id=author,
            content="the plum tree",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id=ORIGIN,
            created_at=now_iso(hours=-48),
            expires_at=now_iso(hours=24 * 20),
            is_public=True,
            received_via="gfs",
            received_via_gfs_id="g1",
        )
    )


def _cert(moment_id: str = "m-1", *, key=AUTHOR, **over) -> dict:
    return delete_cert(
        key.private_key,
        key.public_key,
        moment_id=moment_id,
        author_user_id=over.pop("author_user_id", "u-dana"),
        origin_instance_id=over.pop("origin", derive_instance_id(key.public_key)),
        occurred_at=over.pop("occurred_at", now_iso(hours=-24 * 4)),
    )


async def _push(env, certs: list, gfs_id: str = "g1") -> None:
    await env.inbound.handle(
        {"type": "moment_delete_certs", "certs": certs}, gfs_id=gfs_id
    )


async def _live(env, moment_id: str = "m-1") -> bool:
    row = await env.moments.get(moment_id, include_deleted=True)
    return row is not None and row.deleted_at is None


async def test_a_certificate_removes_the_moment_and_a_replay_changes_nothing(db):
    env = await _follower(db)
    await _hold(env)
    cert = _cert()
    await _push(env, [cert])
    assert not await _live(env)
    assert [e.moment_id for e in env.deleted] == ["m-1"]
    # The GFS pushes it again on the next reconnect: nothing changes.
    await _push(env, [cert, cert])
    assert len(env.deleted) == 1


async def test_a_forged_certificate_changes_nothing(db):
    """Signed by a key other than the one pinned for the author — even one
    whose id derives correctly — is refused."""
    env = await _follower(db)
    await _hold(env)
    await _push(env, [_cert(key=MALLORY), _cert(key=MALLORY, origin=ORIGIN)])
    tampered = {**_cert(), "moment_id": "m-1"}
    tampered["origin_sig"] = _cert(moment_id="m-other")["origin_sig"]
    await _push(env, [tampered])
    assert await _live(env) and env.deleted == []


async def test_a_certificate_for_another_author_changes_nothing(db):
    """Genuinely signed by the followed household, but naming a moment held
    under another author — the stored row wins; and an author not followed
    through this GFS gets nothing applied."""
    env = await _follower(db)
    await _hold(env, "m-sam", author="u-sam")
    await _push(env, [_cert("m-sam")])
    assert await _live(env, "m-sam")
    await _hold(env)
    await _push(env, [_cert(author_user_id="u-sam")])  # nobody follows u-sam
    await _push(env, [_cert()], gfs_id="g2")  # u-dana not followed via g2
    assert await _live(env) and env.deleted == []


async def test_an_unknown_suite_changes_nothing(db):
    env = await _follower(db)
    await _hold(env)
    await _push(env, [{**_cert(), "origin_sig_suite": "ed25519+mldsa65"}])
    assert await _live(env)


async def test_a_certificate_older_than_a_moment_can_live_changes_nothing(db):
    env = await _follower(db)
    await _hold(env)
    await _push(env, [_cert(occurred_at=now_iso(hours=-24 * 32))])
    await _push(env, [_cert(occurred_at=now_iso(hours=2))])
    assert await _live(env)


async def test_a_certificate_for_a_moment_never_held_is_kept_as_a_tombstone(db):
    """A create that arrives later (another path, a replay) is refused."""
    env = await _follower(db)
    await _push(env, [_cert("m-unseen")])
    tomb = await env.moments.get("m-unseen", include_deleted=True)
    assert tomb is not None and tomb.deleted_at is not None
    assert env.deleted == []


# ── The GFS log: TTL, cap, only followed authors, end to end ───────────


class _Socket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


@pytest.fixture
async def gfs(tmp_dir):
    db = AsyncDatabase(
        tmp_dir / "gfs.db", migrations_dir=_GFS_MIGRATIONS, batch_timeout_ms=10
    )
    await db.startup()
    for iid in (ORIGIN, "inst-follower", "inst-sam"):
        await db.enqueue(
            "INSERT INTO client_instances(instance_id, display_name, public_key,"
            " status) VALUES(?, ?, 'aa', 'active')",
            (iid, iid),
        )
    for uid, iid in (("u-dana", ORIGIN), ("u-sam", "inst-sam")):
        await db.enqueue(
            "INSERT INTO gfs_user_registrations(user_id, instance_id, username,"
            " display_name, home_instance_pk, registered_at, status)"
            " VALUES(?, ?, ?, ?, 'aa', 1, 'active')",
            (uid, iid, uid, uid),
        )
    await db.enqueue(
        "INSERT INTO gfs_moment_follows(follower_user_id, follower_instance_id,"
        " followed_user_id, created_at) VALUES('u1', 'inst-follower', 'u-dana', 1)"
    )
    ws = GfsWebSocketRegistry()
    sock = _Socket()
    await ws.register("inst-follower", sock)  # type: ignore[arg-type]
    log = MomentDeleteLog(
        SqliteGfsMomentDeleteLogRepo(db), SqliteGfsMomentFollowRepo(db), ws
    )
    yield SimpleNamespace(db=db, log=log, sock=sock)
    await db.shutdown()


def _frames(sock: _Socket) -> list[dict]:
    return [orjson.loads(s) for s in sock.sent]


async def test_an_offline_follower_catches_up_through_the_gfs(db, gfs):
    """The follower was away four days: the live delete and the 24 h queue
    missed it. On reconnect the GFS pushes the certificate; the moment goes."""
    env = await _follower(db)
    await _hold(env)
    away = int(time.time()) - 4 * 86_400
    await gfs.db.enqueue(
        "UPDATE client_instances SET relay_seen_at=? WHERE instance_id='inst-follower'",
        (away,),
    )
    assert await gfs.log.keep(author_user_id="u-dana", cert=_cert()) == "kept"
    assert await gfs.log.keep(author_user_id="u-dana", cert=_cert()) == "already"
    assert await gfs.log.push_to("inst-follower") == 1
    for frame in _frames(gfs.sock):
        await env.inbound.handle(frame, gfs_id="g1")
    assert not await _live(env)


async def test_the_gfs_pushes_only_followed_authors_within_thirty_days_capped(gfs):
    now = int(time.time())
    repo = SqliteGfsMomentDeleteLogRepo(gfs.db)
    await repo.add(author_user_id="u-sam", moment_id="m-sam", cert="{}", now=now)
    await repo.add(
        author_user_id="u-dana",
        moment_id="m-old",
        cert='{"moment_id": "m-old"}',
        now=now - DELETE_LOG_TTL_S - 1,
    )
    total = DELETE_CERTS_PER_AUTHOR_PER_PUSH + 5
    for i in range(total):
        await repo.add(
            author_user_id="u-dana",
            moment_id=f"m{i:04d}",
            cert=orjson.dumps({"moment_id": f"m{i:04d}"}).decode(),
            now=now - 1000 + i,
        )
    # One push carries one author's fair share…
    assert await gfs.log.push_to("inst-follower", now=now) == (
        DELETE_CERTS_PER_AUTHOR_PER_PUSH
    )
    # …and the rest is still owed after the household is marked seen
    # (adversarial review F1: never skipped by the seen mark).
    await gfs.db.enqueue(
        "UPDATE client_instances SET relay_seen_at=? WHERE instance_id=?",
        (now + 60, "inst-follower"),
    )
    assert await gfs.log.push_to("inst-follower", now=now + 7200) == 5
    ids = [c["moment_id"] for f in _frames(gfs.sock) for c in f["certs"]]
    assert sorted(ids) == [f"m{i:04d}" for i in range(total)]
    assert "m-sam" not in ids and "m-old" not in ids
    # Pruned past its 30 days, never pushed again.
    assert await gfs.log.prune(now) == 1


async def test_a_flood_by_one_followed_author_never_loses_anothers_delete(db, gfs):
    """Adversarial review F1: a followed author posts certificates for
    hundreds of made-up ids while the follower is away; the real delete of
    another followed author still reaches it — in the first push, and even
    after the household is marked seen."""
    now = int(time.time())
    repo = SqliteGfsMomentDeleteLogRepo(gfs.db)
    await gfs.db.enqueue(
        "INSERT INTO gfs_moment_follows(follower_user_id, follower_instance_id,"
        " followed_user_id, created_at) VALUES('u2', 'inst-follower', 'u-sam', 1)"
    )
    await gfs.db.enqueue(
        "UPDATE client_instances SET relay_seen_at=? WHERE instance_id=?",
        (now - 10 * 86_400, "inst-follower"),
    )
    for i in range(300):
        await repo.add(
            author_user_id="u-sam",
            moment_id=f"fake-{i:03d}",
            cert=orjson.dumps({"moment_id": f"fake-{i:03d}"}).decode(),
            now=now - 86_400 + i,
        )
    env = await _follower(db)
    await _hold(env)
    await repo.add(
        author_user_id="u-dana",
        moment_id="m-1",
        cert=orjson.dumps(_cert()).decode(),
        now=now - 86_400 + 400,
    )
    await gfs.log.push_to("inst-follower", now=now)
    first = _frames(gfs.sock)[0]
    assert "m-1" in [c["moment_id"] for c in first["certs"]]
    for frame in _frames(gfs.sock):
        await env.inbound.handle(frame, gfs_id="g1")
    assert not await _live(env)
    # The flood's remainder is still owed after the seen mark moves on.
    await gfs.db.enqueue(
        "UPDATE client_instances SET relay_seen_at=? WHERE instance_id=?",
        (now + 60, "inst-follower"),
    )
    for k in range(5):
        await gfs.log.push_to("inst-follower", now=now + 7200 + k)
    ids = {c["moment_id"] for f in _frames(gfs.sock) for c in f["certs"]}
    assert {f"fake-{i:03d}" for i in range(300)} <= ids


def test_a_certificate_retried_for_a_day_is_still_fresh_at_the_gfs():
    """Adversarial review F3: the author household retries a failed
    certificate post for a day; the GFS takes one at least that old (with
    slack), and a 404 / 422 from an old node mid-upgrade is retried."""
    assert DELETE_CERT_MAX_AGE_S >= DELETE_CERT_RETRY_WINDOW_S + 3600
    for status in (404, 422, 429, 503):
        assert classify_cert_status(status, None).kind == "transient"
    for status in (403, 409):
        assert classify_cert_status(status, None).kind == "permanent"

"""Tests for :class:`MomentPublicOutbound`.

Verifies the bus subscriber path: a local public moment fires the
signed, encrypted POST per registered GFS that proved
``encrypted_moments`` (v_62), while non-public / GFS-received /
remote-author events are skipped — and an unproven GFS gets nothing.
"""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.events import (
    MediaTranscodeFailed,
    MediaTranscodeReady,
    MomentCreated,
    MomentDeleted,
    UserDeprovisioned,
    UserProvisioned,
)
from socialhome.domain.moment import Moment
from socialhome.infrastructure.event_bus import EventBus
from socialhome.moment_origin import DELETE_CERT_FIELDS, delete_cert_refusal
from socialhome.moment_public_seal import (
    ENVELOPE_FIELDS,
    item_key,
    open_body,
    verify_wire,
)
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_media_outbound import PreparedMedia
from socialhome.services.moment_public_outbound import MomentPublicOutbound


class _StubResp:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self.status)


KEY = os.urandom(32)
GEN = 1_700_000_000


class _Keys:
    """The author's moment content key (``MomentPublicKeyOutbound``)."""

    async def publish_key(self, author_user_id: str) -> tuple[int, int, bytes]:
        """What a publish seals under (rotating first when one is due)."""
        return GEN, 3, KEY


class _Gfs:
    """Which connections proved ``encrypted_moments`` — and (v_66)
    ``moment_delete_log``."""

    def __init__(self, proven: set[str], delete_log: set[str] | None = None) -> None:
        self.proven = proven
        self.delete_log = delete_log or set()

    async def encrypted_moments_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven

    async def moment_delete_log_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.delete_log


def _opened(envelope: dict) -> dict:
    """The sealed body of an envelope, opened with the author's key."""
    assert set(envelope) == ENVELOPE_FIELDS
    return open_body(step_key=KEY, envelope=envelope)


@pytest.fixture
async def outbound_setup(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    # Register two GFSes so we can assert fan-out hits both.
    for gfs_id, host in (("g1", "gfs1"), ("g2", "gfs2")):
        await db.enqueue(
            "INSERT INTO gfs_connections("
            "id, gfs_instance_id, display_name, public_key, inbox_url, "
            "status, paired_at) VALUES(?,?,?,'ff'*32,?,'active', datetime('now'))",
            (gfs_id, gfs_id, gfs_id.upper(), f"https://{host}.example"),
        )
    bus = EventBus()
    moment_repo = SqliteMomentRepo(db)
    reg_repo = SqliteMomentPublicRegistrationRepo(db)
    user_repo = SqliteUserRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=moment_repo,
        registration_repo=reg_repo,
        user_repo=user_repo,
        gfs_repo=gfs_repo,
        key_outbound=_Keys(),
        gfs_service=_Gfs({"g1", "g2"}),
    )
    sess = _StubSession()
    sub.attach_session(sess)
    sub.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    sub.wire()
    return {
        "bus": bus,
        "moments": moment_repo,
        "regs": reg_repo,
        "session": sess,
        "sub": sub,
        "gfs": sub._gfs_service,
    }


def _moment(
    *,
    moment_id: str = "m-1",
    is_public: bool = True,
    received_via: str = "self",
    received_via_gfs_id: str | None = None,
    parent_moment_id: str | None = None,
) -> Moment:
    return Moment(
        id=moment_id,
        author_user_id="u1",
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=parent_moment_id,
        origin_instance_id="inst-self",
        created_at="2026-05-06T12:00:00Z",
        expires_at="2026-05-07T12:00:00Z",
        is_public=is_public,
        received_via=received_via,
        received_via_gfs_id=received_via_gfs_id,
    )


def _created_event(moment_id: str = "m-1") -> MomentCreated:
    return MomentCreated(
        moment_id=moment_id,
        author_user_id="u1",
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-self",
        expires_at="2026-05-07T12:00:00Z",
    )


async def test_public_local_moment_fans_to_each_registered_gfs(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    await s["bus"].publish(_created_event())
    # Two POSTs, one to each GFS — each carries a signed envelope
    # ending in /gfs/moments/publish.
    assert len(s["session"].posts) == 2
    urls = {p[0] for p in s["session"].posts}
    assert any(u.endswith("/gfs/moments/publish") for u in urls)
    envelope = s["session"].posts[0][1]
    assert envelope["author_user_id"] == "u1"
    assert (envelope["moment_id"], envelope["key_gen"], envelope["key_step"]) == (
        "m-1",
        GEN,
        3,
    )
    assert "signature" in envelope
    # Nothing of the moment's content in the clear — routing fields only.
    assert "hello" not in str(envelope) and "Alice" not in str(envelope)
    body = _opened(envelope)
    assert (body["op"], body["content"]) == ("create", "hello")
    assert body["author_user_id"] == "u1"
    # v_64: the signing time rides sealed (signed with the ciphertext).
    assert isinstance(body["sent_at"], int)
    assert abs(body["sent_at"] - time.time()) < 5
    # ONE ciphertext: both servers get the same bytes.
    assert s["session"].posts[1][1] == envelope


async def test_the_profile_rides_only_when_it_changed(outbound_setup, db):
    """Every moment names the profile_rev; the profile itself rides the
    first moment and then only when it changed."""
    s = outbound_setup
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    for mid in ("m-1", "m-2"):
        await s["moments"].save(_moment(moment_id=mid))
        await s["bus"].publish(_created_event(mid))
    await db.enqueue("UPDATE users SET bio='Beekeeper.' WHERE user_id='u1'")
    await s["moments"].save(_moment(moment_id="m-3"))
    await s["bus"].publish(_created_event("m-3"))
    first, second, third = [_opened(b) for _u, b in s["session"].posts]
    assert first["profile"]["display_name"] == "Alice"
    assert "profile" not in second
    assert first["profile_rev"] == second["profile_rev"]
    assert third["profile"]["bio"] == "Beekeeper."
    assert third["profile_rev"] != second["profile_rev"]


async def test_a_gfs_without_encrypted_moments_gets_nothing(outbound_setup, caplog):
    """No plaintext fallback: an unproven server is sent no public moment,
    with one WARNING per server."""
    s = outbound_setup
    s["gfs"].proven = {"g1"}
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    await s["bus"].publish(_created_event())
    await s["bus"].publish(_created_event())
    assert {u for u, _ in s["session"].posts} == {
        "https://gfs1.example/gfs/moments/publish"
    }
    warned = [r for r in caplog.records if "encrypted_moments" in r.getMessage()]
    assert len(warned) == 1


async def test_non_public_moment_does_not_fan(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment(is_public=False))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_a_public_reply_to_a_household_moment_is_not_published(outbound_setup):
    """Defence in depth: a public reply never goes to a GFS once the moment
    it answers isn't public. The DB refuses storing such a reply (migration
    0123), so the parent loses ``is_public`` after the reply is stored —
    e.g. an inbound upsert of a moment first held from a GFS."""
    s = outbound_setup
    await s["moments"].save(_moment(moment_id="root"))
    await s["moments"].save(_moment(moment_id="r-1", parent_moment_id="root"))
    await s["moments"].save(_moment(moment_id="root", is_public=False))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event("r-1"))
    assert s["session"].posts == []


async def test_a_public_reply_to_a_public_moment_is_published(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment(moment_id="root"))
    await s["moments"].save(_moment(moment_id="r-1", parent_moment_id="root"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event("r-1"))
    assert len(s["session"].posts) == 1


async def test_gfs_received_moment_does_not_re_fan(outbound_setup):
    s = outbound_setup
    # Same author, but the row is tagged as received-via-gfs.
    await s["moments"].save(_moment(received_via="gfs", received_via_gfs_id="g1"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_remote_author_event_is_skipped(outbound_setup):
    s = outbound_setup
    # Moment for a user that doesn't exist locally.
    await s["moments"].save(
        Moment(
            id="m-2",
            author_user_id="u-remote",
            content="hi",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-remote",
            created_at="2026-05-06T12:00:00Z",
            expires_at="2026-05-07T12:00:00Z",
            is_public=True,
        )
    )
    event = MomentCreated(
        moment_id="m-2",
        author_user_id="u-remote",
        content="hi",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-remote",
        expires_at="2026-05-07T12:00:00Z",
    )
    await s["bus"].publish(event)
    assert s["session"].posts == []


async def test_no_registration_means_no_fan(outbound_setup):
    s = outbound_setup
    await s["moments"].save(_moment())
    # No regs registered.
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_delete_event_fans_tombstone_to_each_registered_gfs(outbound_setup):
    s = outbound_setup
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    delete = MomentDeleted(
        moment_id="m-1",
        author_user_id="u1",
        origin_instance_id="inst-self",
        is_public=True,
    )
    await s["bus"].publish(delete)
    assert len(s["session"].posts) == 2
    assert all(p[0].endswith("/gfs/moments/delete") for p in s["session"].posts)
    envelope = s["session"].posts[0][1]
    assert envelope["moment_id"] == "m-1"
    body = _opened(envelope)
    sent_at = body.pop("sent_at")
    assert body == {
        "op": "delete",
        "author_user_id": "u1",
        "origin_instance_id": "inst-self",
    }
    # v_64: when it was signed, inside the signed ciphertext.
    assert isinstance(sent_at, int) and abs(sent_at - time.time()) < 5


async def test_a_private_moments_delete_reaches_no_gfs(outbound_setup):
    """v_66: a moment no GFS ever carried is not named to one when it goes —
    neither the sealed delete nor a certificate."""
    s = outbound_setup
    s["gfs"].delete_log = {"g1", "g2"}
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(
        MomentDeleted(
            moment_id="m-1", author_user_id="u1", origin_instance_id="inst-self"
        )
    )
    assert s["session"].posts == []


async def test_a_delete_certificate_goes_only_where_the_log_is_proven(
    outbound_setup,
):
    """v_66: the delete's signed certificate goes to each GFS that proved
    ``moment_delete_log`` — one signed body, the same for each — and a GFS
    that didn't gets only the sealed delete. The certificate is the mesh
    MOMENT_DELETED's origin signature: it verifies under the household's
    identity key and names nothing but who deleted which moment when."""
    s = outbound_setup
    s["gfs"].delete_log = {"g2"}
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    seed = b"\x01" * 32
    pk = ed25519_public_key(seed)
    s["sub"].attach_identity(own_instance_id=derive_instance_id(pk), signing_key=seed)
    await s["bus"].publish(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="u1",
            origin_instance_id="ignored",
            is_public=True,
        )
    )
    certs = [p for p in s["session"].posts if p[0].endswith("/delete_cert")]
    assert [u for u, _ in certs] == ["https://gfs2.example/gfs/moments/delete_cert"]
    body = certs[0][1]
    assert set(body) == {"instance_id", "cert", "signature"}
    assert verify_wire(body, pk.hex())
    cert = body["cert"]
    assert set(cert) == DELETE_CERT_FIELDS
    assert cert["moment_id"] == "m-1" and cert["author_user_id"] == "u1"
    assert cert["origin_instance_id"] == derive_instance_id(pk)
    assert delete_cert_refusal(cert, identity_pk_hex=pk.hex()) is None
    # Both servers still got the sealed delete.
    assert sum(u.endswith("/gfs/moments/delete") for u, _ in s["session"].posts) == 2


async def test_outbound_used_before_attach_skips_silently(db):
    """Bus event that fires before ``attach_session`` shouldn't crash."""
    bus = EventBus()
    sub = MomentPublicOutbound(
        bus=bus,
        moment_repo=SqliteMomentRepo(db),
        registration_repo=SqliteMomentPublicRegistrationRepo(db),
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
        key_outbound=_Keys(),
        gfs_service=_Gfs({"g1"}),
    )
    sub.wire()
    # No session attached — should be a no-op even if the moment exists.
    await bus.publish(
        MomentCreated(
            moment_id="m-x",
            author_user_id="ghost",
            content="",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="",
            expires_at="2026-05-07T12:00:00Z",
        )
    )


class _ProtectedCp:
    """Stub ChildProtectionService: every listed user is protected."""

    def __init__(self, protected: set[str]) -> None:
        self._protected = protected

    def register_gate(self, gate):
        pass

    async def is_restricted(self, user_id, capability):
        return user_id in self._protected


async def test_protected_author_moment_is_not_published(outbound_setup):
    """§CP.R at use time: a registration made before the account was
    protected no longer publishes its moments to the connection server."""
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp({"u1"}))
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []


async def test_protected_author_can_still_retract_a_moment(outbound_setup):
    """Deletes keep flowing so earlier public moments can be withdrawn."""
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp({"u1"}))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="u1",
            origin_instance_id="inst-self",
            is_public=True,
        )
    )
    assert len(s["session"].posts) == 1
    assert s["session"].posts[0][0].endswith("/gfs/moments/delete")


async def test_unprotected_author_still_publishes_with_cp_wired(outbound_setup):
    s = outbound_setup
    s["sub"].attach_child_protection(_ProtectedCp(set()))
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert len(s["session"].posts) == 1


async def test_envelope_carries_the_directory_bio_and_mirrored_picture(
    outbound_setup, db
):
    """The bio and picture digest ride the signed envelope — only what the
    GFS directory already holds: the digest only to a GFS that mirrors the
    current picture."""
    s = outbound_setup
    await db.enqueue(
        "UPDATE users SET bio='Beekeeper.', picture_hash='0123456789abcdef'"
        " WHERE user_id='u1'"
    )
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    await s["regs"].set_last_picture_digest(
        user_id="u1", gfs_id="g1", digest="0123456789abcdef"
    )
    await s["regs"].set_last_picture_digest(
        user_id="u1", gfs_id="g2", digest="ffffffffffffffff"
    )
    await s["bus"].publish(_created_event())
    bodies = {
        url.split("//")[1].split(".")[0]: _opened(body)
        for url, body in s["session"].posts
    }
    assert bodies["gfs1"]["profile"]["bio"] == "Beekeeper."
    assert bodies["gfs1"]["profile"]["picture_digest"] == "0123456789abcdef"
    # g2 mirrors an older picture: no digest, so no stale picture shows.
    assert bodies["gfs2"]["profile"]["picture_digest"] is None
    assert bodies["gfs1"]["profile"]["display_name"] == "Alice"


async def test_a_bio_longer_than_a_directory_holds_is_not_sent(outbound_setup, db):
    s = outbound_setup
    await db.enqueue("UPDATE users SET bio=? WHERE user_id='u1'", ("x" * 290,))
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    [(_url, envelope)] = s["session"].posts
    body = _opened(envelope)
    assert body["profile"]["bio"] is None
    assert body["profile"]["picture_digest"] is None


async def test_a_user_added_after_the_first_moment_still_publishes(outbound_setup, db):
    """Regression (federation demo): the local-user set was cached on the
    first event and never re-read, so a member provisioned later never
    published a public moment."""
    s = outbound_setup
    await s["moments"].save(_moment())
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert len(s["session"].posts) == 1
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u2','noor','Noor','active')"
    )
    # Provisioning announces the new member (UserService does).
    await s["bus"].publish(
        UserProvisioned(user_id="u2", username="noor", is_admin=False)
    )
    await s["moments"].save(
        Moment(
            id="m-2",
            author_user_id="u2",
            content="bees",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="inst-self",
            created_at="2026-05-06T12:00:00Z",
            expires_at="2026-05-07T12:00:00Z",
            is_public=True,
        )
    )
    await s["regs"].upsert(user_id="u2", gfs_id="g1")
    await s["bus"].publish(
        MomentCreated(
            moment_id="m-2",
            author_user_id="u2",
            content="bees",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="inst-self",
            expires_at="2026-05-07T12:00:00Z",
        )
    )
    assert len(s["session"].posts) == 2
    assert s["session"].posts[1][1]["author_user_id"] == "u2"


async def test_a_remote_authors_moment_reads_the_users_once_a_minute(outbound_setup):
    """Finding 10: a non-local author's moment no longer re-reads every
    local user each time — at most once a minute, and provisioning keeps
    the set current."""
    s = outbound_setup
    calls = []
    real = s["sub"]._users.list_all

    async def counting():
        calls.append(1)
        return await real()

    s["sub"]._users.list_all = counting  # type: ignore[method-assign]
    for _ in range(5):
        assert not await s["sub"]._is_local("u-remote")
    assert len(calls) == 1
    await s["bus"].publish(
        UserProvisioned(user_id="u-new", username="n", is_admin=False)
    )
    assert await s["sub"]._is_local("u-new")
    await s["bus"].publish(UserDeprovisioned(user_id="u-new", username="n"))
    assert not await s["sub"]._is_local("u-new")
    assert len(calls) == 1


# ── Encrypted media (v_63) ────────────────────────────────────────────────


class _Media:
    """Stand-in for ``MomentPublicMediaOutbound``: records what happens."""

    def __init__(self, *, ready: bool = True, none: bool = False) -> None:
        self.ready = ready
        self.none = none
        self.calls: list[tuple] = []

    async def prepare(self, moment, *, item_key):
        self.calls.append(("prepare", moment.id, item_key))
        if self.none:
            return None
        return PreparedMedia(
            body={
                "blob_id": "b" * 64,
                "media_suite": "aesgcm-256-pad",
                "content_type": "image/webp",
            },
            blobs={"b" * 64: b"blob"},
            ready=self.ready,
        )

    async def upload(self, gfs_id, prepared, *, author_user_id, expires_at):
        self.calls.append(("upload", gfs_id, author_user_id, expires_at))
        return True

    def remember(self, author, moment_id, gfs_id, prepared):
        self.calls.append(("remember", moment_id, gfs_id))

    async def withdraw(self, author, moment_id):
        self.calls.append(("withdraw", moment_id))
        return 1


def _with_media(url: str, media_type: str, duration_ms: int | None = None) -> Moment:
    return dataclasses.replace(
        _moment(), media_url=url, media_type=media_type, duration_ms=duration_ms
    )


async def test_media_is_sealed_once_uploaded_first_and_named_in_the_body(
    outbound_setup,
):
    s = outbound_setup
    media = _Media()
    s["sub"].attach_media(media)
    await s["moments"].save(_with_media("api/media/x.webp", "image"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["regs"].upsert(user_id="u1", gfs_id="g2")
    await s["bus"].publish(_created_event())
    kinds = [c[0] for c in media.calls]
    assert kinds.count("prepare") == 1  # one seal for every GFS
    assert [c[1] for c in media.calls if c[0] == "upload"] == ["g1", "g2"]
    # The media is sealed under the moment's item key.
    assert media.calls[0][2] == item_key(KEY, "m-1")
    body = _opened(s["session"].posts[0][1])
    assert body["media"]["blob_id"] == "b" * 64
    # The author's local reference never travels: it names nothing there.
    assert "media_url" not in body
    assert body["media_type"] == "image"


async def test_a_video_still_transcoding_waits_for_its_file(outbound_setup):
    s = outbound_setup
    media = _Media(ready=False)
    s["sub"].attach_media(media)
    await s["moments"].save(_with_media("api/media/clip.webm", "video", 3000))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert s["session"].posts == []
    media.ready = True
    await s["bus"].publish(
        MediaTranscodeReady(
            output_filename="other.webm",
            thumbnail_filename="other.webp",
            owner_user_id="u1",
        )
    )
    assert s["session"].posts == []
    await s["bus"].publish(
        MediaTranscodeReady(
            output_filename="clip.webm",
            thumbnail_filename="clip.webp",
            owner_user_id="u1",
        )
    )
    assert len(s["session"].posts) == 1
    assert _opened(s["session"].posts[0][1])["media"]["blob_id"] == "b" * 64


async def test_a_failed_transcode_publishes_the_moment_without_its_file(
    outbound_setup,
):
    s = outbound_setup
    s["sub"].attach_media(_Media(ready=False))
    await s["moments"].save(_with_media("api/media/clip.webm", "video", 3000))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    await s["bus"].publish(
        MediaTranscodeFailed(output_filename="clip.webm", owner_user_id="u1")
    )
    [(_, env)] = s["session"].posts
    body = _opened(env)
    assert "media" not in body and body["media_type"] is None


async def test_a_file_we_dont_send_leaves_the_moment_without_media(outbound_setup):
    s = outbound_setup
    s["sub"].attach_media(_Media(none=True))
    await s["moments"].save(_with_media("api/media/x.webp", "image"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    body = _opened(s["session"].posts[0][1])
    assert "media" not in body and body["media_type"] is None


async def test_a_delete_withdraws_the_media(outbound_setup):
    s = outbound_setup
    media = _Media()
    s["sub"].attach_media(media)
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="u1",
            origin_instance_id="inst-self",
            is_public=True,
        )
    )
    assert ("withdraw", "m-1") in media.calls


async def test_a_public_moment_keeps_its_link_material(outbound_setup):
    """the item key, and the media descriptor per GFS that holds the file,
    that a later share link needs."""
    s = outbound_setup
    recorded: list = []

    class _Shares:
        async def record(self, moment, *, item_key, media_by_gfs):
            recorded.append((moment.id, item_key, media_by_gfs))

    s["sub"].attach_shares(_Shares())
    s["sub"].attach_media(_Media())
    await s["moments"].save(_with_media("api/media/x.webp", "image"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    [(mid, key, media_by_gfs)] = recorded
    assert (mid, key) == ("m-1", item_key(KEY, "m-1"))
    assert list(media_by_gfs) == ["g1"]
    assert media_by_gfs["g1"]["blob_id"] == "b" * 64


async def test_a_gfs_that_refused_the_file_is_not_recorded_as_holding_it(
    outbound_setup,
):
    s = outbound_setup
    recorded: list = []

    class _Shares:
        async def record(self, moment, *, item_key, media_by_gfs):
            recorded.append(media_by_gfs)

    class _Refusing(_Media):
        async def upload(self, gfs_id, prepared, *, author_user_id, expires_at):
            return False

    s["sub"].attach_shares(_Shares())
    s["sub"].attach_media(_Refusing())
    await s["moments"].save(_with_media("api/media/x.webp", "image"))
    await s["regs"].upsert(user_id="u1", gfs_id="g1")
    await s["bus"].publish(_created_event())
    assert recorded == [{}]

"""The listing counts in the GFS publish body: inside the signed canonical
bytes, only for a GFS that proved ``listing_counts``."""

from __future__ import annotations

import json

from socialhome.crypto import b64url_decode, generate_identity_keypair, verify_ed25519
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

from . import test_gfs_connection_service as _base

_make_conn = _base._make_conn
_StubSession = _base._StubSession
#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(env, *, counts_cap: bool):
    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-c", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-c",
            name="Chess Zürich",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.REQUEST,
        )
    )
    for uid, role in (("u1", "owner"), ("u2", "member"), ("u3", "subscriber")):
        await space_repo.save_member(
            SpaceMember(space_id="sp-c", user_id=uid, role=role, joined_at="2026")
        )
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
        " tombstoned) VALUES('sp-c', 'p', 'r1', 'member', 0),"
        " ('sp-c', 'p', 'r2', 'subscriber', 0), ('sp-c', 'p', 'r3', 'subscriber', 1)"
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    if counts_cap:
        # As a connect's verified ``/gfs/info`` leaves it: no probe per publish.
        svc._listing_counts["gfs-c"] = True
    return svc, session, kp


def _canonical(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


async def test_the_counts_ride_inside_the_signed_body(env):
    svc, session, kp = await _svc(env, counts_cap=True)
    await svc.publish_space("sp-c", "gfs-c")
    body = dict(session._last_body)  # type: ignore[attr-defined]
    # Members: two local seats + one live remote member; followers: one
    # local + one live remote follower seat (the tombstoned one is gone).
    assert (body["member_count"], body["follower_count"]) == (3, 2)
    # One round-trip: the publish itself, no capability probe.
    assert [c[0] for c in session.calls] == ["POST"]  # type: ignore[attr-defined]
    sig = b64url_decode(body.pop("signature"))
    assert verify_ed25519(kp.public_key, _canonical(body), sig)


async def test_a_tampered_count_fails_the_signature(env):
    svc, session, kp = await _svc(env, counts_cap=True)
    await svc.publish_space("sp-c", "gfs-c")
    body = dict(session._last_body)  # type: ignore[attr-defined]
    sig = b64url_decode(body.pop("signature"))
    for field in ("member_count", "follower_count"):
        tampered = {**body, field: body[field] + 100}
        assert not verify_ed25519(kp.public_key, _canonical(tampered), sig)


async def test_an_older_gfs_gets_the_body_without_counts(env):
    """It folds only the fields it knows into the bytes it verifies, so the
    counts would 403 the whole publish."""
    svc, session, kp = await _svc(env, counts_cap=False)
    await svc.publish_space("sp-c", "gfs-c")
    body = dict(session._last_body)  # type: ignore[attr-defined]
    assert "member_count" not in body
    assert "follower_count" not in body
    sig = b64url_decode(body.pop("signature"))
    assert verify_ed25519(kp.public_key, _canonical(body), sig)


def test_only_a_signed_block_proves_listing_counts():
    """The capability comes from the verified ``/gfs/info`` block only — a
    bare top-level flag (or a block signed by another key) proves nothing."""
    svc = GfsConnectionService(object())  # type: ignore[arg-type]
    conn = _make_conn("g1", public_key=_base._GFS_KP.public_key.hex())
    caps = {"anonymous_publish": True, "listing_counts": True}
    forged = _base._signed_info(
        gfs_instance_id="inst-g1", capabilities=caps, kp=generate_identity_keypair()
    )
    svc._record_capabilities(conn, forged)
    assert not svc._listing_counts.get("g1")
    svc._record_capabilities(conn, {**_base._stripped_info(), "listing_counts": True})
    assert not svc._listing_counts.get("g1")
    svc._record_capabilities(
        conn, _base._signed_info(gfs_instance_id="inst-g1", capabilities=caps)
    )
    assert svc._listing_counts.get("g1") is True


async def test_a_count_republish_skips_the_members_list_refresh(env):
    """A count cannot move the members' GFS list: ``announce=False`` keeps
    the re-publish to the listing servers alone."""
    svc, _session, _kp = await _svc(env, counts_cap=True)
    _db, conn_repo = env
    await conn_repo.publish_space("sp-c", "gfs-c")
    announced: list[str] = []

    async def _listings(space_id: str) -> None:
        announced.append(space_id)

    svc.attach_on_listings_changed(_listings)
    assert await svc.republish_space("sp-c", announce=False) == 1
    assert announced == []
    assert await svc.republish_space("sp-c") == 1
    assert announced == ["sp-c"]

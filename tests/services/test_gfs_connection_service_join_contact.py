"""Which listings carry the owner's join contact, and to which GFS (v_84,
v_96).

A space that takes requests sends it to a GFS that proved ``join_contact``
(as before). A space that takes follows but no requests (invite-only with
followers on, v_96) sends it only to a GFS that proved BOTH
``join_contact`` and ``subscriber_seats`` — an older one would not serve a
contact for an invite-only listing. A space that takes neither sends none
and probes nothing."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

from . import test_gfs_connection_service as _base

_make_conn = _base._make_conn
_StubSession = _base._StubSession
#: The connection-repo fixture, shared with the main suite.
env = _base.env


async def _svc(
    env,
    monkeypatch,
    *,
    join_mode: JoinMode,
    allow_subscribers: bool,
    caps: frozenset[str],
):
    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-c", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(os.urandom(32)))
    authority = generate_identity_keypair()
    await space_repo.save(
        Space(
            id="sp-c",
            name="Chess Zürich",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key=authority.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=allow_subscribers),
            space_type=SpaceType.GLOBAL,
            join_mode=join_mode,
        )
    )
    await space_repo.set_space_seed("sp-c", authority.private_key)
    probed: list[str] = []

    async def _supported(self, conn, name, cache):
        probed.append(name)
        return name in caps

    monkeypatch.setattr(
        GfsConnectionService, "_signed_capability_supported", _supported
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    svc.attach_join_contact_keys("bb" * 32, "kw-sig")
    return svc, session, probed


BOTH = frozenset({"join_contact", "subscriber_seats"})


@pytest.mark.parametrize("allow_subscribers", [False, True])
async def test_a_space_that_takes_requests_keeps_todays_rule(
    env, monkeypatch, allow_subscribers
):
    """``join_contact`` alone is enough — followers on or off."""
    svc, session, probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.REQUEST,
        allow_subscribers=allow_subscribers,
        caps=frozenset({"join_contact"}),
    )
    await svc.publish_space("sp-c", "gfs-c")
    assert session._last_body["join_contact"]["instance_id"] == "alpha.home"  # type: ignore[attr-defined]
    assert "subscriber_seats" not in probed


async def test_a_space_that_only_takes_follows_lists_a_contact_to_a_v96_gfs(
    env, monkeypatch
):
    svc, session, probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
        caps=BOTH,
    )
    assert await svc._wants_join_contact("sp-c")
    await svc.publish_space("sp-c", "gfs-c")
    assert session._last_body["join_contact"]["instance_id"] == "alpha.home"  # type: ignore[attr-defined]
    assert set(probed) >= BOTH


@pytest.mark.parametrize(
    "caps", [frozenset({"join_contact"}), frozenset({"subscriber_seats"})]
)
async def test_a_follows_only_space_sends_no_contact_to_an_older_gfs(
    env, monkeypatch, caps
):
    """An older GFS would drop (or never serve) a contact on an invite-only
    listing: it gets today's body without one."""
    svc, session, _probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
        caps=caps,
    )
    await svc.publish_space("sp-c", "gfs-c")
    assert "join_contact" not in session._last_body  # type: ignore[attr-defined]


async def test_a_space_that_takes_neither_sends_none_and_probes_nothing(
    env, monkeypatch
):
    svc, session, probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=False,
        caps=BOTH,
    )
    assert not await svc._wants_join_contact("sp-c")
    await svc.publish_space("sp-c", "gfs-c")
    assert "join_contact" not in session._last_body  # type: ignore[attr-defined]
    assert "join_contact" not in probed
    assert "subscriber_seats" not in probed


async def test_the_contact_signer_follows_the_same_rule(env, monkeypatch):
    svc, _session, _probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
        caps=BOTH,
    )
    space = await svc._space_repo.get("sp-c")  # type: ignore[union-attr]
    assert (await svc._join_contact_for(space))["instance_id"] == "alpha.home"
    # The same space with followers turned off: no contact.
    closed = replace(space, features=SpaceFeatures())
    assert await svc._join_contact_for(closed) is None


async def test_the_subscriber_seats_cache_is_forgotten_with_the_listing(
    env, monkeypatch
):
    svc, _session, _probed = await _svc(
        env,
        monkeypatch,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
        caps=BOTH,
    )
    svc._subscriber_seats["gfs-x"] = True
    svc._forget_listing("gfs-x")
    assert "gfs-x" not in svc._subscriber_seats

"""§27.9 release blocker: what a GFS learns about a space — guards.

Since v_96 a space's content reaches a GFS only through its opaque channel,
whose blindness is pinned by ``test_gfs_global_channel_blind`` /
``test_gfs_private_channel_blind``; the GFS's space-keyed relay
(``POST /gfs/publish``, ``space_post_public`` fanned out to follower seats)
is gone (P7) — its removal proof is ``test_gfs_space_keyed_routes_gone``,
which stands in for the fan-out checks this file used to make. What stays:
no module — household or GFS — produces or opens a subscriber key handoff
or an author-profiles bundle, the v_44 authority cert names no member and no
reason, and the public directory never serves the owner's cert.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.global_server.domain import GlobalSpace
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security


# ─── Distinctive markers ─────────────────────────────────────────────────
#
# Every one of these is a string that MUST NOT appear anywhere a GFS sees.
# They are deliberately unique so a substring search is a sound leak
# detector.

SPACE_ID = "sp-minimization"
PUBLISHER_INSTANCE = "publisher-household-distinctive.example"
SUBSCRIBER_INSTANCE_NAME = "subscriber-household-distinctive.example"
AUTHOR_USERNAME = "aurelia-distinctive-author"

# ─── Recording doubles ───────────────────────────────────────────────────


class _RecordingResp:
    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """Records the exact JSON body the household POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _RecordingResp()


# ─── Helpers ─────────────────────────────────────────────────────────────


#: Every marker that must be absent from a serialized publish body / frame.


# ─── The subscriber key handoff is gone (v_96) ────────────────────────────


_PKG = Path(__file__).resolve().parents[2] / "socialhome"


def test_no_household_module_produces_or_opens_a_key_handoff() -> None:
    """The handoff once had to carry no identity on the GFS wire. Since v_96
    no household sends or opens one at all (a follower is a member; the key
    reaches it by federation), and since P7 no GFS relays one: no module —
    the GFS and the signing-domain constants included — names the event
    type or the removed sender / receiver."""
    banned = (
        "space_subscriber_key_handoff",
        "AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF",
        "space_author_profiles_bundle",
        "AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE",
        "SpaceSubscriberKeyOutbound",
        "SpaceSubscriberKeyInbound",
    )
    hits = []
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG)
        text = path.read_text(encoding="utf-8")
        hits += [(rel.as_posix(), b) for b in banned if b in text]
    assert hits == []


# ─── v_44: the authority cert names no member and no reason ─────────────


#: Every key the owner-signed authority cert carries — and so everything a
#: connection server learns from a rotation: which space, which owner (it
#: already knows both), which key, and how many rotations happened.
AUTHORITY_CERT_KEYS: frozenset[str] = frozenset(
    {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_suite",
        "key_epoch",
        "issued_at",
        "cert_sig_suite",
        "cert_sig",
    }
)


async def test_publish_after_rotation_carries_only_the_cert_facts(tmp_dir):
    """The owner's publish after a rotation adds exactly one field — the
    cert — and nothing in it (or in the body) names the revoked household,
    any member, or why the key rotated."""
    db = AsyncDatabase(tmp_dir / "rot.db", batch_timeout_ms=10)
    await db.startup()
    try:
        owner = generate_identity_keypair()
        owner_id = derive_instance_id(owner.public_key)
        spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x03" * 32))
        await spaces.save(
            Space(
                id=SPACE_ID,
                name="Rot",
                owner_instance_id=owner_id,
                owner_username="anna",
                identity_public_key="aa" * 32,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.GLOBAL,
                join_mode=JoinMode.OPEN,
            )
        )
        k2 = generate_space_keypair()
        await spaces.rotate_authority_key(
            SPACE_ID,
            public_key_hex=k2.public_key.hex(),
            seed=k2.private_key,
            key_epoch=1,
        )
        from socialhome.capabilities_sig import sign_capabilities

        gfs_kp = generate_identity_keypair()
        conns = SqliteGfsConnectionRepo(db)
        await conns.save(
            GfsConnection(
                id="g1",
                gfs_instance_id="gfs",
                display_name="GFS",
                public_key=gfs_kp.public_key.hex(),
                inbox_url="https://gfs.example",
                status="active",
                paired_at="2025-01-01T00:00:00+00:00",
            )
        )
        caps = {"anonymous_publish": True, "authority_rotation": True}
        caps_sig, caps_suite = sign_capabilities(gfs_kp.private_key, "gfs", caps)

        class _Session(_RecordingSession):
            def get(self, url, **_kw):
                resp = _RecordingResp()

                async def _json():
                    return {
                        "capabilities": caps,
                        "capabilities_sig": caps_sig,
                        "capabilities_sig_suite": caps_suite,
                    }

                resp.json = _json  # type: ignore[attr-defined]
                return resp

            def post(self, url, *, json=None, **_kw):
                self.posts.append((url, json or {}))
                resp = _RecordingResp()

                async def _json():
                    return {"status": "active"}

                resp.json = _json  # type: ignore[attr-defined]
                return resp

        session = _Session()
        svc = GfsConnectionService(conns, http_client=session)
        svc.attach_publish_context(
            space_repo=spaces,
            own_instance_id=owner_id,
            own_signing_key=owner.private_key,
        )
        await svc.publish_space(SPACE_ID, "g1")
        body = session.posts[-1][1]
        assert set(body["authority_cert"]) == AUTHORITY_CERT_KEYS
        blob = json.dumps(body)
        for marker in (PUBLISHER_INSTANCE, SUBSCRIBER_INSTANCE_NAME, AUTHOR_USERNAME):
            assert marker not in blob
        assert "reason" not in body["authority_cert"]
    finally:
        await db.shutdown()


def test_public_directory_never_serves_the_owner_cert():
    """The unauthenticated listing / detail show the current key and its
    epoch only: the owner's household identity key and the rotation time
    stay inside the GFS."""
    from socialhome.authority_cert import sign_authority_cert
    from socialhome.global_server.routes.relay import public_space_dict

    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    k2 = generate_space_keypair()
    cert = sign_authority_cert(
        space_id=SPACE_ID,
        owner_instance_id=owner_id,
        owner_seed=owner.private_key,
        owner_pk_hex=owner.public_key.hex(),
        authority_pk_hex=k2.public_key.hex(),
        key_epoch=1790000000,
    )
    body = public_space_dict(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=owner_id,
            identity_public_key=k2.public_key.hex(),
            authority_cert=cert,
            authority_rotation_seq=3,
        )
    )
    blob = json.dumps(body)
    assert "authority_cert" not in body
    assert owner.public_key.hex() not in blob
    assert cert["issued_at"] not in blob
    assert cert["cert_sig"] not in blob
    # No wall-clock epoch (it would date the revocation) — only the GFS's
    # own re-pin counter.
    assert "authority_key_epoch" not in body
    assert "1790000000" not in blob
    assert body["authority_rotation_seq"] == 3
    assert body["identity_public_key"] == k2.public_key.hex()

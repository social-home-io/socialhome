# ruff: noqa: F401, F811 — ``world`` is the shared fixture of the e2e scenario
"""Release-blocker protocol tests: public moments are encrypted from the GFS
(v_62). Marked ``@pytest.mark.security``.

Two households and the real GFS app (the e2e scenario's ``world``). Pins the
invariants the owner decision rests on: the GFS never carries a public
moment in plaintext, a follower never stores one, a key the GFS (or anyone
but the author's household) makes up is never taken, a GFS that swaps in a
key-wrap key of its own gets nothing sealed, and end to end the GFS sees no
moment content.
"""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.moment_public_seal import key_plaintext, seal_body, sign_wire
from tests.moment_public_wire import now_iso
from tests.scenarios.test_moment_public_encrypted_e2e import (
    MOMENT_ID,
    SECRET_TEXT,
    _assert_gfs_saw_no_content,
    _post_moment,
    world,
)

pytestmark = pytest.mark.security


async def _follow(w) -> None:
    await w["follow"].follow(
        follower_user_id="u-anna", gfs_id="g1", followed_user_id="u-noor"
    )
    await w["pump"]()


async def test_the_gfs_never_carries_a_plaintext_moment(world):
    """A validly signed plaintext envelope from the author's household is
    refused and reaches no follower."""
    w = world
    await _follow(w)
    author = w["author"]
    plain = {
        "author_user_id": "u-noor",
        "instance_id": author.instance_id,
        "moment_id": MOMENT_ID,
        "content": SECRET_TEXT,
    }
    r = await w["gfs"].post(
        "/gfs/moments/publish", json=sign_wire(author.ident.private_key, plain)
    )
    assert r.status == 422
    assert w["pushed"] == []


async def test_a_follower_never_stores_a_plaintext_moment(world):
    w = world
    await _follow(w)
    author = w["author"]
    canonical_body = {
        "moment_id": MOMENT_ID,
        "author_user_id": "u-noor",
        "content": SECRET_TEXT,
        "origin_instance_id": author.instance_id,
        "created_at": now_iso(hours=-1),
        "expires_at": now_iso(hours=23),
        "instance_id": author.instance_id,
    }
    frame = {
        "type": "incoming_public_moment",
        "payload": sign_wire(author.ident.private_key, canonical_body),
    }
    await w["inbound"].handle(frame, gfs_id="g1")
    assert await w["follower_moments"].get(MOMENT_ID) is None


async def test_a_key_the_gfs_makes_up_is_never_taken(world):
    """The GFS knows every household's key-wrap PUBLIC key, so it can seal a
    key of its own to a follower — but it can't sign as the author, so the
    follower never takes it, and a moment sealed under it never opens."""
    w = world
    await _follow(w)
    follower = w["follower"]
    gfs_key = generate_identity_keypair()
    planted = os.urandom(32)
    gen = 2_000_000_000
    delivery = sign_wire(
        gfs_key.private_key,
        {
            "author_user_id": "u-noor",
            "instance_id": w["author"].instance_id,
            "to": follower.instance_id,
            "key_gen": gen,
            "key_step": 0,
            "key_suite": "aesgcm-256",
            "sealed": seal_to_keywrap(
                recipient_keywrap_pub=follower.wrap.public_key,
                plaintext=key_plaintext(
                    author_user_id="u-noor",
                    key_gen=gen,
                    key_step=0,
                    to=follower.instance_id,
                    key=planted,
                ),
            ),
        },
    )
    await w["inbound"].handle({"type": "moment_key", "payload": delivery}, gfs_id="g1")
    assert await w["keys_in"].key_for("u-noor", gen, 0) is None
    forged = sign_wire(
        gfs_key.private_key,
        {
            "author_user_id": "u-noor",
            "instance_id": w["author"].instance_id,
            **seal_body(
                step_key=planted,
                author_user_id="u-noor",
                key_gen=gen,
                key_step=0,
                moment_id="m-forged",
                body={
                    "op": "create",
                    "author_user_id": "u-noor",
                    "content": "forged",
                    "created_at": now_iso(hours=-1),
                    "expires_at": now_iso(hours=23),
                },
            ),
        },
    )
    await w["inbound"].handle(
        {"type": "incoming_public_moment", "payload": forged}, gfs_id="g1"
    )
    assert await w["follower_moments"].get("m-forged") is None


async def test_a_substituted_keywrap_key_gets_nothing_sealed(world):
    """A GFS naming a follower with a key-wrap key IT controls: the author's
    household refuses to seal — the binding to the identity fails."""
    w = world
    ident = generate_identity_keypair()
    real_wrap, gfs_wrap = generate_x25519_keypair(), generate_x25519_keypair()
    frame = {
        "type": "follow_changed",
        "action": "add",
        "followed_user_id": "u-noor",
        "follower": {
            "instance_id": derive_instance_id(ident.public_key),
            "identity_public_key": ident.public_key.hex(),
            "keywrap_public_key": gfs_wrap.public_key.hex(),
            "kem_suite": "x25519",
            # Signed over the REAL key-wrap key — not the substituted one.
            "keywrap_sig": b64url_encode(
                sign_ed25519(ident.private_key, real_wrap.public_key)
            ),
        },
    }
    assert await w["keys_out"].handle(frame, gfs_id="g1") == 0
    assert await w["keys_out"].flush() == 0


async def test_end_to_end_the_gfs_sees_no_moment_content(world):
    w = world
    await _follow(w)
    await _post_moment(w)
    assert await w["pump"]() == ["incoming_public_moment"]
    got = await w["follower_moments"].get(MOMENT_ID)
    assert got is not None and got.content == SECRET_TEXT
    _assert_gfs_saw_no_content(w)
    # Not in the GFS's own database either (main file or WAL).
    for name in ("gfs.db", "gfs.db-wal"):
        path = w["gfs_dir"] / name
        if path.exists():
            raw = path.read_bytes()
            assert SECRET_TEXT.encode() not in raw

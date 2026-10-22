"""End-to-end tests for the GFS ``/gfs/moments/users/*`` and
``/gfs/moments/*`` HTTP routes (§Momentum-public)."""

from __future__ import annotations

import json
import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server import moment_public_registry as registry_mod
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_moment_public_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.ws_registry import GfsWebSocketRegistry
from socialhome.moment_public_seal import MAX_CIPHERTEXT_CHARS, seal_body


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


@pytest.fixture
async def author(client):
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-author",
            display_name="Author",
            public_key=kp.public_key.hex(),
            status="active",
        )
    )
    return kp


@pytest.fixture
async def follower(client):
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-follower",
            display_name="Follower",
            public_key=kp.public_key.hex(),
            status="active",
        )
    )
    return kp


def _sign(kp, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    body = dict(body)
    body["signature"] = b64url_encode(sign_ed25519(kp.private_key, canonical))
    return body


# ── Register / deregister ──────────────────────────────────────────────


async def test_register_creates_directory_entry(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 201
    out = await resp.json()
    assert out["user_id"] == "u-author"
    # Directory now lists the user.
    listing = await (await client.get("/gfs/moments/users")).json()
    assert any(u["user_id"] == "u-author" for u in listing["users"])


async def test_register_missing_home_pk_returns_422(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 422


async def test_register_instance_mismatch_returns_403(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-other",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 403


async def test_deregister_removes_directory_entry(client, author):
    body = _sign(
        author,
        {
            "user_id": "u-author",
            "instance_id": "inst-author",
            "username": "alice",
            "display_name": "Alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    assert (await client.post("/gfs/moments/users/register", json=body)).status == 201
    resp = await client.post(
        "/gfs/moments/users/u-author/deregister",
        json=_sign(author, {"user_id": "u-author", "instance_id": "inst-author"}),
    )
    assert resp.status == 200
    out = await resp.json()
    assert out == {"deregistered": True}


async def test_deregister_unknown_user_returns_404(client, author):
    resp = await client.post(
        "/gfs/moments/users/u-ghost/deregister",
        json=_sign(author, {"user_id": "u-ghost", "instance_id": "inst-author"}),
    )
    assert resp.status == 404


# ── Follow / unfollow ──────────────────────────────────────────────────


async def test_follow_then_unfollow(client, author, follower):
    # Author registers.
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "bio": "Builds boats.",
                "home_instance_pk": "ab" * 32,
            },
        ),
    )
    # Follower follows. The auth middleware reads ``instance_id`` from
    # the body, so we send it alongside the role-named field.
    resp = await client.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 201
    out = await resp.json()
    assert out["user"]["user_id"] == "u-author"
    # The same public directory fields ``GET /gfs/moments/users/{id}``
    # serves anyone — the follower names the author with them.
    assert out["user"]["bio"] == "Builds boats."
    assert out["user"]["picture_digest"] is None
    assert out["follow"]["followed_user_id"] == "u-author"
    # Unfollow.
    resp = await client.post(
        "/gfs/moments/users/u-author/unfollow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 200
    out = await resp.json()
    assert out["unfollowed"] is True


async def test_follow_unknown_author_returns_404(client, follower):
    resp = await client.post(
        "/gfs/moments/users/u-ghost/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_user_id": "u-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 404


async def test_follow_missing_follower_id_returns_422(client, follower):
    resp = await client.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_instance_id": "inst-follower",
            },
        ),
    )
    assert resp.status == 422


# ── Publish / delete ───────────────────────────────────────────────────


async def _register_author(client, author) -> None:
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": author.public_key.hex(),
            },
        ),
    )


def _encrypted(
    author,
    *,
    op: str = "create",
    key_gen: int | None = None,
    key_step: int = 0,
    **extra,
) -> dict:
    """An envelope in the v_62 shape: routing fields + ciphertext only."""
    return _sign(
        author,
        {
            "instance_id": "inst-author",
            "author_user_id": "u-author",
            **seal_body(
                step_key=os.urandom(32),
                author_user_id="u-author",
                key_gen=int(time.time()) if key_gen is None else key_gen,
                key_step=key_step,
                moment_id="m-1",
                body={"op": op, "content": "hi"},
            ),
            **extra,
        },
    )


@pytest.mark.security
@pytest.mark.parametrize("path", ["/gfs/moments/publish", "/gfs/moments/delete"])
async def test_an_envelope_from_an_earlier_day_is_refused_as_a_re_post(
    client, author, path
):
    """A captured envelope re-posted after its key step's day: the author's
    household always seals under today's step, so it is a replay — refused,
    never fanned out (#1111 follow-up)."""
    await _register_author(client, author)
    two_days_ago = int(time.time()) - 2 * 86_400
    resp = await client.post(path, json=_encrypted(author, key_gen=two_days_ago))
    assert resp.status == 409
    assert await resp.json() == {"error": "stale_envelope"}


@pytest.mark.security
async def test_a_household_past_its_publish_budget_is_429(client, author, monkeypatch):
    """Publishes and deletes share one per-household budget; past it the
    envelope is refused before any fan-out."""
    monkeypatch.setattr(registry_mod, "MOMENT_PUBLISHES_PER_MINUTE", 2)
    await _register_author(client, author)
    assert (
        await client.post("/gfs/moments/publish", json=_encrypted(author))
    ).status == 200
    resp = await client.post(
        "/gfs/moments/delete", json=_encrypted(author, op="delete")
    )
    assert resp.status == 200
    resp = await client.post("/gfs/moments/publish", json=_encrypted(author))
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "60"
    assert await resp.json() == {"error": "rate_limited"}


async def test_a_refused_envelope_spends_no_budget(client, author, monkeypatch):
    monkeypatch.setattr(registry_mod, "MOMENT_PUBLISHES_PER_MINUTE", 1)
    await _register_author(client, author)
    stale = _encrypted(author, key_gen=int(time.time()) - 2 * 86_400)
    assert (await client.post("/gfs/moments/publish", json=stale)).status == 409
    assert (
        await client.post("/gfs/moments/publish", json=_encrypted(author))
    ).status == 200


async def test_a_generation_days_old_publishes_under_todays_step(client, author):
    await _register_author(client, author)
    three_days_ago = int(time.time()) - 3 * 86_400 - 60
    resp = await client.post(
        "/gfs/moments/publish",
        json=_encrypted(author, key_gen=three_days_ago, key_step=3),
    )
    assert resp.status == 200


async def test_publish_returns_delivered_count(client, author):
    # Author registers but has no followers — delivered=0.
    await _register_author(client, author)
    resp = await client.post("/gfs/moments/publish", json=_encrypted(author))
    assert resp.status == 200
    out = await resp.json()
    assert out == {"delivered": 0}


async def test_delete_returns_delivered_count(client, author):
    await _register_author(client, author)
    resp = await client.post(
        "/gfs/moments/delete", json=_encrypted(author, op="delete")
    )
    assert resp.status == 200


@pytest.mark.parametrize("path", ["/gfs/moments/publish", "/gfs/moments/delete"])
@pytest.mark.parametrize(
    "extra",
    [
        {"content": "hi"},
        {"key_epoch": 1},
        {"author_display_name": "Alice"},
        {"media_url": "api/media/x.webp"},
    ],
)
async def test_a_plaintext_field_is_refused_never_fanned_out(
    frames, client, author, follower, path, extra
):
    """v_62: the GFS never carries a public moment in the clear — an
    envelope with any field beyond the routing ones is refused 422, and
    no follower is sent anything."""
    await _register_author(client, author)
    resp = await client.post(path, json=_encrypted(author, **extra))
    assert resp.status == 422
    assert (await resp.json())["error"] == "encryption_required"
    assert frames == []


async def test_an_older_households_plaintext_envelope_is_refused(client, author):
    await _register_author(client, author)
    resp = await client.post(
        "/gfs/moments/publish",
        json=_sign(
            author,
            {
                "instance_id": "inst-author",
                "moment_id": "m-1",
                "author_user_id": "u-author",
                "content": "hi",
            },
        ),
    )
    assert resp.status == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"key_gen": True},
        {"key_step": -1},
        {"moment_id": ""},
        {"moment_id": "m" * 201},
        {"ciphertext": 7},
    ],
)
async def test_a_malformed_key_id_or_moment_id_is_refused(client, author, patch):
    await _register_author(client, author)
    env = {k: v for k, v in _encrypted(author).items() if k != "signature"}
    resp = await client.post(
        "/gfs/moments/publish", json=_sign(author, {**env, **patch})
    )
    assert resp.status == 422


async def test_publish_for_an_author_of_another_household_is_refused(
    client, author, follower
):
    """Only the household the author registered from may publish as them."""
    await _register_author(client, author)
    env = _sign(
        follower,
        {
            **{k: v for k, v in _encrypted(author).items() if k != "signature"},
            "instance_id": "inst-follower",
        },
    )
    resp = await client.post("/gfs/moments/publish", json=env)
    assert resp.status == 403


async def test_an_oversized_envelope_is_refused_413(client, author):
    await _register_author(client, author)
    env = _encrypted(author)
    env["ciphertext"] = "x" * (MAX_CIPHERTEXT_CHARS + 4096)
    resp = await client.post("/gfs/moments/publish", json=_sign(author, env))
    assert resp.status == 413


# ── Key routing (v_62) ─────────────────────────────────────────────────


SUITES = {"kem": ["x25519"], "seal": ["aesgcm-256"]}


class _Frames(list):
    """A list that also carries the set of households that are offline."""


@pytest.fixture
def frames(monkeypatch) -> _Frames:
    out = _Frames()
    out.offline = set()  # type: ignore[attr-defined]

    async def _send(self, instance_id, frame):
        if instance_id in out.offline:  # type: ignore[attr-defined]
            return False
        out.append((instance_id, frame))
        return True

    monkeypatch.setattr(GfsWebSocketRegistry, "send", _send)
    return out


async def _follow(
    client, follower, *, user: str = "u-fan", suites: dict | None = SUITES
) -> None:
    body = {
        "instance_id": "inst-follower",
        "follower_instance_id": "inst-follower",
        "follower_user_id": user,
    }
    if suites is not None:
        body["moment_suites"] = suites
    resp = await client.post(
        "/gfs/moments/users/u-author/follow", json=_sign(follower, body)
    )
    assert resp.status == 201


def _delivery(to: str = "inst-follower", **extra) -> dict:
    return {
        "author_user_id": "u-author",
        "instance_id": "inst-author",
        "to": to,
        "key_gen": 1_700_000_000,
        "key_step": 0,
        "key_suite": "aesgcm-256",
        "sealed": {"kem_suite": "x25519", "eph_pk": "e", "ciphertext": "c:t"},
        "signature": "s",
        **extra,
    }


async def _post_keys(client, author, deliveries) -> dict:
    resp = await client.post(
        "/gfs/moments/keys",
        json=_sign(author, {"instance_id": "inst-author", "deliveries": deliveries}),
    )
    assert resp.status == 200
    return await resp.json()


async def test_follow_notice_names_the_followers_keys_and_suites(
    frames, client, author
):
    """The author gets the new follower household's published identity +
    key-wrap material and what it declared it can open."""
    await _register_author(client, author)
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-follower",
            display_name="Follower",
            public_key=kp.public_key.hex(),
            status="active",
            keywrap_public_key="cd" * 32,
            kem_suite="x25519",
            keywrap_sig="sig",
        )
    )
    await _follow(client, kp)
    ((target, frame),) = frames
    assert target == "inst-author"
    assert frame["type"] == "follow_changed" and frame["action"] == "add"
    assert frame["follower"] == {
        "instance_id": "inst-follower",
        "identity_public_key": kp.public_key.hex(),
        "keywrap_public_key": "cd" * 32,
        "kem_suite": "x25519",
        "keywrap_sig": "sig",
        "moment_suites": SUITES,
    }


@pytest.mark.parametrize(
    "suites",
    [
        "x25519",
        {"kem": "x25519"},
        {"kem": ["x"] * 9},
        {"kem": [""]},
        {"seal": ["s" * 65]},
        {"other": []},
    ],
)
async def test_malformed_follow_suites_are_refused(client, author, follower, suites):
    await _register_author(client, author)
    resp = await client.post(
        "/gfs/moments/users/u-author/follow",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "follower_instance_id": "inst-follower",
                "follower_user_id": "u-fan",
                "moment_suites": suites,
            },
        ),
    )
    assert resp.status == 422


async def test_a_household_that_cant_decrypt_is_sent_no_moments(
    frames, client, author, follower
):
    """A follow without declared suites (an older household) gets no
    ciphertext it can't open; once it declares them it does."""
    await _register_author(client, author)
    await _follow(client, follower, suites=None)
    frames.clear()
    out = await (
        await client.post("/gfs/moments/publish", json=_encrypted(author))
    ).json()
    assert out == {"delivered": 0} and frames == []
    await _follow(client, follower, user="u-fan-2")
    frames.clear()
    out = await (
        await client.post("/gfs/moments/publish", json=_encrypted(author))
    ).json()
    assert out == {"delivered": 1}
    assert [t for t, f in frames if f["type"] == "incoming_public_moment"] == [
        "inst-follower"
    ]


async def test_keys_go_only_to_a_household_that_follows(
    frames, client, author, follower
):
    await _register_author(client, author)
    await _follow(client, follower)
    frames.clear()
    out = await _post_keys(client, author, [_delivery(), _delivery(to="inst-stranger")])
    assert out == {"delivered": 1}
    assert frames == [("inst-follower", {"type": "moment_key", "payload": _delivery()})]


async def test_an_offline_follower_gets_its_key_when_it_reconnects(
    frames, client, author, follower
):
    """Offline at delivery: the GFS keeps the latest sealed key (which it
    can't open) and pushes it on reconnect — once, not on every reconnect."""
    await _register_author(client, author)
    await _follow(client, follower)
    registry = client._app[gfs_moment_public_registry_key]
    frames.offline.add("inst-follower")
    assert await _post_keys(client, author, [_delivery()]) == {"delivered": 0}
    # A newer key replaces the older one in the mailbox.
    newer = _delivery(key_step=1)
    await _post_keys(client, author, [newer])
    frames.offline.clear()
    frames.clear()
    assert await registry.on_follower_connected("inst-follower") == 1
    assert frames == [("inst-follower", {"type": "moment_key", "payload": newer})]
    frames.clear()
    assert await registry.on_follower_connected("inst-follower") == 0
    assert frames == []


async def test_a_key_request_is_answered_from_the_mailbox(
    frames, client, author, follower
):
    """The author's household isn't asked when the GFS already holds a key
    of the wanted generation for that household."""
    await _register_author(client, author)
    await _follow(client, follower)
    await _post_keys(client, author, [_delivery()])
    frames.clear()
    body = {"instance_id": "inst-follower", "want_gen": 1_700_000_000}
    await client.post(
        "/gfs/moments/users/u-author/key_request", json=_sign(follower, body)
    )
    assert frames == [("inst-follower", {"type": "moment_key", "payload": _delivery()})]


async def test_unfollowing_drops_the_mailbox_only_when_the_household_left(
    frames, client, author, follower
):
    await _register_author(client, author)
    await _follow(client, follower)
    await _follow(client, follower, user="u-fan-2")
    await _post_keys(client, author, [_delivery()])
    mailbox = client._app[gfs_moment_public_registry_key]._mailbox
    frames.clear()
    for user, still in (("u-fan", True), ("u-fan-2", False)):
        await client.post(
            "/gfs/moments/users/u-author/unfollow",
            json=_sign(
                follower,
                {
                    "instance_id": "inst-follower",
                    "follower_instance_id": "inst-follower",
                    "follower_user_id": user,
                },
            ),
        )
        notice = [f for _t, f in frames if f["type"] == "follow_changed"][-1]
        assert notice["action"] == "remove"
        assert notice["follower_instance_id"] == "inst-follower"
        assert notice["household_still_follows"] is still
        held = await mailbox.get("u-author", "inst-follower")
        assert (held is not None) is still


async def test_only_the_authors_household_may_send_its_keys(
    frames, client, author, follower
):
    await _register_author(client, author)
    await _follow(client, follower)
    frames.clear()
    resp = await client.post(
        "/gfs/moments/keys",
        json=_sign(
            follower,
            {
                "instance_id": "inst-follower",
                "deliveries": [_delivery(instance_id="inst-follower")],
            },
        ),
    )
    assert resp.status == 200
    assert (await resp.json()) == {"delivered": 0}
    assert frames == []


@pytest.mark.parametrize(
    "deliveries",
    [
        [],
        "nope",
        [_delivery(content="hi")],
        [_delivery(instance_id="inst-other")],
        [_delivery(key_gen="1")],
        [_delivery(key_step=True)],
        [_delivery(key_suite=7)],
        [_delivery(sealed={"kem_suite": "x25519", "eph_pk": "e"})],
        [_delivery(sealed={"kem_suite": "x", "eph_pk": "e", "ciphertext": "c" * 2000})],
        [_delivery()] * 101,
    ],
)
async def test_malformed_or_oversized_key_batches_are_refused(
    client, author, deliveries
):
    await _register_author(client, author)
    resp = await client.post(
        "/gfs/moments/keys",
        json=_sign(author, {"instance_id": "inst-author", "deliveries": deliveries}),
    )
    assert resp.status == 422


async def test_key_request_reaches_the_author_with_the_followers_keys(
    frames, client, author, follower
):
    await _register_author(client, author)
    await _follow(client, follower)
    await _follow(client, follower, user="u-fan-2")
    frames.clear()
    body = {"instance_id": "inst-follower", "want_gen": 5}
    for _ in range(4):
        resp = await client.post(
            "/gfs/moments/users/u-author/key_request", json=_sign(follower, body)
        )
        assert resp.status == 202
    # Throttled to two a minute per (household, author).
    assert len(frames) == 2
    target, frame = frames[0]
    assert target == "inst-author"
    assert frame["type"] == "moment_key_request"
    assert frame["followed_user_id"] == "u-author"
    assert frame["want_gen"] == 5
    assert frame["follower_user_ids"] == ["u-fan", "u-fan-2"]
    assert frame["follower"]["instance_id"] == "inst-follower"


async def test_key_request_from_a_non_follower_is_silently_dropped(
    frames, client, author, follower
):
    await _register_author(client, author)
    resp = await client.post(
        "/gfs/moments/users/u-author/key_request",
        json=_sign(follower, {"instance_id": "inst-follower", "want_gen": True}),
    )
    assert resp.status == 202
    assert frames == []
    unknown = await client.post(
        "/gfs/moments/users/u-nobody/key_request",
        json=_sign(follower, {"instance_id": "inst-follower"}),
    )
    assert unknown.status == 202


async def test_follower_stats_count_households_that_cant_decrypt(
    client, author, follower
):
    """Counts only, for the author's own settings — and only the author."""
    await _register_author(client, author)
    await _follow(client, follower, suites=None)
    stats = await client.post(
        "/gfs/moments/users/u-author/follower_stats",
        json=_sign(author, {"instance_id": "inst-author"}),
    )
    assert stats.status == 200
    assert await stats.json() == {
        "followers": 1,
        "households": 1,
        "households_unable": 1,
    }
    other = await client.post(
        "/gfs/moments/users/u-author/follower_stats",
        json=_sign(follower, {"instance_id": "inst-follower"}),
    )
    assert other.status == 403


async def test_info_advertises_encrypted_moments_signed(client):
    info = await (await client.get("/gfs/info")).json()
    assert info["capabilities"]["encrypted_moments"] is True
    assert info["capabilities_sig"]


# ── Public discovery ───────────────────────────────────────────────────


async def test_users_html_directory_renders(client, author):
    await client.post(
        "/gfs/moments/users/register",
        json=_sign(
            author,
            {
                "user_id": "u-author",
                "instance_id": "inst-author",
                "username": "alice",
                "display_name": "Alice",
                "home_instance_pk": "ab" * 32,
            },
        ),
    )
    resp = await client.get("/moments")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    # SPA shell: actual rendering happens client-side via the
    # static JS bundle, not in the HTML response.
    assert "Public Momentum" in text
    assert "/static/users_directory.js" in text
    # Fonts are self-hosted on the GFS, never Google Fonts.
    assert "/static/fonts/fonts.css" in text
    assert "fonts.googleapis.com" not in text


async def test_users_html_directory_empty_state(client):
    resp = await client.get("/moments")
    assert resp.status == 200
    text = await resp.text()
    assert "No registered users yet" in text


async def test_users_json_directory_empty(client):
    resp = await client.get("/gfs/moments/users")
    assert resp.status == 200
    out = await resp.json()
    assert out["users"] == []
    assert out["count"] == 0


# ── Suites re-declared on connect (v_62) ──────────────────────────────────


async def test_a_household_redeclares_its_suites_for_every_follow(
    client, author, follower
):
    """A follow made before the household upgraded declared nothing (it gets
    no public moments); ``POST /gfs/moments/suites`` fixes every follow of
    that household at once — and only that household's."""
    await _register_author(client, author)
    await _follow(client, follower, suites=None)
    registry = client._app[gfs_moment_public_registry_key]
    assert await registry._follows.suites_at("u-author", "inst-follower") is None
    body = {"instance_id": "inst-follower", "moment_suites": SUITES}
    r = await client.post("/gfs/moments/suites", json=_sign(follower, body))
    assert r.status == 200
    assert (await r.json()) == {"updated": 1}
    assert await registry._follows.suites_at("u-author", "inst-follower")
    # Idempotent: nothing changes the second time.
    r = await client.post("/gfs/moments/suites", json=_sign(follower, body))
    assert (await r.json()) == {"updated": 0}


@pytest.mark.parametrize("suites", [None, "x", {"kem": "x25519"}, {"other": []}])
async def test_malformed_suites_declarations_are_refused(client, follower, suites):
    body = {"instance_id": "inst-follower"}
    if suites is not None:
        body["moment_suites"] = suites
    r = await client.post("/gfs/moments/suites", json=_sign(follower, body))
    assert r.status == 422


@pytest.mark.parametrize("user_id", ["u" * 129, "u-line\nbreak"])
async def test_register_refuses_an_unusable_user_id(client, author, user_id):
    """A user id a cluster node would refuse as an audience key (too long,
    unprintable) is refused at registration — it would otherwise register
    and never reach a follower on another node."""
    body = _sign(
        author,
        {
            "user_id": user_id,
            "instance_id": "inst-author",
            "username": "alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 422
    assert await resp.json() == {"error": "invalid_user_id"}


async def test_register_accepts_a_user_id_at_the_cap(client, author):
    body = _sign(
        author,
        {
            "user_id": "u" * 128,
            "instance_id": "inst-author",
            "username": "alice",
            "home_instance_pk": "ab" * 32,
        },
    )
    resp = await client.post("/gfs/moments/users/register", json=body)
    assert resp.status == 201

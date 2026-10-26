"""Release-blocker protocol tests: reactions reach authors further away (v_67).

Marked ``@pytest.mark.security``.

A reaction to a moment two or three hops from its author travels back along
the path the moment came, as ``MOMENT_REACTION_ROUTED``: sealed to the
origin household's key-wrap key, signed by the reactor's household, carried
hop by hop on return tokens only the relay that minted each can open. The
rules these tests encode, against the real application registry and SQLite:

* **Origin.** A routed reaction is applied only when the seal opens with this
  household's key-wrap key; the reactor's household key derives to its
  instance id (and equals the pinned key when paired); the household's
  signature verifies under a known suite; the reactor's user id derives from
  that key and its anchor; the moment is live, authored here and not
  ``no_relay`` (protected author); the reactor is neither one of ours,
  blocked by the author nor on a banned household; the reaction is under
  25 h old and not a replay. Adds and removals apply in ``occurred_at``
  order. Everything else is refused at WARNING and changes nothing.
* **Relay.** A relay forwards only when its own (last) return token opens —
  minted for this moment, handed back by the neighbour it was minted for,
  not expired — to a confirmed v_67 household, the next hop the token names,
  under its rate limits. It keeps no state.
* **Create.** A receiver keeps a reaction route only when the origin's
  key-wrap key is bound to the origin's identity key (no relay can
  substitute its own) and every relay added a token.
* **Reactor.** A reaction is routed only along a complete route through a
  v_67 neighbour; otherwise it stays in the household and the API says so.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    instance_keywrap_public_key_key,
    instance_public_key_key,
    moment_reaction_router_key,
    moment_service_key,
)
from socialhome.config import Config
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation.federation_service import FederationService
from socialhome.federation.moment_reaction_route import (
    RETURN_TOKEN_SUITE_AESGCM_256,
    build_reaction,
    open_reaction,
    origin_keywrap_fields,
    reaction_signing_bytes,
    seal_reaction,
    verify_reaction,
)
from socialhome.moment_origin import sign_moment_origin

pytestmark = pytest.mark.security

FET = FederationEventType
V67 = FederationCapability.MIN_FOR_ROUTED_MOMENT_REACTIONS

REACTOR_KEY = generate_identity_keypair()  # a household two hops away
REACTOR = derive_instance_id(REACTOR_KEY.public_key)
RITA = derive_user_id(REACTOR_KEY.public_key, "rita-anchor")
ORIGIN_KEY = generate_identity_keypair()  # a far-away origin (reactor tests)
ORIGIN = derive_instance_id(ORIGIN_KEY.public_key)
ORIGIN_KW = generate_x25519_keypair()
NEIGHBOUR = "peer-neighbour"  # paired, v_67 — hands reactions to us
OTHER = "peer-other"  # paired, v_67 — another neighbour
OLD = "peer-old"  # paired, v_66
EXPIRES = (datetime.now(UTC) + timedelta(days=5)).isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "moments.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, pk: bytes, proto_version: int) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            pk.hex(),
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            proto_version,
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    client = await aiohttp_client(app)
    db = app[db_key]
    own = app[federation_service_key].own_instance_id
    own_pk = app[instance_public_key_key]
    anna = derive_user_id(own_pk, "anna-anchor")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, identity_anchor)"
        " VALUES(?,?,?,?)",
        ("anna", anna, "Anna", "anna-anchor"),
    )
    await _seed_peer(db, NEIGHBOUR, generate_identity_keypair().public_key, V67)
    await _seed_peer(db, OTHER, generate_identity_keypair().public_key, V67)
    await _seed_peer(db, OLD, generate_identity_keypair().public_key, V67 - 1)
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at, received_via) VALUES(?,?,?,?,?,'self')",
        ("m-anna", anna, "by anna", own, EXPIRES),
    )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    router = app[moment_reaction_router_key]
    router._debounce_s = 0  # no 3 s wait in tests
    return app, db, sent, own, anna, router, client


async def _dispatch(app, event_type, payload, *, from_instance=NEIGHBOUR) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )


def _now_ms(delta_s: float = 0) -> int:
    return int((datetime.now(UTC).timestamp() + delta_s) * 1000)


def _inner(
    own: str,
    anna: str,
    *,
    emoji: str | None = "👏",
    at_ms: int | None = None,
    key=REACTOR_KEY,
    anchor="rita-anchor",
    reactor_id: str | None = None,
    reactor_home: str | None = None,
    moment_id="m-anna",
) -> dict:
    inner = build_reaction(
        seed=key.private_key,
        identity_pk=key.public_key,
        reactor_instance_id=reactor_home or derive_instance_id(key.public_key),
        reactor_user_id=reactor_id or derive_user_id(key.public_key, anchor),
        reactor_anchor=anchor,
        moment_id=moment_id,
        author_user_id=anna,
        origin_instance_id=own,
        emoji=emoji,
        occurred_at_ms=at_ms if at_ms is not None else _now_ms(),
    )
    return inner


def _routed(app, own: str, inner: dict, *, moment_id="m-anna") -> dict:
    return {
        "moment_id": moment_id,
        "origin_instance_id": own,
        "rt": [],
        "rt_suite": RETURN_TOKEN_SUITE_AESGCM_256,
        "sealed": seal_reaction(
            origin_keywrap_pub=app[instance_keywrap_public_key_key], inner=inner
        ),
    }


def _resign(inner: dict, key=REACTOR_KEY) -> dict:
    inner = {k: v for k, v in inner.items() if k != "reaction_sig"}
    inner["reaction_sig"] = b64url_encode(
        sign_ed25519(key.private_key, reaction_signing_bytes(inner))
    )
    return inner


async def _reactions(db) -> list[tuple]:
    rows = await db.fetchall(
        "SELECT moment_id, reactor_user_id, emoji FROM moment_reactions"
        " ORDER BY reactor_user_id",
        (),
    )
    return [tuple(r) for r in rows]


# ── Origin: a well-formed reaction from two hops away lands ───────────────


async def test_origin_applies_a_routed_reaction_and_its_removal(env):
    app, db, sent, own, anna, _router, _c = env
    await _dispatch(
        app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, _inner(own, anna))
    )
    assert await _reactions(db) == [("m-anna", RITA, "👏")]
    await _dispatch(
        app,
        FET.MOMENT_REACTION_ROUTED,
        _routed(app, own, _inner(own, anna, emoji=None, at_ms=_now_ms(1))),
    )
    assert await _reactions(db) == []
    assert sent == []  # the origin forwards nothing


# ── Origin: everything forged or out of bounds changes nothing ────────────


def _wrong_anchor(own, anna):
    # The user id is not the one the household key + anchor derive to.
    return _inner(own, anna, reactor_id=derive_user_id(REACTOR_KEY.public_key, "x"))


def _wrong_household(own, anna):
    # Claims another household id than its key derives to.
    return _inner(own, anna, reactor_home=ORIGIN)


def _tampered(own, anna):
    return {**_inner(own, anna), "emoji": "🔥"}


def _unknown_suite(own, anna):
    return {**_inner(own, anna), "reaction_sig_suite": "rot13"}


def _no_suite(own, anna):
    inner = _inner(own, anna)
    inner.pop("reaction_sig_suite")
    return inner


def _too_old(own, anna):
    return _inner(own, anna, at_ms=_now_ms(-26 * 3600))


def _future(own, anna):
    return _inner(own, anna, at_ms=_now_ms(3600))


def _other_moment(own, anna):
    return _inner(own, anna, moment_id="m-elsewhere")


def _long_emoji(own, anna):
    return _resign({**_inner(own, anna), "emoji": "x" * 40})


def _two_emoji(own, anna):
    return _resign({**_inner(own, anna), "emoji": "👏👏"})


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(_wrong_anchor, id="user id does not derive from its anchor"),
        pytest.param(_wrong_household, id="household key does not derive"),
        pytest.param(_tampered, id="emoji changed after signing"),
        pytest.param(_unknown_suite, id="unknown reaction_sig_suite"),
        pytest.param(_no_suite, id="reaction_sig_suite stripped"),
        pytest.param(_too_old, id="older than 25 h"),
        pytest.param(_future, id="from the future"),
        pytest.param(_other_moment, id="sealed for another moment"),
        pytest.param(_long_emoji, id="emoji over 32 bytes"),
        pytest.param(_two_emoji, id="two emoji"),
    ],
)
async def test_origin_refuses_a_forged_reaction(env, make, caplog):
    app, db, _sent, own, anna, _router, _c = env
    with caplog.at_level("WARNING"):
        await _dispatch(
            app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, make(own, anna))
        )
    assert await _reactions(db) == []
    assert "refusing" in caplog.text


async def test_origin_refuses_a_reactor_whose_key_differs_from_the_pinned_one(env):
    app, db, _sent, own, anna, _router, _c = env
    # The reactor's household is paired here under another key.
    await _seed_peer(db, REACTOR, generate_identity_keypair().public_key, V67)
    await _dispatch(
        app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, _inner(own, anna))
    )
    assert await _reactions(db) == []


async def test_origin_refuses_a_seal_to_another_key(env):
    app, db, _sent, own, anna, _router, _c = env
    payload = _routed(app, own, _inner(own, anna))
    payload["sealed"] = seal_reaction(
        origin_keywrap_pub=generate_x25519_keypair().public_key,
        inner=_inner(own, anna),
    )
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    assert await _reactions(db) == []


async def test_origin_refuses_an_unknown_kem_suite(env):
    app, db, _sent, own, anna, _router, _c = env
    payload = _routed(app, own, _inner(own, anna))
    payload["sealed"] = {**payload["sealed"], "kem_suite": "x448"}
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    assert await _reactions(db) == []


async def test_origin_refuses_a_replay(env):
    app, db, _sent, own, anna, _router, _c = env
    payload = _routed(app, own, _inner(own, anna))
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    await db.enqueue("DELETE FROM moment_reactions", ())
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    assert await _reactions(db) == []


async def test_origin_refuses_return_tokens_left_over(env):
    app, db, _sent, own, anna, _router, _c = env
    payload = {**_routed(app, own, _inner(own, anna)), "rt": ["left-over"]}
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    assert await _reactions(db) == []


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        pytest.param(
            "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
            ("anna", "rita"),
            id="author blocked the reactor",
        ),
        pytest.param(
            "INSERT INTO household_instance_bans(instance_id) VALUES(?)",
            ("reactor",),
            id="reactor household banned",
        ),
        pytest.param(
            "UPDATE users SET child_protection_enabled=1, declared_age=12"
            " WHERE user_id=?",
            ("anna",),
            id="protected author (no_relay)",
        ),
        pytest.param(
            "UPDATE moments SET expires_at='2000-01-01T00:00:00+00:00'",
            (),
            id="moment ended",
        ),
        pytest.param(
            "UPDATE moments SET content='', deleted_at=datetime('now')",
            (),
            id="moment deleted",
        ),
    ],
)
async def test_origin_refuses_by_local_policy(env, sql, params):
    app, db, _sent, own, anna, _router, _c = env
    values = {"anna": anna, "rita": RITA, "reactor": REACTOR}
    await db.enqueue(sql, tuple(values[p] for p in params))
    await _dispatch(
        app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, _inner(own, anna))
    )
    assert await _reactions(db) == []


async def test_origin_refuses_a_moment_it_did_not_author(env):
    app, db, _sent, own, anna, _router, _c = env
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at) VALUES('m-far', 'u-far', 'x', ?, ?)",
        (NEIGHBOUR, EXPIRES),
    )
    inner = _inner(own, anna, moment_id="m-far")
    await _dispatch(
        app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, inner, moment_id="m-far")
    )
    assert await _reactions(db) == []


async def test_origin_orders_adds_and_removals_by_when_they_were_made(env):
    app, db, _sent, own, anna, _router, _c = env

    async def _send(emoji, at):
        inner = _inner(own, anna, emoji=emoji, at_ms=_now_ms(at))
        await _dispatch(app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, inner))

    await _send("👏", -60)
    await _send("🔥", -120)  # older than the stored one — loses
    assert await _reactions(db) == [("m-anna", RITA, "👏")]
    await _send(None, -30)  # removal
    await _send("😂", -45)  # older than the removal — loses
    assert await _reactions(db) == []
    await _send("😮", -10)  # newer — lands
    assert await _reactions(db) == [("m-anna", RITA, "😮")]


async def test_origin_rate_limits_one_reactor_household(env):
    app, _db, _sent, own, anna, router, _c = env
    for i in range(61):
        inner = _inner(own, anna, emoji="👏", at_ms=_now_ms(-100 + i))
        await _dispatch(
            app,
            FET.MOMENT_REACTION_ROUTED,
            _routed(app, own, inner),
            from_instance=NEIGHBOUR if i % 2 else OTHER,
        )
    assert not router._limiter.is_allowed(f"r:{REACTOR}", limit=60, window_s=60)


# ── Relay: forwards only its own, valid, unexpired token ──────────────────


def _token(router, *, to=NEIGHBOUR, prev=OTHER, moment_id="m-far", expires=EXPIRES):
    create = {
        "moment_id": moment_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "expires_at": expires,
        "origin_keywrap_pk": "00" * 32,
        "hop_count": 2,
    }
    stamped = router.stamp_relayed_create(
        create, received_hop=1, from_instance=prev, to_instance=to
    )
    return stamped["rt"][-1]


def _relay_payload(token, *, origin=OTHER, moment_id="m-far", suite=None) -> dict:
    return {
        "moment_id": moment_id,
        "origin_instance_id": origin,
        "rt": [token],
        "rt_suite": suite or RETURN_TOKEN_SUITE_AESGCM_256,
        "sealed": seal_reaction(
            origin_keywrap_pub=ORIGIN_KW.public_key,
            inner={"kind": "moment_reaction"},
        ),
    }


async def test_relay_forwards_one_hop_back_to_the_origin(env):
    app, _db, sent, _own, _anna, router, _c = env
    payload = _relay_payload(_token(router))
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload)
    assert len(sent) == 1
    to, event_type, forwarded = sent[0]
    assert (to, event_type) == (OTHER, FET.MOMENT_REACTION_ROUTED)
    assert forwarded["rt"] == []
    assert forwarded["sealed"] == payload["sealed"]  # opaque, untouched


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(
            lambda r: (_relay_payload(_token(r)), OTHER),
            id="token handed back by another neighbour (AAD)",
        ),
        pytest.param(
            lambda r: (_relay_payload(_token(r), moment_id="m-other"), NEIGHBOUR),
            id="token for another moment",
        ),
        pytest.param(
            lambda r: (
                _relay_payload(
                    _token(
                        r,
                        expires=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                    )
                ),
                NEIGHBOUR,
            ),
            id="expired token",
        ),
        pytest.param(
            lambda r: (_relay_payload(_token(r), suite="chacha"), NEIGHBOUR),
            id="unknown rt_suite",
        ),
        pytest.param(
            lambda r: (_relay_payload("AAAA" + _token(r)[4:]), NEIGHBOUR),
            id="tampered token",
        ),
        pytest.param(
            lambda r: (_relay_payload(_token(r, prev=OLD), origin=OLD), NEIGHBOUR),
            id="previous hop older than v_67",
        ),
        pytest.param(
            lambda r: (_relay_payload(_token(r), origin="peer-elsewhere"), NEIGHBOUR),
            id="token does not lead to the origin",
        ),
    ],
)
async def test_relay_refuses_a_token_that_is_not_its_own(env, make, caplog):
    app, _db, sent, _own, _anna, router, _c = env
    payload, sender = make(router)
    with caplog.at_level("WARNING"):
        await _dispatch(app, FET.MOMENT_REACTION_ROUTED, payload, from_instance=sender)
    assert sent == []
    assert "refusing" in caplog.text


async def test_relay_rate_limits_a_neighbour_and_a_moment(env):
    app, _db, sent, _own, _anna, router, _c = env
    token = _token(router)
    for _ in range(15):
        await _dispatch(app, FET.MOMENT_REACTION_ROUTED, _relay_payload(token))
    assert len(sent) == 10  # ≤ 10 a minute on one moment from one neighbour
    for i in range(60):
        mid = f"m-{i}"
        await _dispatch(
            app,
            FET.MOMENT_REACTION_ROUTED,
            _relay_payload(_token(router, moment_id=mid), moment_id=mid),
        )
    # ≤ 60 a minute from one neighbour in all: the 15 above counted too.
    assert len(sent) == 10 + (60 - 15)


# ── Create: only a bound key-wrap key and a complete chain are kept ───────


def _far_create(
    moment_id: str, *, hop: int, kw_fields: dict, tokens: list[str]
) -> dict:
    created = sign_moment_origin(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        event_type=FET.MOMENT_CREATED,
        payload={
            "moment_id": moment_id,
            "author_user_id": "u-olga",
            "origin_instance_id": ORIGIN,
            "content": "far away",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "expires_at": EXPIRES,
            "occurred_at": datetime.now(UTC).isoformat(),
            "hop_count": 1,
        },
    )
    return {
        **created,
        **kw_fields,
        "hop_count": hop,
        "rt": tokens,
        "rt_suite": RETURN_TOKEN_SUITE_AESGCM_256,
    }


def _origin_kw_fields() -> dict:
    return origin_keywrap_fields(
        ORIGIN_KW.public_key,
        b64url_encode(sign_ed25519(ORIGIN_KEY.private_key, ORIGIN_KW.public_key)),
    )


async def _route_of(db, moment_id: str):
    row = await db.fetchone(
        "SELECT reaction_route FROM moments WHERE id=?", (moment_id,)
    )
    return None if row is None else row[0]


async def test_receiver_keeps_a_complete_bound_route(env):
    app, db, _sent, _own, _anna, _router, _c = env
    await _dispatch(
        app,
        FET.MOMENT_CREATED,
        _far_create("m-ok", hop=2, kw_fields=_origin_kw_fields(), tokens=["tok-1"]),
    )
    assert '"via":"peer-neighbour"' in (await _route_of(db, "m-ok") or "")


def _substituted_kw() -> dict:
    # A relay swaps in its own key-wrap key — it has no signature by the
    # origin's identity key over it.
    fields = _origin_kw_fields()
    fields["origin_keywrap_pk"] = generate_x25519_keypair().public_key.hex()
    return fields


def _relay_bound_kw() -> dict:
    # …or binds its key with its *own* identity key.
    relay = generate_identity_keypair()
    kw = generate_x25519_keypair()
    return origin_keywrap_fields(
        kw.public_key, b64url_encode(sign_ed25519(relay.private_key, kw.public_key))
    )


@pytest.mark.parametrize(
    ("kw_fields", "tokens", "hop"),
    [
        pytest.param(_substituted_kw(), ["tok-1"], 2, id="substituted key-wrap key"),
        pytest.param(_relay_bound_kw(), ["tok-1"], 2, id="key bound by the relay"),
        pytest.param(
            {**_origin_kw_fields(), "origin_keywrap_suite": "x448"},
            ["tok-1"],
            2,
            id="unknown key-wrap suite",
        ),
        pytest.param(_origin_kw_fields(), ["tok-1"], 3, id="a relay added no token"),
        pytest.param({}, ["tok-1"], 2, id="older origin shipped no key"),
    ],
)
async def test_receiver_keeps_no_route_it_cannot_trust(env, kw_fields, tokens, hop):
    app, db, _sent, _own, _anna, _router, _c = env
    await _dispatch(
        app,
        FET.MOMENT_CREATED,
        _far_create("m-x", hop=hop, kw_fields=kw_fields, tokens=tokens),
    )
    assert await db.fetchone("SELECT 1 FROM moments WHERE id='m-x'", ()) is not None
    assert await _route_of(db, "m-x") is None


# ── Reactor: routed only along a complete route through a v_67 neighbour ──


async def _held_far_moment(app, db, *, via: str) -> None:
    await _dispatch(
        app,
        FET.MOMENT_CREATED,
        _far_create("m-far", hop=2, kw_fields=_origin_kw_fields(), tokens=["tok-1"]),
        from_instance=via,
    )


async def test_reactor_seals_its_reaction_to_the_origin(env):
    app, db, sent, _own, anna, _router, _client = env
    await _held_far_moment(app, db, via=NEIGHBOUR)
    await app[moment_service_key].react("m-far", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    routed = [s for s in sent if s[1] is FET.MOMENT_REACTION_ROUTED]
    assert len(routed) == 1
    to, _et, payload = routed[0]
    assert to == NEIGHBOUR
    assert payload["rt"] == ["tok-1"] and payload["origin_instance_id"] == ORIGIN
    inner = open_reaction(sealed=payload["sealed"], keywrap_priv=ORIGIN_KW.private_key)
    assert verify_reaction(inner) is None
    assert (inner["reactor_user_id"], inner["emoji"]) == (anna, "👏")
    # Nothing a relay can read names the reactor or the emoji.
    assert anna not in str({k: v for k, v in payload.items() if k != "sealed"})


async def test_reactor_keeps_the_reaction_home_behind_an_older_neighbour(env):
    app, db, sent, _own, anna, _router, _client = env
    await _held_far_moment(app, db, via=OLD)
    await app[moment_service_key].react("m-far", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert [s for s in sent if s[1] is FET.MOMENT_REACTION_ROUTED] == []
    moment = await app[moment_service_key].get_moment("m-far")
    assert await app[moment_service_key].reaction_reach([moment]) == {
        "m-far": "household"
    }


async def test_existing_reaction_path_refuses_an_oversized_emoji(env):
    app, db, _sent, _own, anna, _router, _c = env
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-nora', ?, 'nora', 'Nora')",
        (NEIGHBOUR,),
    )
    await _dispatch(
        app,
        FET.MOMENT_REACTED,
        {
            "moment_id": "m-anna",
            "reactor_user_id": "u-nora",
            "author_user_id": anna,
            "emoji": "x" * 40,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert await _reactions(db) == []


# ── Review fixes (#1178): hide list, replays and restarts ─────────────────


async def test_reactor_uses_the_direct_path_to_a_paired_origin(env):
    """F1: the origin is paired but the author is not on our roster (it came
    in at hop 2) — the reaction goes straight to the origin, never routed."""
    app, db, sent, _own, anna, _router, _client = env
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key, V67)
    await _held_far_moment(app, db, via=NEIGHBOUR)
    await app[moment_service_key].react("m-far", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert [s for s in sent if s[1] is FET.MOMENT_REACTION_ROUTED] == []
    direct = [s for s in sent if s[1] is FET.MOMENT_REACTED]
    assert [(to, p["reactor_user_id"]) for to, _e, p in direct] == [(ORIGIN, anna)]


async def test_reactor_hidden_from_a_paired_origin_sends_nothing(env):
    """F1: a person hidden from the (paired) origin household reaches it
    neither directly nor along the route."""
    app, db, sent, _own, anna, _router, _client = env
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key, V67)
    await db.enqueue(
        "INSERT INTO peer_user_visibility(instance_id, user_id, visible)"
        " VALUES(?, ?, 0)",
        (ORIGIN, anna),
    )
    await _held_far_moment(app, db, via=NEIGHBOUR)
    await app[moment_service_key].react("m-far", reactor_user_id=anna, emoji="👏")
    await asyncio.sleep(0.05)
    assert [s for s in sent if s[1] is FET.MOMENT_REACTION_ROUTED] == []
    assert [s for s in sent if s[0] == ORIGIN] == []


async def test_replays_do_not_drain_the_reactor_households_budget(env):
    """F2: a relay re-sending one genuine sealed reaction is refused as a
    replay and does not use up the reactor household's rate."""
    app, db, _sent, own, anna, _router, _c = env
    genuine = _routed(app, own, _inner(own, anna, emoji="👏", at_ms=_now_ms(-30)))
    for _ in range(60):
        await _dispatch(app, FET.MOMENT_REACTION_ROUTED, genuine)
    fresh = _inner(own, anna, emoji="🔥", at_ms=_now_ms(-1))
    await _dispatch(
        app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, fresh), from_instance=OTHER
    )
    assert await _reactions(db) == [("m-anna", RITA, "🔥")]


def _restart(router) -> None:
    """What an origin restart forgets: every in-memory cache."""
    router._seen.clear()
    removed = getattr(router, "_removed", None)
    if removed is not None:
        removed.clear()


async def test_a_replayed_add_does_not_bring_back_a_removal_after_a_restart(env):
    """F3: the removal is kept on disk, so an older add replayed after the
    origin restarted (its replay cache gone) stays removed."""
    app, db, _sent, own, anna, router, _c = env
    add = _routed(app, own, _inner(own, anna, emoji="👏", at_ms=_now_ms(-60)))
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, add)
    removal = _inner(own, anna, emoji=None, at_ms=_now_ms(-30))
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, removal))
    assert await _reactions(db) == []
    _restart(router)
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, add)  # identical bytes
    assert await _reactions(db) == []


async def test_reactions_in_the_same_second_keep_their_order_after_a_restart(env):
    """F3: ``reacted_at`` keeps milliseconds, so an add replayed after a
    restart does not tie with — and overwrite — a newer one that second."""
    app, db, _sent, own, anna, router, _c = env
    base = (_now_ms(-60) // 1000) * 1000
    first = _routed(app, own, _inner(own, anna, emoji="🔥", at_ms=base + 100))
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, first)
    second = _inner(own, anna, emoji="😂", at_ms=base + 700)
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, _routed(app, own, second))
    _restart(router)
    await _dispatch(app, FET.MOMENT_REACTION_ROUTED, first)  # identical bytes
    assert await _reactions(db) == [("m-anna", RITA, "😂")]

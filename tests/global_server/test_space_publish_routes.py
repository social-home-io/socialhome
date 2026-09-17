"""Tests for the global-space publish surface (``GET /gfs/spaces/{id}``,
``POST /gfs/spaces/{id}/publish``, ``DELETE /gfs/spaces/{id}/unpublish``)
plus the matching :class:`GfsFederationService` methods.

The publish wire flow is what an SH-side ``GfsConnectionService.publish_space``
hits when a household flips a local space to ``space_type=global``.
The GFS verifies the Ed25519 signature against the registered
``ClientInstance.public_key`` and upserts a ``GlobalSpace`` row at
``status='active'`` (or ``pending`` if the GFS admin has
``auto_accept_clients=0``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
)
from socialhome.global_server.domain import ClientInstance, GlobalSpace


@pytest.fixture
async def gfs_client(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def _register_owner(
    app,
    *,
    instance_id: str = "owner.home",
    auto_accept: bool = True,
) -> tuple[bytes, bytes]:
    """Insert a ClientInstance row and return its (seed, public-key-bytes).

    Tests sign their own publish bodies with ``seed`` so the signature
    verifies against the stored ``ClientInstance.public_key``.
    """
    kp = generate_identity_keypair()
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key=kp.public_key.hex(),
            inbox_url="https://owner.example/federation/inbox/x",
            status="active",
            auto_accept=auto_accept,
        )
    )
    return kp.private_key, kp.public_key


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_iso_offset(*, minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _sign_publish_body(body: dict, *, seed: bytes, ts: str | None = None) -> dict:
    """Compute the canonical signature the route verifier expects.

    Mirrors the body-shape ``GfsFederationService.publish_space``
    canonicalises before verifying — sorted keys, no whitespace,
    no ``signature`` field included in the signed bytes.

    ``ts`` (optional) folds a signed, replay-guarded timestamp into the body —
    the shape a current household sends, and the only one that can restore an
    owner-withdrawn listing. Omitting it reproduces a legacy household.
    """
    # The service canonicalises ``identity_public_key`` (Phase 5a TOFU pin)
    # into the signed body, defaulting to "" when none is supplied; mirror that
    # here so the recomputed canonical matches.
    body = {"identity_public_key": "", **body}
    if ts is not None:
        body = {**body, "ts": ts}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


# ── GET /gfs/spaces/{id} ────────────────────────────────────────────────


async def test_get_space_detail_returns_active_row(gfs_client):
    """A published, active space surfaces under ``GET /gfs/spaces/{id}``."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-detail",
            owning_instance="owner.home",
            name="Detail Space",
            description="hello",
            cover_url="https://cdn.example/cover.jpg",
            status="active",
            published_at="2026-05-10T00:00:00Z",
        )
    )
    resp = await gfs_client.get("/gfs/spaces/sp-detail")
    assert resp.status == 200
    body = await resp.json()
    assert body["space_id"] == "sp-detail"
    assert body["name"] == "Detail Space"
    assert body["cover_url"] == "https://cdn.example/cover.jpg"
    assert body["status"] == "active"


async def test_get_space_detail_404_when_missing(gfs_client):
    resp = await gfs_client.get("/gfs/spaces/nope")
    assert resp.status == 404


async def test_get_space_detail_404_when_banned(gfs_client):
    """Banned rows stay in the DB (audit trail) but disappear from the
    public surface — the same rule the listing endpoint follows."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-banned",
            owning_instance="owner.home",
            name="Banned",
            status="banned",
        )
    )
    resp = await gfs_client.get("/gfs/spaces/sp-banned")
    assert resp.status == 404


# ── POST /gfs/spaces/{id}/publish ───────────────────────────────────────


async def test_publish_space_happy_path_active(gfs_client):
    """Auto-accepted owner with a valid signature lands as ``active``
    and shows up on ``GET /gfs/spaces``."""
    seed, _pk = await _register_owner(gfs_client.server.app, auto_accept=True)
    body = _sign_publish_body(
        {
            "space_id": "sp-1",
            "owning_instance": "owner.home",
            "name": "Local Birds",
            "description": "everyday birds in the neighbourhood",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-1/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "active", "space_id": "sp-1"}

    listing = await gfs_client.get("/gfs/spaces")
    items = (await listing.json())["spaces"]
    assert any(sp["space_id"] == "sp-1" and sp["name"] == "Local Birds" for sp in items)


async def test_publish_space_pending_when_auto_accept_off(gfs_client):
    """A registered-but-not-auto-accepted owner lands as ``pending``;
    the row exists in the DB but the public listing hides it."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=False)
    body = _sign_publish_body(
        {
            "space_id": "sp-pending",
            "owning_instance": "owner.home",
            "name": "Pending",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-pending/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload["status"] == "pending"

    listing = await gfs_client.get("/gfs/spaces")
    items = (await listing.json())["spaces"]
    assert all(sp["space_id"] != "sp-pending" for sp in items)


async def test_publish_space_missing_field_400(gfs_client):
    """``name`` is required by the route — missing it returns 400 even
    before signature verification kicks in."""
    seed, _pk = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {
            "space_id": "sp-bad",
            "owning_instance": "owner.home",
            # ``name`` deliberately missing
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-bad/publish", json=body)
    assert resp.status == 400


async def test_publish_space_unknown_instance_403(gfs_client):
    """An owner the GFS hasn't registered cannot publish — 403."""
    body = {
        "space_id": "sp-unknown",
        "owning_instance": "ghost.home",
        "name": "Ghost",
        "signature": "AAAA",
    }
    resp = await gfs_client.post("/gfs/spaces/sp-unknown/publish", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "ghost.home" in err["error"]


async def test_publish_space_invalid_signature_403(gfs_client):
    """The owner is known but the body signature fails verification —
    403 with ``Invalid Ed25519 signature``."""
    await _register_owner(gfs_client.server.app)
    # Sign with a *different* keypair than the one we registered on
    # the server. The body shape is otherwise valid; the verify call
    # is what trips.
    other_seed = generate_identity_keypair().private_key
    body = _sign_publish_body(
        {
            "space_id": "sp-badsig",
            "owning_instance": "owner.home",
            "name": "Wrong Key",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=other_seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-badsig/publish", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "signature" in err["error"].lower()


async def test_publish_space_preserves_subscriber_count_across_publishes(gfs_client):
    """Subscriber count + posts_per_week + published_at belong to the
    GFS — a re-publish with new metadata must not zero them out."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    # Seed a row with non-zero counts.
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-keep",
            owning_instance="owner.home",
            name="Old Name",
            status="active",
            subscriber_count=42,
            posts_per_week=3.5,
            published_at="2026-05-01T00:00:00Z",
        )
    )
    body = _sign_publish_body(
        {
            "space_id": "sp-keep",
            "owning_instance": "owner.home",
            "name": "New Name",
            "description": "freshly renamed",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-keep/publish", json=body)
    assert resp.status == 200
    detail = await (await gfs_client.get("/gfs/spaces/sp-keep")).json()
    assert detail["name"] == "New Name"
    assert detail["subscriber_count"] == 42
    assert detail["posts_per_week"] == 3.5
    assert detail["published_at"] == "2026-05-01T00:00:00Z"


async def test_publish_space_cannot_unban_a_banned_row(gfs_client):
    """A banned space stays banned even if the owner re-publishes —
    only the GFS admin can lift the ban (see admin portal)."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-banned-2",
            owning_instance="owner.home",
            name="Banned",
            status="banned",
        )
    )
    body = _sign_publish_body(
        {
            "space_id": "sp-banned-2",
            "owning_instance": "owner.home",
            "name": "Re-Published",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-banned-2/publish", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload["status"] == "banned"


# ── DELETE /gfs/spaces/{id}/unpublish ───────────────────────────────────


def _sign_unpublish(
    space_id: str,
    *,
    seed: bytes,
    owning_instance: str = "owner.home",
    ts: str | None = None,
    action: str = "unpublish",
) -> dict:
    """Build the signed unpublish body the GFS verifies.

    Mirrors ``GfsFederationService.hide_space``'s canonical payload
    ``{action, owning_instance, space_id, ts}``. ``action`` is settable so a
    test can sign a DIFFERENT action (domain separation) and watch the
    server reject the captured signature.
    """
    ts = ts or datetime.now(timezone.utc).isoformat()
    canonical = json.dumps(
        {
            "action": action,
            "owning_instance": owning_instance,
            "space_id": space_id,
            "ts": ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "owning_instance": owning_instance,
        "ts": ts,
        "signature": b64url_encode(sign_ed25519(seed, canonical)),
    }


async def _listed(gfs_client, space_id: str) -> bool:
    """True when *space_id* shows up on the public ``GET /gfs/spaces`` list."""
    listing = await gfs_client.get("/gfs/spaces")
    items = (await listing.json())["spaces"]
    return any(sp["space_id"] == space_id for sp in items)


async def test_unpublish_space_unsigned_is_rejected_and_stays_listed(gfs_client):
    """REGRESSION (security): ``unpublish`` used to take NO authentication at
    all, so any internet caller could delist any space. An empty signature is
    now a hard 403 and the space stays publicly listed."""
    app = gfs_client.server.app
    await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-unsigned",
            owning_instance="owner.home",
            name="Still Here",
            status="active",
        )
    )
    resp = await gfs_client.delete(
        "/gfs/spaces/sp-unsigned/unpublish",
        json={
            "owning_instance": "owner.home",
            "ts": datetime.now(timezone.utc).isoformat(),
            "signature": "",
        },
    )
    assert resp.status == 403
    assert await _listed(gfs_client, "sp-unsigned")
    assert (await gfs_client.get("/gfs/spaces/sp-unsigned")).status == 200


async def test_unpublish_space_by_non_owner_403(gfs_client):
    """A REGISTERED household that is not the owner has a perfectly valid
    signature — authentication alone is not authorization, so the owner check
    must reject it."""
    app = gfs_client.server.app
    await _register_owner(app)
    attacker_seed, _pk = await _register_owner(app, instance_id="attacker.home")
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-notmine",
            owning_instance="owner.home",
            name="Not Yours",
            status="active",
        )
    )
    body = _sign_unpublish(
        "sp-notmine",
        seed=attacker_seed,
        owning_instance="attacker.home",
    )
    resp = await gfs_client.delete("/gfs/spaces/sp-notmine/unpublish", json=body)
    assert resp.status == 403
    assert await _listed(gfs_client, "sp-notmine")


async def test_unpublish_space_stale_ts_403(gfs_client):
    """The ±300 s replay guard rejects a captured, correctly-signed body."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-stale",
            owning_instance="owner.home",
            name="Stale",
            status="active",
        )
    )
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    body = _sign_unpublish("sp-stale", seed=seed, ts=stale)
    resp = await gfs_client.delete("/gfs/spaces/sp-stale/unpublish", json=body)
    assert resp.status == 403
    assert await _listed(gfs_client, "sp-stale")


async def test_unpublish_space_signature_for_other_action_403(gfs_client):
    """Domain separation: a signature captured from a ``subscribe`` request
    can't be replayed as an unpublish — ``action`` is inside the signed bytes."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-action",
            owning_instance="owner.home",
            name="Action",
            status="active",
        )
    )
    body = _sign_unpublish("sp-action", seed=seed, action="subscribe")
    resp = await gfs_client.delete("/gfs/spaces/sp-action/unpublish", json=body)
    assert resp.status == 403
    assert await _listed(gfs_client, "sp-action")


async def test_unpublish_space_missing_field_400(gfs_client):
    """Each of the three body fields is required — a missing one is a 400
    (bad request), distinct from the 403 an unauthorized caller gets."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    full = _sign_unpublish("sp-400", seed=seed)
    for missing in ("owning_instance", "ts", "signature"):
        body = {k: v for k, v in full.items() if k != missing}
        resp = await gfs_client.delete("/gfs/spaces/sp-400/unpublish", json=body)
        assert resp.status == 400, missing


async def test_unpublish_space_signed_by_owner_withdraws_listing(gfs_client):
    """The happy path: a correctly-signed owner withdrawal drops the space
    from the public listing and detail endpoint, WITHOUT flipping ``status``
    to ``banned`` (that state belongs to the GFS moderator)."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-bye",
            owning_instance="owner.home",
            name="Bye",
            status="active",
            subscriber_count=10,
        )
    )
    body = _sign_unpublish("sp-bye", seed=seed)
    resp = await gfs_client.delete("/gfs/spaces/sp-bye/unpublish", json=body)
    assert resp.status == 200
    assert await resp.json() == {"status": "unpublished"}
    assert not await _listed(gfs_client, "sp-bye")
    assert (await gfs_client.get("/gfs/spaces/sp-bye")).status == 404
    # Row survives for the audit trail; status is untouched, withdrawn is set.
    sp = await fed_repo.get_space("sp-bye")
    assert sp is not None
    assert sp.status == "active"
    assert sp.withdrawn is True
    assert sp.subscriber_count == 10


async def test_unpublish_space_post_method_also_works(gfs_client):
    """The router accepts both POST and DELETE on the unpublish endpoint
    (some HTTP clients struggle with DELETE bodies) — both are signed."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-bye-2",
            owning_instance="owner.home",
            name="Bye2",
            status="active",
        )
    )
    body = _sign_unpublish("sp-bye-2", seed=seed)
    resp = await gfs_client.post("/gfs/spaces/sp-bye-2/unpublish", json=body)
    assert resp.status == 200
    assert not await _listed(gfs_client, "sp-bye-2")


async def test_unpublish_space_idempotent_on_missing(gfs_client):
    """Unpublishing a space this GFS never saw is a signed no-op — the
    existence of the space is never leaked to an UNSIGNED caller (the
    signature is checked first), but a legitimate owner retrying after a
    partial fan-out sees a clean 200."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app)
    body = _sign_unpublish("nope", seed=seed)
    resp = await gfs_client.delete("/gfs/spaces/nope/unpublish", json=body)
    assert resp.status == 200
    assert await resp.json() == {"status": "unpublished"}


_BACK_META = {
    "owning_instance": "owner.home",
    "name": "Back Again",
    "description": "",
    "about_markdown": "",
    "cover_url": "",
    "min_age": 0,
    "category": "general",
    "accent_color": "#D2542A",
    "icon_url": "",
    "primary_color": "#D2542A",
}


async def test_republish_after_withdrawal_restores_listing(gfs_client):
    """The recovery path: owner withdrawal is REVERSIBLE — a later FRESH
    publish (one carrying a signed, replay-guarded ``ts``) from the same owner
    puts the space back on the public list."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    meta = {**_BACK_META, "space_id": "sp-back"}
    publish = _sign_publish_body(meta, seed=seed, ts=_now_iso())
    assert (
        await gfs_client.post("/gfs/spaces/sp-back/publish", json=publish)
    ).status == 200
    assert await _listed(gfs_client, "sp-back")

    body = _sign_unpublish("sp-back", seed=seed)
    assert (
        await gfs_client.delete("/gfs/spaces/sp-back/unpublish", json=body)
    ).status == 200
    assert not await _listed(gfs_client, "sp-back")

    # Re-publish, freshly signed → visible again (a ban would NOT come back).
    republish = _sign_publish_body(meta, seed=seed, ts=_now_iso())
    assert (
        await gfs_client.post("/gfs/spaces/sp-back/publish", json=republish)
    ).status == 200
    assert await _listed(gfs_client, "sp-back")
    assert (await gfs_client.get("/gfs/spaces/sp-back")).status == 200


async def test_replayed_stale_publish_cannot_undo_withdrawal(gfs_client):
    """SECURITY: a captured publish body is worthless once its signed ``ts``
    goes stale — it is rejected outright and the space stays withdrawn.

    Without the timestamp inside the signed bytes, anyone holding one
    historical publish body could re-list a space its owner deliberately
    delisted, repeatedly and indefinitely.
    """
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    meta = {**_BACK_META, "space_id": "sp-replay", "name": "Replay"}
    assert (
        await gfs_client.post(
            "/gfs/spaces/sp-replay/publish",
            json=_sign_publish_body(meta, seed=seed, ts=_now_iso()),
        )
    ).status == 200
    assert (
        await gfs_client.delete(
            "/gfs/spaces/sp-replay/unpublish",
            json=_sign_unpublish("sp-replay", seed=seed),
        )
    ).status == 200

    stale = _now_iso_offset(minutes=-30)
    resp = await gfs_client.post(
        "/gfs/spaces/sp-replay/publish",
        json=_sign_publish_body(meta, seed=seed, ts=stale),
    )
    assert resp.status == 403
    assert not await _listed(gfs_client, "sp-replay")
    stored = await fed_repo.get_space("sp-replay")
    assert stored is not None
    assert stored.withdrawn is True


async def test_legacy_publish_without_ts_updates_metadata_but_keeps_withdrawn(
    gfs_client,
):
    """Backward compatibility: an older household sends no ``ts``. Its publish
    still registers and refreshes metadata (so the core feature keeps working
    during a mixed-version window), but its body is replayable forever, so it
    must NOT restore an owner-withdrawn listing."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    meta = {**_BACK_META, "space_id": "sp-legacy", "name": "Legacy"}
    assert (
        await gfs_client.post(
            "/gfs/spaces/sp-legacy/publish",
            json=_sign_publish_body(meta, seed=seed),
        )
    ).status == 200
    assert await _listed(gfs_client, "sp-legacy")
    assert (
        await gfs_client.delete(
            "/gfs/spaces/sp-legacy/unpublish",
            json=_sign_unpublish("sp-legacy", seed=seed),
        )
    ).status == 200

    renamed = {**meta, "name": "Legacy Renamed"}
    resp = await gfs_client.post(
        "/gfs/spaces/sp-legacy/publish",
        json=_sign_publish_body(renamed, seed=seed),
    )
    assert resp.status == 200
    stored = await fed_repo.get_space("sp-legacy")
    assert stored is not None
    assert stored.name == "Legacy Renamed"  # metadata still refreshes
    assert stored.withdrawn is True  # …but the listing stays withdrawn
    assert not await _listed(gfs_client, "sp-legacy")


async def test_legacy_publish_without_ts_still_publishes_fresh_space(gfs_client):
    """A legacy (no-``ts``) publish of a space that was never withdrawn is
    listed normally — the compatibility branch is not a silent downgrade."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    meta = {**_BACK_META, "space_id": "sp-legacy-new", "name": "Legacy New"}
    assert (
        await gfs_client.post(
            "/gfs/spaces/sp-legacy-new/publish",
            json=_sign_publish_body(meta, seed=seed),
        )
    ).status == 200
    assert await _listed(gfs_client, "sp-legacy-new")


async def test_publish_with_naive_ts_is_403(gfs_client):
    """A signed but timezone-naive ``ts`` is untrusted — rejected, not
    silently treated as UTC."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    meta = {**_BACK_META, "space_id": "sp-naive", "name": "Naive"}
    naive = datetime.now().replace(tzinfo=None).isoformat()
    resp = await gfs_client.post(
        "/gfs/spaces/sp-naive/publish",
        json=_sign_publish_body(meta, seed=seed, ts=naive),
    )
    assert resp.status == 403


async def test_admin_ban_survives_withdrawal_and_republish(gfs_client):
    """Moderation is NOT reversible by the owner: a banned space stays
    banned (and hidden) through a withdraw + re-publish cycle."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    publish = _sign_publish_body(
        {
            "space_id": "sp-banned-3",
            "owning_instance": "owner.home",
            "name": "Naughty",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    await gfs_client.post("/gfs/spaces/sp-banned-3/publish", json=publish)
    await fed_repo.set_space_status("sp-banned-3", "banned")

    body = _sign_unpublish("sp-banned-3", seed=seed)
    await gfs_client.delete("/gfs/spaces/sp-banned-3/unpublish", json=body)
    resp = await gfs_client.post("/gfs/spaces/sp-banned-3/publish", json=publish)
    assert resp.status == 200
    assert (await resp.json())["status"] == "banned"
    assert not await _listed(gfs_client, "sp-banned-3")
    stored = await fed_repo.get_space("sp-banned-3")
    assert stored is not None
    assert stored.status == "banned"


async def test_unpublish_preserves_branding_fields(gfs_client):
    """Withdrawal must not silently drop ``icon_url`` / ``primary_color`` —
    the old row round-trip through ``upsert_space`` dropped both."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    publish = _sign_publish_body(
        {
            "space_id": "sp-brand",
            "owning_instance": "owner.home",
            "name": "Branded",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#123456",
            "icon_url": "data:image/webp;base64,AAAA",
            "primary_color": "#654321",
        },
        seed=seed,
        ts=_now_iso(),
    )
    await gfs_client.post("/gfs/spaces/sp-brand/publish", json=publish)
    body = _sign_unpublish("sp-brand", seed=seed)
    assert (
        await gfs_client.delete("/gfs/spaces/sp-brand/unpublish", json=body)
    ).status == 200

    stored = await fed_repo.get_space("sp-brand")
    assert stored is not None
    assert stored.icon_url == "data:image/webp;base64,AAAA"
    assert stored.primary_color == "#654321"
    assert stored.accent_color == "#123456"

    # And they survive the re-publish round-trip too.
    await gfs_client.post("/gfs/spaces/sp-brand/publish", json=publish)
    detail = await (await gfs_client.get("/gfs/spaces/sp-brand")).json()
    assert detail["icon_url"] == "data:image/webp;base64,AAAA"
    assert detail["primary_color"] == "#654321"


async def test_unpublish_preserves_tofu_pinned_identity_key(gfs_client):
    """Withdrawal must not clear the TOFU-pinned space authority key, and a
    re-publish offering a DIFFERENT key must still keep the pinned one — the
    withdrawn row has to stay visible to ``publish_space``'s owner/TOFU
    checks, or a re-publish would look like a first publish."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    fed_repo = app[gfs_fed_repo_key]
    pinned = "11" * 32
    base = {
        "space_id": "sp-tofu",
        "owning_instance": "owner.home",
        "name": "Pinned",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "icon_url": "",
        "primary_color": "#D2542A",
    }
    first = _sign_publish_body({**base, "identity_public_key": pinned}, seed=seed)
    await gfs_client.post("/gfs/spaces/sp-tofu/publish", json=first)

    body = _sign_unpublish("sp-tofu", seed=seed)
    await gfs_client.delete("/gfs/spaces/sp-tofu/unpublish", json=body)
    stored = await fed_repo.get_space("sp-tofu")
    assert stored is not None
    assert stored.identity_public_key == pinned
    assert stored.owning_instance == "owner.home"

    swapped = _sign_publish_body({**base, "identity_public_key": "22" * 32}, seed=seed)
    assert (
        await gfs_client.post("/gfs/spaces/sp-tofu/publish", json=swapped)
    ).status == 200
    stored = await fed_repo.get_space("sp-tofu")
    assert stored is not None
    assert stored.identity_public_key == pinned


# ── POST /gfs/instance (signed display-name update) ─────────────────────


def _sign_body(body: dict, *, seed: bytes) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


async def test_instance_update_happy_path_200(gfs_client):
    """A registered instance renames itself with a valid signature + fresh ts."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="rename.home")
    ts = datetime.now(timezone.utc).isoformat()
    body = _sign_body(
        {"instance_id": "rename.home", "display_name": "Fresh Name", "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 200
    payload = await resp.json()
    assert payload == {"status": "ok", "instance_id": "rename.home"}

    stored = await app[gfs_fed_repo_key].get_instance("rename.home")
    assert stored is not None
    assert stored.display_name == "Fresh Name"


async def test_instance_update_unknown_instance_403(gfs_client):
    """An instance the GFS hasn't registered can't rename — 403."""
    ts = datetime.now(timezone.utc).isoformat()
    body = {
        "instance_id": "ghost.home",
        "display_name": "Ghost",
        "ts": ts,
        "signature": "AAAA",
    }
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403


async def test_instance_update_bad_signature_403(gfs_client):
    """A signature from the wrong key fails verification — 403."""
    app = gfs_client.server.app
    await _register_owner(app, instance_id="known.home")
    other_seed = generate_identity_keypair().private_key
    ts = datetime.now(timezone.utc).isoformat()
    body = _sign_body(
        {"instance_id": "known.home", "display_name": "Hijack", "ts": ts},
        seed=other_seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403
    err = await resp.json()
    assert "signature" in err["error"].lower()


async def test_instance_update_stale_timestamp_403(gfs_client):
    """A stale ts is rejected with 403 (replay guard)."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="stale.home")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    body = _sign_body(
        {"instance_id": "stale.home", "display_name": "Late", "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 403


async def test_instance_update_overlong_name_422(gfs_client):
    """A >80-char display_name maps to 422."""
    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, instance_id="long.home")
    ts = datetime.now(timezone.utc).isoformat()
    name = "z" * 81
    body = _sign_body(
        {"instance_id": "long.home", "display_name": name, "ts": ts},
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/instance", json=body)
    assert resp.status == 422


async def test_instance_update_missing_field_400(gfs_client):
    """A missing required field is a 400 before any verification."""
    app = gfs_client.server.app
    await _register_owner(app, instance_id="incomplete.home")
    resp = await gfs_client.post(
        "/gfs/instance",
        json={"instance_id": "incomplete.home"},
    )
    assert resp.status == 400


async def test_publish_space_caps_about_markdown(gfs_client):
    """An oversized about_markdown is truncated at storage (DB-bloat /
    render-cost guard) — the signature still validates the full value."""
    from socialhome.global_server.federation import MAX_ABOUT_MARKDOWN_CHARS

    app = gfs_client.server.app
    seed, _pk = await _register_owner(app, auto_accept=True)
    big = "x" * (MAX_ABOUT_MARKDOWN_CHARS + 5000)
    body = _sign_publish_body(
        {
            "space_id": "sp-big",
            "owning_instance": "owner.home",
            "name": "Big",
            "description": "",
            "about_markdown": big,
            "cover_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "icon_url": "",
            "primary_color": "#D2542A",
        },
        seed=seed,
    )
    resp = await gfs_client.post("/gfs/spaces/sp-big/publish", json=body)
    assert resp.status == 200
    stored = await app[gfs_fed_repo_key].get_space("sp-big")
    assert stored is not None
    assert len(stored.about_markdown) == MAX_ABOUT_MARKDOWN_CHARS

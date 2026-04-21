"""End-to-end tests for the GFS admin API endpoints (§24.9)."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from social_home.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from social_home.global_server.admin import hash_password
from social_home.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
)
from social_home.global_server.config import GfsConfig
from social_home.global_server.domain import ClientInstance, GlobalSpace
from social_home.global_server.server import create_gfs_app


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
        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("admin-pw"),
        )
        login = await tc.post("/admin/login", json={"password": "admin-pw"})
        assert login.status == 200
        tc._app = app
        yield tc


# ── Overview ───────────────────────────────────────────────────────────


async def test_overview_returns_zero_counts_on_empty_db(client):
    resp = await client.get("/admin/api/overview")
    assert resp.status == 200
    body = await resp.json()
    assert body["clients"] == {"active": 0, "pending": 0}
    assert body["spaces"] == {"active": 0, "pending": 0}
    assert body["open_reports"] == 0


# ── Clients ────────────────────────────────────────────────────────────


async def test_clients_list_accept_ban_unban(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="pascal.home",
            display_name="Pascal",
            public_key="aa" * 32,
            endpoint_url="http://p.example/wh",
            status="pending",
        )
    )
    resp = await client.get("/admin/api/clients?status=pending")
    assert resp.status == 200
    assert len((await resp.json())) == 1

    # Accept → moves to active.
    resp = await client.post("/admin/api/clients/pascal.home/accept")
    assert resp.status == 200
    assert await (await client.get("/admin/api/clients?status=active")).json()

    # Ban → moves to banned AND bans owned spaces.
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="space-p",
            owning_instance="pascal.home",
            name="Pascal's Space",
            status="active",
        )
    )
    resp = await client.post(
        "/admin/api/clients/pascal.home/ban",
        json={"reason": "spam"},
    )
    assert resp.status == 200
    # The space is now banned.
    sp = await fed_repo.get_space("space-p")
    assert sp is not None and sp.status == "banned"

    # Unban → back to pending.
    resp = await client.post("/admin/api/clients/pascal.home/unban")
    assert resp.status == 200
    inst = await fed_repo.get_instance("pascal.home")
    assert inst.status == "pending"


# ── Spaces ─────────────────────────────────────────────────────────────


async def test_spaces_accept_reject_ban(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key="bb" * 32,
            endpoint_url="http://o.example/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="space-q",
            owning_instance="owner.home",
            name="Makers",
            status="pending",
        )
    )
    # Accept.
    resp = await client.post("/admin/api/spaces/space-q/accept")
    assert resp.status == 200
    # Ban.
    resp = await client.post(
        "/admin/api/spaces/space-q/ban", json={"reason": "off-topic"}
    )
    assert resp.status == 200
    # Public /gfs/spaces no longer lists it.
    resp = await client.get("/gfs/spaces")
    assert resp.status == 200
    assert (await resp.json())["spaces"] == []


# ── Policy + branding ──────────────────────────────────────────────────


async def test_policy_patch_persists(client):
    resp = await client.patch(
        "/admin/api/policy",
        json={
            "auto_accept_clients": False,
            "auto_accept_spaces": True,
            "fraud_threshold": 10,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "auto_accept_clients": False,
        "auto_accept_spaces": True,
        "fraud_threshold": 10,
    }
    # GET reflects the write.
    resp = await client.get("/admin/api/policy")
    assert (await resp.json())["fraud_threshold"] == 10


async def test_branding_patch_persists(client):
    resp = await client.patch(
        "/admin/api/branding",
        json={
            "server_name": "Test GFS",
            "landing_markdown": "# hi",
            "header_image_file": "hero.webp",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["server_name"] == "Test GFS"
    assert body["landing_markdown"] == "# hi"


# ── Fraud reports ──────────────────────────────────────────────────────


def _signed_report_body(
    kp,
    *,
    target_type="space",
    target_id="space-bad",
    category="spam",
    reporter="reporter.home",
    reporter_user="u-1",
    notes=None,
):
    body = {
        "target_type": target_type,
        "target_id": target_id,
        "category": category,
        "notes": notes,
        "reporter_instance_id": reporter,
        "reporter_user_id": reporter_user,
        "created_at": "2026-04-19T00:00:00Z",
    }
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    signature = sign_ed25519(kp.private_key, canonical)
    body["signature"] = b64url_encode(signature)
    return body


async def test_fraud_report_happy_path_records_row(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="Reporter",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r.example/wh",
            status="active",
        )
    )
    resp = await client.post("/gfs/report", json=_signed_report_body(kp))
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "recorded"
    # Listed in admin reports.
    resp = await client.get("/admin/api/reports?status=pending")
    assert len((await resp.json())) == 1


async def test_fraud_report_unknown_reporter_is_403(client):
    kp = generate_identity_keypair()
    resp = await client.post("/gfs/report", json=_signed_report_body(kp))
    assert resp.status == 403


async def test_fraud_report_bad_signature_is_401(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="Reporter",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r.example/wh",
            status="active",
        )
    )
    body = _signed_report_body(kp)
    body["signature"] = "0" * 88  # bogus
    resp = await client.post("/gfs/report", json=body)
    assert resp.status == 401


async def test_threshold_crossing_auto_bans_space(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    admin_repo = app[gfs_admin_repo_key]
    # Lower the threshold for the test.
    await admin_repo.set_config("fraud_threshold", "2")
    # Rebuild the admin service with the new threshold (it cached at init).
    from social_home.global_server.app_keys import gfs_admin_service_key

    app[gfs_admin_service_key]._fraud_threshold = 2

    # Two reporters cross the threshold.
    keys: dict[str, object] = {}
    for name in ("reporter-a", "reporter-b"):
        kp = generate_identity_keypair()
        keys[name] = kp
        await fed_repo.upsert_instance(
            ClientInstance(
                instance_id=name,
                display_name=name,
                public_key=kp.public_key.hex(),
                endpoint_url=f"http://{name}.example/wh",
                status="active",
            )
        )

    # Seed the target space (owner must exist first for FK).
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="space-bad",
            owning_instance="reporter-a",
            name="Bad",
            status="active",
        )
    )

    # Both reporters file — second crosses threshold and auto-bans.
    for name in ("reporter-a", "reporter-b"):
        body = _signed_report_body(keys[name], reporter=name)
        resp = await client.post("/gfs/report", json=body)
        assert resp.status == 200

    # Re-check the final state via admin API: targeted space banned.
    resp = await client.get("/admin/api/reports?status=pending")
    pending = await resp.json()
    # All reports have been marked 'acted' when threshold was crossed.
    assert pending == []
    # The target space was set to banned.
    resp = await client.get("/admin/api/spaces?status=banned")
    banned = await resp.json()
    assert any(s["space_id"] == "space-bad" for s in banned)


async def test_report_review_dismiss(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="R",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r/wh",
            status="active",
        )
    )
    await client.post("/gfs/report", json=_signed_report_body(kp))
    reports = await (await client.get("/admin/api/reports?status=pending")).json()
    rid = reports[0]["id"]
    resp = await client.post(
        f"/admin/api/reports/{rid}/review",
        json={"action": "dismiss"},
    )
    assert resp.status == 200
    # Now dismissed.
    resp = await client.get("/admin/api/reports?status=dismissed")
    assert len((await resp.json())) == 1


# ── Audit log ──────────────────────────────────────────────────────────


async def test_audit_log_records_accept_and_ban(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="pascal.home",
            display_name="Pascal",
            public_key="aa" * 32,
            endpoint_url="http://p/wh",
            status="pending",
        )
    )
    await client.post("/admin/api/clients/pascal.home/accept")
    await client.post("/admin/api/clients/pascal.home/ban", json={})
    resp = await client.get("/admin/api/audit?limit=10")
    actions = [r["action"] for r in await resp.json()]
    assert "accept_client" in actions
    assert "ban_client" in actions


# ── Appeals ────────────────────────────────────────────────────────────


async def test_appeal_roundtrip(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    admin_svc = app[
        __import__(
            "social_home.global_server.app_keys", fromlist=["*"]
        ).gfs_admin_service_key
    ]
    # Seed a banned space.
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="banned.home",
            display_name="Banned",
            public_key="cc" * 32,
            endpoint_url="http://b/wh",
            status="banned",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="banned-space",
            owning_instance="banned.home",
            name="Banned Space",
            status="banned",
        )
    )
    # Household files an appeal directly via the service (wire path later).
    appeal = await admin_svc.record_appeal(
        target_type="space",
        target_id="banned-space",
        message="false positive, we think",
    )
    # Admin lists + lifts.
    resp = await client.get("/admin/api/appeals?status=pending")
    lst = await resp.json()
    assert any(a["id"] == appeal.id for a in lst)
    resp = await client.post(
        f"/admin/api/appeals/{appeal.id}/decide",
        json={"action": "lift"},
    )
    assert resp.status == 200
    # Space is now pending again.
    sp = await fed_repo.get_space("banned-space")
    assert sp.status == "pending"


async def test_appeal_decide_unknown_is_404(client):
    resp = await client.post(
        "/admin/api/appeals/missing/decide",
        json={"action": "lift"},
    )
    assert resp.status == 404


async def test_appeal_decide_bad_action_is_422(client):
    app = client._app
    admin_svc = app[
        __import__(
            "social_home.global_server.app_keys", fromlist=["*"]
        ).gfs_admin_service_key
    ]
    appeal = await admin_svc.record_appeal(
        target_type="space",
        target_id="x",
        message="",
    )
    resp = await client.post(
        f"/admin/api/appeals/{appeal.id}/decide",
        json={"action": "bogus"},
    )
    assert resp.status == 422


# ── Dismiss + double-review handling ──────────────────────────────────


async def test_double_review_returns_409(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="R",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r/wh",
            status="active",
        )
    )
    await client.post("/gfs/report", json=_signed_report_body(kp))
    reports = await (await client.get("/admin/api/reports?status=pending")).json()
    rid = reports[0]["id"]
    await client.post(
        f"/admin/api/reports/{rid}/review",
        json={"action": "dismiss"},
    )
    resp = await client.post(
        f"/admin/api/reports/{rid}/review",
        json={"action": "dismiss"},
    )
    assert resp.status == 409


async def test_review_unknown_report_is_404(client):
    resp = await client.post(
        "/admin/api/reports/missing/review",
        json={"action": "dismiss"},
    )
    assert resp.status == 404


async def test_review_bad_action_is_422(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="R",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r/wh",
            status="active",
        )
    )
    await client.post("/gfs/report", json=_signed_report_body(kp))
    rid = (await (await client.get("/admin/api/reports?status=pending")).json())[0][
        "id"
    ]
    resp = await client.post(
        f"/admin/api/reports/{rid}/review",
        json={"action": "bogus"},
    )
    assert resp.status == 422


async def test_report_missing_fields_is_422(client):
    resp = await client.post("/gfs/report", json={"target_type": "space"})
    assert resp.status == 422


async def test_report_ban_instance_action(client):
    """Review action=ban_instance bans the space's owning household."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    # The space we're going to report is owned by a household we want to ban.
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key="dd" * 32,
            endpoint_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="suspect-space",
            owning_instance="owner.home",
            name="Suspect",
            status="active",
        )
    )
    # Reporter (separate instance).
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="reporter.home",
            display_name="R",
            public_key=kp.public_key.hex(),
            endpoint_url="http://r/wh",
            status="active",
        )
    )
    await client.post(
        "/gfs/report",
        json=_signed_report_body(
            kp,
            target_id="suspect-space",
        ),
    )
    rid = (await (await client.get("/admin/api/reports?status=pending")).json())[0][
        "id"
    ]
    resp = await client.post(
        f"/admin/api/reports/{rid}/review",
        json={"action": "ban_instance"},
    )
    assert resp.status == 200
    inst = await fed_repo.get_instance("owner.home")
    assert inst.status == "banned"


# ── Public /gfs/spaces filters ─────────────────────────────────────────


async def test_client_reject_removes_row(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="rej.home",
            display_name="Rej",
            public_key="ff" * 32,
            endpoint_url="http://rej/wh",
            status="pending",
        )
    )
    resp = await client.post("/admin/api/clients/rej.home/reject")
    assert resp.status == 200
    assert await fed_repo.get_instance("rej.home") is None


async def test_space_reject_deletes_row(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o2.home",
            display_name="O",
            public_key="aa" * 32,
            endpoint_url="http://o2/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="rej-space",
            owning_instance="o2.home",
            name="R",
            status="pending",
        )
    )
    resp = await client.post("/admin/api/spaces/rej-space/reject")
    assert resp.status == 200
    assert await fed_repo.get_space("rej-space") is None


async def test_space_unban_sets_pending(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o3.home",
            display_name="O",
            public_key="aa" * 32,
            endpoint_url="http://o3/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="ub-space",
            owning_instance="o3.home",
            name="UB",
            status="banned",
        )
    )
    resp = await client.post("/admin/api/spaces/ub-space/unban")
    assert resp.status == 200
    sp = await fed_repo.get_space("ub-space")
    assert sp.status == "pending"


async def test_duplicate_fraud_report_is_duplicate_status(client):
    """Second report from same reporter on same target returns 'duplicate'."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="dup.home",
            display_name="D",
            public_key=kp.public_key.hex(),
            endpoint_url="http://d/wh",
            status="active",
        )
    )
    body = _signed_report_body(kp, reporter="dup.home")
    resp = await client.post("/gfs/report", json=body)
    assert (await resp.json())["status"] == "recorded"
    resp = await client.post("/gfs/report", json=body)
    assert (await resp.json())["status"] == "duplicate"


async def test_fraud_report_reporter_cap_rate_limits(client, monkeypatch):
    """Exceeding the per-reporter 24h cap returns status=recorded=False."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    # Tighten the cap for this test.
    from social_home.global_server import admin_service as _as

    monkeypatch.setattr(_as, "MAX_REPORTS_PER_REPORTER_PER_DAY", 2)
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="cap.home",
            display_name="C",
            public_key=kp.public_key.hex(),
            endpoint_url="http://c/wh",
            status="active",
        )
    )
    # Two under the cap succeed.
    for tid in ("t1", "t2"):
        body = _signed_report_body(
            kp,
            reporter="cap.home",
            target_id=tid,
        )
        resp = await client.post("/gfs/report", json=body)
        assert (await resp.json())["status"] == "recorded"
    # Third one is above the cap → duplicate (silently dropped).
    body = _signed_report_body(kp, reporter="cap.home", target_id="t3")
    resp = await client.post("/gfs/report", json=body)
    assert (await resp.json())["status"] == "duplicate"


async def test_list_appeals_with_status_filter(client):
    app = client._app
    admin_svc = app[
        __import__(
            "social_home.global_server.app_keys", fromlist=["*"]
        ).gfs_admin_service_key
    ]
    await admin_svc.record_appeal(
        target_type="instance",
        target_id="x",
        message="m",
    )
    # No filter returns the entry.
    resp = await client.get("/admin/api/appeals")
    assert len(await resp.json()) >= 1
    # Decide + status=lifted → empty because lift requires existing ban.
    # Just ensure the filter works.
    resp = await client.get("/admin/api/appeals?status=lifted")
    assert (await resp.json()) == []


async def test_appeal_ingress_records_pending_row(client):
    """POST /gfs/appeal signs + records an appeal from a banned household."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="banned.home",
            display_name="B",
            public_key=kp.public_key.hex(),
            endpoint_url="http://b/wh",
            status="banned",
        )
    )
    body = {
        "target_type": "instance",
        "target_id": "banned.home",
        "message": "false positive",
        "from_instance": "banned.home",
    }
    canonical = json.dumps(
        body,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body["signature"] = b64url_encode(sign_ed25519(kp.private_key, canonical))
    resp = await client.post("/gfs/appeal", json=body)
    assert resp.status == 201
    resp = await client.get("/admin/api/appeals?status=pending")
    lst = await resp.json()
    assert any(a["message"] == "false positive" for a in lst)


async def test_appeal_ingress_unknown_sender_is_403(client):
    body = {
        "target_type": "space",
        "target_id": "sp",
        "message": "hi",
        "from_instance": "ghost.home",
        "signature": "sig",
    }
    resp = await client.post("/gfs/appeal", json=body)
    assert resp.status == 403


async def test_header_image_upload_writes_file_and_updates_config(
    client,
):
    from io import BytesIO
    from pathlib import Path

    import aiohttp
    from PIL import Image

    app = client._app
    # Build a tiny JPG in memory.
    img = Image.new("RGB", (200, 120), color="red")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    form = aiohttp.FormData()
    form.add_field(
        "file",
        buf.getvalue(),
        filename="hero.jpg",
        content_type="image/jpeg",
    )
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=form,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["header_image_file"].endswith(".webp")
    from social_home.global_server.app_keys import gfs_config_key

    cfg = app[gfs_config_key]
    assert (Path(cfg.media_dir) / body["header_image_file"]).is_file()
    assert (
        await app[gfs_admin_repo_key].get_config(
            "header_image_file",
        )
        == body["header_image_file"]
    )


async def test_header_image_upload_rejects_non_image(client):
    import aiohttp

    form = aiohttp.FormData()
    form.add_field(
        "file",
        b"not an image",
        filename="junk.bin",
        content_type="application/octet-stream",
    )
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=form,
    )
    assert resp.status == 415


async def test_header_image_upload_rejects_missing_file(client):
    import aiohttp

    form = aiohttp.FormData()
    form.add_field("something_else", b"x")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=form,
    )
    assert resp.status == 400


async def test_public_spaces_excludes_pending_and_banned(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="ee" * 32,
            endpoint_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="active-one",
            owning_instance="o.home",
            name="A",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="pending-one",
            owning_instance="o.home",
            name="P",
            status="pending",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="banned-one",
            owning_instance="o.home",
            name="B",
            status="banned",
        )
    )
    resp = await client.get("/gfs/spaces")
    spaces = (await resp.json())["spaces"]
    ids = {s["space_id"] for s in spaces}
    assert ids == {"active-one"}

"""Tests for PushService + VAPID key persistence."""

from __future__ import annotations


from social_home.repositories.push_subscription_repo import PushSubscription
from social_home.services.push_service import (
    PushPayload,
    PushService,
    VapidKeyPair,
    load_or_create_vapid,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _MemRepo:
    def __init__(self) -> None:
        self.subs: dict[str, PushSubscription] = {}

    async def save(self, sub):
        self.subs[sub.id] = sub

    async def list_for_user(self, user_id):
        return [s for s in self.subs.values() if s.user_id == user_id]

    async def get(self, sub_id):
        return self.subs.get(sub_id)

    async def delete(self, sub_id, *, user_id):
        sub = self.subs.get(sub_id)
        if sub is None or sub.user_id != user_id:
            return False
        del self.subs[sub_id]
        return True

    async def delete_by_endpoint(self, endpoint):
        ids = [sid for sid, s in self.subs.items() if s.endpoint == endpoint]
        for sid in ids:
            del self.subs[sid]
        return len(ids)


def _vapid() -> VapidKeyPair:
    return VapidKeyPair(private_pem="dummy", public_b64url="public-key")


# ─── PushPayload ──────────────────────────────────────────────────────────


def test_payload_drops_none_fields():
    p = PushPayload(title="Hi", click_url=None, space_id="sp-1")
    import json as _json

    body = _json.loads(p.to_json())
    assert body == {"title": "Hi", "space_id": "sp-1"}
    assert "click_url" not in body


def test_payload_omits_body_per_25_3():
    """§25.3 — body field must never appear, even via to_json."""
    p = PushPayload(title="Hi from Pascal")
    assert "body" not in p.to_json()


# ─── PushService ──────────────────────────────────────────────────────────


async def test_push_to_user_returns_delivered_count_in_stub_mode():
    """Without pywebpush installed the dispatcher is a no-op success."""
    repo = _MemRepo()
    svc = PushService(sub_repo=repo, vapid=_vapid())
    await repo.save(
        PushSubscription(
            id="s1",
            user_id="alice",
            endpoint="https://x/y",
            p256dh="p",
            auth_secret="a",
        )
    )
    n = await svc.push_to_user("alice", PushPayload(title="Hi"))
    assert n == 1


async def test_push_to_user_no_subscriptions():
    svc = PushService(sub_repo=_MemRepo(), vapid=_vapid())
    n = await svc.push_to_user("nobody", PushPayload(title="Hi"))
    assert n == 0


async def test_push_to_users_aggregates():
    repo = _MemRepo()
    svc = PushService(sub_repo=repo, vapid=_vapid())
    await repo.save(
        PushSubscription(
            id="s1",
            user_id="alice",
            endpoint="https://x/1",
            p256dh="p",
            auth_secret="a",
        )
    )
    await repo.save(
        PushSubscription(
            id="s2",
            user_id="bob",
            endpoint="https://x/2",
            p256dh="p",
            auth_secret="a",
        )
    )
    n = await svc.push_to_users(["alice", "bob"], PushPayload(title="Hi"))
    assert n == 2


def test_vapid_public_key_property():
    svc = PushService(sub_repo=_MemRepo(), vapid=_vapid())
    assert svc.vapid_public_key == "public-key"


async def test_notify_missed_call_fans_out_title_only():
    """Missed-call push: title-only body (§25.3), deep-links to DM thread."""
    repo = _MemRepo()
    svc = PushService(sub_repo=repo, vapid=_vapid())
    await repo.save(
        PushSubscription(
            id="s1",
            user_id="uid-bob",
            endpoint="https://x/1",
            p256dh="p",
            auth_secret="a",
        )
    )
    delivered = await svc.notify_missed_call(
        recipient_user_ids=["uid-bob"],
        caller_user_id="uid-alice",
        call_id="c1",
        conversation_id="conv-ab",
    )
    assert delivered == 1


async def test_notify_missed_call_payload_shape():
    """Payload must be title-only (no body) + clickthrough to the DM thread."""
    captured: list[tuple[str, PushPayload]] = []

    class _CapService(PushService):
        async def push_to_user(self, user_id, payload):  # type: ignore[override]
            captured.append((user_id, payload))
            return 1

    svc = _CapService(sub_repo=_MemRepo(), vapid=_vapid())
    await svc.notify_missed_call(
        recipient_user_ids=["uid-bob", "uid-charlie"],
        caller_user_id="uid-alice",
        call_id="c1",
        conversation_id="conv-ab",
    )
    assert len(captured) == 2
    _uid, p = captured[0]
    assert p.title == "Missed call"
    assert p.tag == "call-missed:c1"
    assert p.click_url and "conv-ab" in p.click_url
    import json as _json

    body = _json.loads(p.to_json())
    assert "body" not in body
    assert body["title"] == "Missed call"


# ─── load_or_create_vapid ─────────────────────────────────────────────────


def test_load_or_create_vapid_generates_persistent_keypair(tmp_dir):
    kp1 = load_or_create_vapid(tmp_dir)
    kp2 = load_or_create_vapid(tmp_dir)
    assert kp1.public_b64url == kp2.public_b64url
    assert "BEGIN" in kp1.private_pem


def test_load_or_create_vapid_writes_files_with_strict_perms(tmp_dir):
    load_or_create_vapid(tmp_dir)
    priv = (tmp_dir / ".vapid_private.pem").stat()
    # 0o600 — only owner read/write.
    assert priv.st_mode & 0o077 == 0

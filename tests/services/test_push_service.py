"""Tests for PushService + VAPID key persistence."""

from __future__ import annotations

import threading

import orjson

from socialhome.repositories.push_subscription_repo import PushSubscription
from socialhome.services import push_service as push_mod
from socialhome.services.push_service import (
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


# ─── Real send path (pywebpush) runs off the event loop ─────────────────


def _sub(sid: str, user_id: str = "alice") -> PushSubscription:
    return PushSubscription(
        id=sid,
        user_id=user_id,
        endpoint=f"https://push.example/{sid}",
        p256dh="p",
        auth_secret="a",
    )


class _FakeWebPushError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.response = type("R", (), {"status_code": status})()


async def test_webpush_runs_in_a_worker_thread_not_the_event_loop(monkeypatch):
    """pywebpush's ``webpush()`` is a blocking HTTP call — it must run via
    ``asyncio.to_thread`` so a slow push service never stalls the loop."""
    loop_thread = threading.get_ident()
    seen: list[tuple[int, dict]] = []

    def fake_webpush(**kwargs):
        seen.append((threading.get_ident(), kwargs))

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    repo = _MemRepo()
    await repo.save(_sub("s1"))
    svc = PushService(sub_repo=repo, vapid=_vapid(), contact_email="op@x")
    n = await svc.push_to_user("alice", PushPayload(title="Hi", click_url="/dms"))
    assert n == 1
    [(thread_id, kwargs)] = seen
    assert thread_id != loop_thread
    assert kwargs["subscription_info"]["endpoint"] == "https://push.example/s1"
    assert orjson.loads(kwargs["data"]) == {"title": "Hi", "click_url": "/dms"}
    assert kwargs["vapid_claims"] == {"sub": "mailto:op@x"}


async def test_push_to_user_sends_to_every_device_concurrently(monkeypatch):
    """Two devices are sent to in parallel: each fake send waits on a
    two-party barrier, which only trips when both run at the same time."""
    barrier = threading.Barrier(2, timeout=5)

    def fake_webpush(**kwargs):
        barrier.wait()

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    repo = _MemRepo()
    await repo.save(_sub("s1"))
    await repo.save(_sub("s2"))
    svc = PushService(sub_repo=repo, vapid=_vapid())
    assert await svc.push_to_user("alice", PushPayload(title="Hi")) == 2


async def test_push_to_users_sends_to_recipients_concurrently(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)

    def fake_webpush(**kwargs):
        barrier.wait()

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    repo = _MemRepo()
    await repo.save(_sub("s1", "alice"))
    await repo.save(_sub("s2", "bob"))
    svc = PushService(sub_repo=repo, vapid=_vapid())
    assert await svc.push_to_users(["alice", "bob"], PushPayload(title="Hi")) == 2


async def test_dead_subscription_is_removed(monkeypatch):
    def fake_webpush(**kwargs):
        raise _FakeWebPushError(410)

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    monkeypatch.setattr(push_mod, "WebPushException", _FakeWebPushError)
    repo = _MemRepo()
    await repo.save(_sub("s1"))
    svc = PushService(sub_repo=repo, vapid=_vapid())
    assert await svc.push_to_user("alice", PushPayload(title="Hi")) == 0
    assert repo.subs == {}


async def test_transient_push_failure_keeps_the_subscription(monkeypatch):
    def fake_webpush(**kwargs):
        raise _FakeWebPushError(500)

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    monkeypatch.setattr(push_mod, "WebPushException", _FakeWebPushError)
    repo = _MemRepo()
    await repo.save(_sub("s1"))
    svc = PushService(sub_repo=repo, vapid=_vapid())
    assert await svc.push_to_user("alice", PushPayload(title="Hi")) == 0
    assert "s1" in repo.subs


async def test_unexpected_push_error_is_swallowed(monkeypatch):
    def fake_webpush(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(push_mod, "_PYWEBPUSH_AVAILABLE", True)
    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    repo = _MemRepo()
    await repo.save(_sub("s1"))
    svc = PushService(sub_repo=repo, vapid=_vapid())
    assert await svc.push_to_user("alice", PushPayload(title="Hi")) == 0


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

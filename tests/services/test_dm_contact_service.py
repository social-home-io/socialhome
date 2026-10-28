"""Answering a request to message you (§23.47): accept opens the chat,
decline is silent; every pending request from that person is answered
exactly once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from socialhome.domain.conversation import (
    ContactRequestHandledError,
    ContactRequestNotFoundError,
    Conversation,
    ConversationType,
)
from socialhome.domain.events import DmContactRequestAnswered
from socialhome.services.dm_contact_service import DmContactService
from socialhome.services.dm_service import RecipientBlockedError


class _Requests:
    """In-memory ``dm_contact_requests``: rows (from, to, status)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, frm: str, to: str, status: str = "pending") -> None:
        self.rows.append({"from_user_id": frm, "to_user_id": to, "status": status})

    async def latest_between(self, frm: str, to: str) -> dict | None:
        mine = [
            r for r in self.rows if (r["from_user_id"], r["to_user_id"]) == (frm, to)
        ]
        pending = [r for r in mine if r["status"] == "pending"]
        return (pending or mine or [None])[-1]

    async def answer_pending(self, frm: str, to: str, status: str) -> int:
        await asyncio.sleep(0)  # let a racing answer interleave
        moved = 0
        for r in self.rows:
            if (r["from_user_id"], r["to_user_id"], r["status"]) == (
                frm,
                to,
                "pending",
            ):
                r["status"] = status
                moved += 1
        return moved


class _Users:
    def __init__(self) -> None:
        self.local = {"anna": SimpleNamespace(user_id="u-anna", username="anna")}
        self.remote = {"ru-ben": SimpleNamespace(user_id="ru-ben")}

    async def get(self, username: str):
        return self.local.get(username)

    async def get_remote(self, user_id: str):
        return self.remote.get(user_id)


class _Dm:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.blocked = False

    async def create_dm(self, *, creator_username: str, other_user_id: str):
        if self.blocked:
            raise RecipientBlockedError("blocked")
        self.opened.append((creator_username, other_user_id))
        return Conversation(id="conv-1", type=ConversationType.DM, created_at=None)  # type: ignore[arg-type]


class _Bus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event: object) -> None:
        self.published.append(event)


@pytest.fixture
def env():
    reqs, users, dm, bus = _Requests(), _Users(), _Dm(), _Bus()
    svc = DmContactService(reqs, users, dm, bus)  # type: ignore[arg-type]
    return SimpleNamespace(reqs=reqs, users=users, dm=dm, bus=bus, svc=svc)


async def test_accept_opens_the_chat_and_answers_every_request(env):
    env.reqs.add("ru-ben", "u-anna")
    env.reqs.add("ru-ben", "u-anna")
    conv = await env.svc.accept(username="anna", requester_user_id="ru-ben")
    assert conv.id == "conv-1"
    assert env.dm.opened == [("anna", "ru-ben")]
    assert {r["status"] for r in env.reqs.rows} == {"accepted"}
    assert env.bus.published == [
        DmContactRequestAnswered(
            requester_user_id="ru-ben",
            recipient_user_id="u-anna",
            accepted=True,
            occurred_at=env.bus.published[0].occurred_at,
        )
    ]


async def test_decline_is_silent_and_opens_nothing(env):
    env.reqs.add("ru-ben", "u-anna")
    await env.svc.decline(username="anna", requester_user_id="ru-ben")
    assert env.dm.opened == []
    assert env.reqs.rows[0]["status"] == "declined"
    (ev,) = env.bus.published
    assert ev.accepted is False


async def test_no_request_is_not_found(env):
    with pytest.raises(ContactRequestNotFoundError):
        await env.svc.accept(username="anna", requester_user_id="ru-ben")
    with pytest.raises(ContactRequestNotFoundError):
        await env.svc.decline(username="nobody", requester_user_id="ru-ben")


async def test_an_answered_request_says_how(env):
    env.reqs.add("ru-ben", "u-anna", "declined")
    with pytest.raises(ContactRequestHandledError) as err:
        await env.svc.accept(username="anna", requester_user_id="ru-ben")
    assert err.value.params == {"status": "declined"}
    assert env.bus.published == []


async def test_two_answers_at_once_only_one_wins(env):
    env.reqs.add("ru-ben", "u-anna")
    results = await asyncio.gather(
        env.svc.decline(username="anna", requester_user_id="ru-ben"),
        env.svc.decline(username="anna", requester_user_id="ru-ben"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ContactRequestHandledError) for r in results) == 1
    assert len(env.bus.published) == 1


async def test_accepting_someone_no_longer_known_leaves_it_waiting(env):
    env.reqs.add("ru-ben", "u-anna")
    del env.users.remote["ru-ben"]
    with pytest.raises(ContactRequestNotFoundError):
        await env.svc.accept(username="anna", requester_user_id="ru-ben")
    assert env.reqs.rows[0]["status"] == "pending"
    # Declining still works.
    await env.svc.decline(username="anna", requester_user_id="ru-ben")


async def test_a_blocked_pair_cant_accept_and_the_request_still_waits(env):
    env.reqs.add("ru-ben", "u-anna")
    env.dm.blocked = True
    with pytest.raises(RecipientBlockedError):
        await env.svc.accept(username="anna", requester_user_id="ru-ben")
    assert env.reqs.rows[0]["status"] == "pending"
    assert env.bus.published == []

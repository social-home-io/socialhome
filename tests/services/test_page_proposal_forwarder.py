"""Tests for :class:`PageProposalForwarder` — stop-and-wait delivery of a
member household's page drafts to the space's host (v_48)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from socialhome.domain.events import (
    ConnectionReachable,
    PageCreated,
    PageProposalSettled,
    PageUpdated,
)
from socialhome.domain.federation import DeliveryResult, FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.page_conflict_service import PageMode, Proposal
from socialhome.services.page_proposal_forwarder import PageProposalForwarder

SID = "sp-1"
HOST = "host-iid"


class _Conflicts:
    def __init__(self) -> None:
        self.mode_ = PageMode.MEMBER
        self.drafts: dict[str, Proposal] = {}
        self.rebased: list[tuple[str, int]] = []

    async def mode(self, space_id):
        return self.mode_, HOST

    async def proposal_for(self, space_id, page_id):
        return self.drafts.get(page_id)

    async def rebase_draft(self, space_id, page_id, *, sent, seq):
        self.rebased.append((sent.content, seq))


class _Pages:
    def __init__(self, conflicts: _Conflicts) -> None:
        self._c = conflicts

    async def list_pending_drafts(self, *, space_id=None):
        return [(SID, pid) for pid in self._c.drafts]


class _Fed:
    def __init__(self) -> None:
        self.sent: list[tuple[str, FederationEventType, dict]] = []
        self.result = DeliveryResult(instance_id=HOST, ok=True)

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload))
        return self.result


class _FedRepo:
    def __init__(self) -> None:
        self.reachable = True

    async def get_instance(self, instance_id):
        return SimpleNamespace(is_reachable=lambda: self.reachable)


@pytest.fixture
def env():
    bus = EventBus()
    conflicts = _Conflicts()
    fed = _Fed()
    repo = _FedRepo()
    fwd = PageProposalForwarder(
        page_repo=_Pages(conflicts),
        conflicts=conflicts,
        federation_service=fed,
        federation_repo=repo,
        bus=bus,
        interval_seconds=3600,
    )
    fwd.wire()
    return SimpleNamespace(bus=bus, conflicts=conflicts, fed=fed, repo=repo, fwd=fwd)


def _draft(content: str, *, base_seq: int = 3, **kw) -> Proposal:
    return Proposal(
        title="T",
        content=content,
        actor_user_id="u1",
        base_seq=base_seq,
        base_hash="sha256:" + "b" * 64 if base_seq else None,
        **kw,
    )


async def test_a_draft_is_proposed_to_the_host_once(env):
    env.conflicts.drafts["pg"] = _draft("one", resolves=("sha256:" + "c" * 64,))
    await env.bus.publish(
        PageUpdated(page_id="pg", space_id=SID, title="T", content="one", proposal=True)
    )
    (to, et, payload) = env.fed.sent[0]
    assert (to, et) == (HOST, FederationEventType.SPACE_PAGE_UPDATED)
    assert payload["base_seq"] == 3 and payload["base_hash"].startswith("sha256:")
    assert payload["resolves"] == ["sha256:" + "c" * 64]
    # Stop and wait: a newer draft waits for the host's answer.
    env.conflicts.drafts["pg"] = _draft("two")
    await env.bus.publish(
        PageUpdated(page_id="pg", space_id=SID, title="T", content="two", proposal=True)
    )
    assert len(env.fed.sent) == 1
    assert env.fwd.outstanding() == {(SID, "pg"): _draft("one").hash}


async def test_an_answer_releases_the_next_draft_rebased(env):
    env.conflicts.drafts["pg"] = _draft("one")
    await env.fwd.kick(SID, "pg")
    env.conflicts.drafts["pg"] = _draft("two")
    await env.bus.publish(
        PageProposalSettled(
            page_id="pg",
            space_id=SID,
            proposal_hash=_draft("one").hash,
            outcome="applied",
            seq=4,
        )
    )
    assert env.conflicts.rebased == [("one", 4)]
    assert [p["content"] for _t, _e, p in env.fed.sent] == ["one", "two"]


async def test_an_answer_to_another_proposal_releases_nothing(env):
    env.conflicts.drafts["pg"] = _draft("one")
    await env.fwd.kick(SID, "pg")
    await env.bus.publish(
        PageProposalSettled(
            page_id="pg",
            space_id=SID,
            proposal_hash="sha256:" + "f" * 64,
            outcome="applied",
        )
    )
    assert len(env.fed.sent) == 1 and env.conflicts.rebased == []


async def test_rate_limited_waits_for_the_tick(env):
    env.conflicts.drafts["pg"] = _draft("one")
    await env.fwd.kick(SID, "pg")
    await env.bus.publish(
        PageProposalSettled(
            page_id="pg",
            space_id=SID,
            proposal_hash=_draft("one").hash,
            outcome="refused",
            reason="rate_limited",
        )
    )
    assert len(env.fed.sent) == 1
    assert env.fwd.outstanding() == {}
    assert await env.fwd.flush() == 1


async def test_a_create_goes_out_as_created(env):
    env.conflicts.drafts["pg"] = _draft("new", base_seq=0, created_by="u1")
    await env.bus.publish(
        PageCreated(page_id="pg", space_id=SID, title="T", content="new", proposal=True)
    )
    (_to, et, payload) = env.fed.sent[0]
    assert et is FederationEventType.SPACE_PAGE_CREATED
    assert payload["created_by"] == "u1" and "base_hash" not in payload


async def test_nothing_goes_out_while_the_host_is_unreachable(env):
    env.repo.reachable = False
    env.conflicts.drafts["pg"] = _draft("one")
    assert not await env.fwd.kick(SID, "pg")
    env.repo.reachable = True
    await env.bus.publish(ConnectionReachable(instance_id=HOST))
    assert len(env.fed.sent) == 1
    # Another host coming back flushes nothing of ours.
    await env.bus.publish(ConnectionReachable(instance_id="someone-else"))
    assert len(env.fed.sent) == 1


async def test_an_unrouted_send_is_retried_a_queued_one_is_not(env):
    env.conflicts.drafts["pg"] = _draft("one")
    env.fed.result = DeliveryResult(instance_id=HOST, ok=False, error="no_route")
    assert not await env.fwd.kick(SID, "pg")
    assert env.fwd.outstanding() == {}
    env.fed.result = DeliveryResult(instance_id=HOST, ok=False, error="delivery_failed")
    assert await env.fwd.kick(SID, "pg")
    # In the outbox already: it stays outstanding, no second row.
    assert not await env.fwd.kick(SID, "pg")
    assert len(env.fed.sent) == 2


async def test_no_proposals_unless_a_member_of_a_v48_host(env):
    env.conflicts.drafts["pg"] = _draft("one")
    for mode in (PageMode.HOST, PageMode.LEGACY):
        env.conflicts.mode_ = mode
        assert not await env.fwd.kick(SID, "pg")
    env.conflicts.mode_ = PageMode.MEMBER
    env.conflicts.drafts.clear()
    assert not await env.fwd.kick(SID, "pg")
    assert env.fed.sent == []


async def test_household_and_non_proposal_events_are_ignored(env):
    env.conflicts.drafts["pg"] = _draft("one")
    await env.bus.publish(
        PageUpdated(page_id="pg", space_id=SID, title="T", content="x")
    )
    await env.bus.publish(
        PageUpdated(page_id="pg", space_id=None, title="T", content="x", proposal=True)
    )
    assert env.fed.sent == []


async def test_start_flushes_and_stop_ends_the_loop(env):
    env.conflicts.drafts["pg"] = _draft("one")
    await env.fwd.start()
    await env.fwd.start()  # idempotent
    for _ in range(20):
        if env.fed.sent:
            break
        await asyncio.sleep(0.01)
    assert len(env.fed.sent) == 1
    await env.fwd.stop()
    assert env.fwd._task is None

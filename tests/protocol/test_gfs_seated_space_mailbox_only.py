"""§27.9 release blocker: no GFS envelope of a seated space is
instance-addressed.

Every space seat — a member's or a follower's (owner 2026-11-24: a
follower is a member with the read-only role on every path) — is reached
through the households' GFS mailboxes once both hold a mailbox: the GFS
then sees mailbox ids and ciphertext, never either household's instance id
(``FederationTransport.send_via_gfs_relay`` falls back to the
instance-addressed relay only when no card names a mailbox). The gap this
pins: a household already seated with the host that then FOLLOWED another
of its spaces held no card of the host there (the member directory was
writer-only), so all of that space's traffic went instance-addressed.

Real households (the e2e harness of ``test_invite_bootstrap_gfs_e2e``):
real federation, transport, mailbox service and member directory; the GFS
is the harness's recording fake.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import JoinMode, SpaceType
from socialhome.federation import gfs_relay_transport as grt
from tests.federation.test_invite_bootstrap_gfs_e2e import (
    POST_MARKER,
    _FakeGfs,
    _household,
    _join,
    _link_join,
    _mailbox_of,
    _post_payload,
)

pytestmark = pytest.mark.security

#: The only envelopes of the follow itself that may go instance-addressed,
#: each for a reason that holds for a MEMBER's join over an existing seat
#: alike (no follower special case): the §D2 redeem and its ACK carry no
#: routing ``space_id`` (a SPACE_* event without one never rides a
#: mailbox — the receiver refuses it there), and the joiner's first
#: ``SPACE_MEMBER_CARD`` ("seated") goes before the host's directory for
#: that space has reached it (a card of space X is never used for Y).
SETUP_ALLOWED: frozenset[str] = frozenset(
    {
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM.value,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK.value,
        FederationEventType.SPACE_MEMBER_CARD.value,
    }
)


@pytest.fixture
def instance_addressed(monkeypatch) -> list[str]:
    """Every envelope the instance-addressed relay carries (event types)."""
    seen: list[str] = []
    send = grt.GfsRelayTransport.send

    async def _spy(self, *, instance, envelope_dict, gfs_url=None):
        seen.append(str(envelope_dict.get("event_type")))
        return await send(
            self, instance=instance, envelope_dict=envelope_dict, gfs_url=gfs_url
        )

    monkeypatch.setattr(grt.GfsRelayTransport, "send", _spy)
    return seen


@pytest.fixture
async def seated(tmp_path, instance_addressed):
    gfs = _FakeGfs()
    server = TestServer(gfs.app())
    await server.start_server()
    gfs.url = str(server.make_url("")).rstrip("/")
    gfs.mailboxes_on = True
    async with aiohttp.ClientSession() as session:
        a = await _household(tmp_path, "pa", gfs, session, mailboxes=True)
        b = await _household(tmp_path, "pb", gfs, session, mailboxes=True)
        for h in (a, b):  # the direct §D2 redeem over the existing seat
            h.coordinator.attach_to(h.federation)
        member_space = await _join(a, b, gfs)
        instance_addressed.clear()  # the first link's bootstrap: by design
        followed = await b.space_service.create_space(
            owner_username=b.username,
            name="Followed",
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
        await b.db.enqueue("UPDATE spaces SET private_gfs=1 WHERE id=?", (followed.id,))
        await _link_join(a, b, gfs, followed, "tok-follow", role="subscriber")
        for _ in range(3):  # the directory's sends go out from the sweep
            await b.directory.sweep_once()
            await a.directory.sweep_once()
            await gfs.drain()
        yield gfs, a, b, (member_space, followed)
        await gfs.drain()
        for h in (a, b):
            await h.mailboxes.stop()
            await h.db.shutdown()
    await server.close()


async def test_the_follow_over_an_existing_seat_goes_instance_addressed_only_to_seat(
    seated, instance_addressed
):
    """The follow itself: the redeem, its ACK and the first card only."""
    assert instance_addressed, "the follow went over the relay"
    assert set(instance_addressed) <= SETUP_ALLOWED


async def test_no_envelope_of_a_seated_space_is_instance_addressed(
    seated, instance_addressed
):
    gfs, a, b, spaces = seated
    mailboxes = {_mailbox_of(a, gfs), _mailbox_of(b, gfs)}
    instance_addressed.clear()
    gfs.mailbox.clear()
    a.received.clear()
    for space in spaces:
        await b.federation.broadcast_to_space_members(
            space.id, FederationEventType.SPACE_POST_CREATED, _post_payload()
        )
        await a.federation.broadcast_to_space_members(
            space.id, FederationEventType.SPACE_COMMENT_CREATED, {"content": "c"}
        )
    await gfs.drain()
    # Both spaces' posts arrived — over the mailbox, the only way here.
    assert [e.payload["content"] for e in a.received] == [POST_MARKER] * 2
    addressed = [to for to, _ in gfs.mailbox]
    assert addressed, "the traffic crossed the GFS"
    seen_after = list(instance_addressed)
    by_id = [to for to in addressed if to in (a.instance_id, b.instance_id)]
    assert by_id == []
    assert set(addressed) <= mailboxes
    # Nothing at all went through the instance-addressed relay.
    assert seen_after == []

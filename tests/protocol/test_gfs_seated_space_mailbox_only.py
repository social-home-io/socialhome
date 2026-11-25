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


@pytest.fixture
async def seated(tmp_path):
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


async def test_no_envelope_of_a_seated_space_is_instance_addressed(seated):
    gfs, a, b, spaces = seated
    mailboxes = {_mailbox_of(a, gfs), _mailbox_of(b, gfs)}
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
    instance_addressed = [
        to for to in addressed if to in (a.instance_id, b.instance_id)
    ]
    assert instance_addressed == []
    assert set(addressed) <= mailboxes

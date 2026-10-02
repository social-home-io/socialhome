"""Direct SQLite tests for ``SqliteSpaceMediaOutboxRepo``.

The scheduler paths are exercised in ``test_space_media_sync_service``;
this file pins the repo-only surfaces.
"""

from __future__ import annotations

import logging

import pytest

from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)


pytestmark = pytest.mark.asyncio


async def _seed_peer(db, instance_id: str, *, status: str = "confirmed") -> None:
    """A minimal ``remote_instances`` row the outbox can queue against."""
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status) VALUES(?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}.invalid/inbox",
            f"inbox-{instance_id}",
            status,
        ),
    )


@pytest.fixture
async def repo(db):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        ("sp-1", "S", "peer", "owner", "aa" * 32, "household", "invite_only"),
    )
    # The outbox queues only for a paired household or a member household
    # of the space (a mesh-only member has no ``remote_instances`` row).
    for peer in ("inst-gone", "inst-kept"):
        await _seed_peer(db, peer)
    repo = SqliteSpaceMediaOutboxRepo(db)
    repo.db = db  # type: ignore[attr-defined]
    return repo


async def test_delete_for_instance_drops_every_row_for_that_household(repo):
    """An unpair tombstone gets nothing but our UNPAIR: every media row
    addressed to it goes, whatever its status; other households keep
    theirs."""
    for blob in ("b-1", "b-2"):
        for target in ("inst-gone", "inst-kept"):
            await repo.enqueue(
                blob_id=blob,
                space_id="sp-1",
                correlation_id="p-1",
                target_instance_id=target,
                bytes_path=f"/m/{blob}",
            )
    await repo.mark_failed(
        blob_id="b-2", target_instance_id="inst-gone", last_error="x"
    )

    await repo.delete_for_instance("inst-gone")

    rows = await repo.list_for_correlation("p-1")
    assert {(r.blob_id, r.target_instance_id) for r in rows} == {
        ("b-1", "inst-kept"),
        ("b-2", "inst-kept"),
    }


async def test_enqueue_checks_the_peer_atomically(repo, caplog):
    """Mirror of the federation outbox's guard: a household with neither a
    pairing nor a seat in the space is refused, and so is an unpair
    tombstone; a mesh-only member household (``space_instances`` row, no
    pairing) is accepted."""
    caplog.set_level(logging.INFO)
    db = repo.db
    await _seed_peer(db, "inst-tomb", status="unpairing")
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        ("sp-1", "inst-mesh"),
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        ("sp-1", "inst-tomb"),
    )
    for target in ("inst-stranger", "inst-tomb", "inst-mesh", "inst-kept"):
        await repo.enqueue(
            blob_id="b-1",
            space_id="sp-1",
            correlation_id="p-9",
            target_instance_id=target,
            bytes_path="/m/b-1",
        )
    rows = await repo.list_for_correlation("p-9")
    assert {r.target_instance_id for r in rows} == {"inst-mesh", "inst-kept"}
    refused = [
        r.getMessage() for r in caplog.records if "not queueing" in r.getMessage()
    ]
    assert len(refused) == 2


async def test_purge_orphaned_keeps_paired_and_member_households(repo):
    """An orphan is a row whose household is neither paired with us nor a
    member household of that space; it is deleted whatever its status."""
    db = repo.db
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        ("sp-1", "inst-mesh"),
    )
    for target in ("inst-gone", "inst-kept", "inst-mesh"):
        await repo.enqueue(
            blob_id="b-1",
            space_id="sp-1",
            correlation_id="p-7",
            target_instance_id=target,
            bytes_path="/m/b-1",
        )
    await db.enqueue("DELETE FROM remote_instances WHERE id='inst-gone'")
    assert await repo.purge_orphaned() == 1
    rows = await repo.list_for_correlation("p-7")
    assert {r.target_instance_id for r in rows} == {"inst-kept", "inst-mesh"}
    # A mesh member that leaves the space is an orphan too.
    await db.enqueue("DELETE FROM space_instances WHERE instance_id='inst-mesh'")
    assert await repo.purge_orphaned() == 1

"""Direct SQLite tests for ``SqliteSpaceMediaOutboxRepo``.

The scheduler paths are exercised in ``test_space_media_sync_service``;
this file pins the repo-only surfaces.
"""

from __future__ import annotations

import pytest

from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def repo(db):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        ("sp-1", "S", "peer", "owner", "aa" * 32, "household", "invite_only"),
    )
    return SqliteSpaceMediaOutboxRepo(db)


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

"""Tests for :class:`SqliteSpaceKeyRepo`."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space_key import SpaceKey
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "keys.db", batch_timeout_ms=10)
    await db.startup()
    for sid in ("sp-1", "sp-2"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'T', 'inst', 'pascal', ?)",
            (sid, "ab" * 32),
        )
    yield SqliteSpaceKeyRepo(db)
    await db.shutdown()


def _key(space_id: str, epoch: int, rotated_by: str | None = None) -> SpaceKey:
    return SpaceKey(
        space_id=space_id,
        epoch=epoch,
        content_key_hex=f"k{epoch}",
        rotated_by=rotated_by,
    )


async def test_save_get_latest_and_next_epoch(repo):
    assert await repo.next_epoch("sp-1") == 0
    await repo.save(_key("sp-1", 0))
    await repo.save(_key("sp-1", 1, "a"))
    assert (await repo.get_latest("sp-1")).epoch == 1
    assert (await repo.get("sp-1", 1)).rotated_by == "a"
    assert await repo.next_epoch("sp-1") == 2
    assert [k.epoch for k in await repo.list_for_space("sp-1")] == [0, 1]


async def test_reset_to_drops_only_older_key_epochs_above_it(repo):
    """v_44 baseline reset: epochs above the bundle's written under an OLDER
    authority key go; one written under the new key (a newer owner rekey
    that arrived first) stays; other spaces are untouched."""
    for epoch in (0, 1, 2, 1_000_000):
        await repo.save(_key("sp-1", epoch))  # authority_epoch 0 (spaces row)
    await repo.save(_key("sp-2", 9))
    # A later epoch written after this household adopted key epoch 3.
    await repo._db.enqueue("UPDATE spaces SET authority_key_epoch=3 WHERE id='sp-1'")
    await repo.save(_key("sp-1", 5, "owner"))
    removed = await repo.reset_to(_key("sp-1", 1, "owner"), authority_epoch=3)
    assert removed == 2  # epochs 2 and 1_000_000
    assert [k.epoch for k in await repo.list_for_space("sp-1")] == [0, 1, 5]
    assert (await repo.get("sp-1", 1)).rotated_by == "owner"
    assert [k.epoch for k in await repo.list_for_space("sp-2")] == [9]


async def test_reset_to_stamps_the_pin_epoch_not_the_delete_cutoff(repo):
    """A missed-baseline catch-up deletes only epochs older than the cutoff,
    but the installed key is stamped with the pin epoch it was installed
    under — a later baseline must see it as new-key state."""
    await repo.save(_key("sp-1", 0))
    await repo._db.enqueue("UPDATE spaces SET authority_key_epoch=7 WHERE id='sp-1'")
    await repo.save(_key("sp-1", 4))  # written under pin 7
    await repo.reset_to(_key("sp-1", 2, "owner"), authority_epoch=9, older_than=5)
    rows = await repo._db.fetchall(
        "SELECT epoch, authority_epoch FROM space_keys WHERE space_id='sp-1'"
        " ORDER BY epoch"
    )
    assert [(r["epoch"], r["authority_epoch"]) for r in rows] == [
        (0, 0),
        (2, 9),
        (4, 7),
    ]


# ─── Writer certs (migration 0074) ───────────────────────────────────────


async def test_writer_cert_stored_per_epoch(repo):
    """The household's held writer cert rides on its epoch's key row."""
    await repo.save(_key("sp-1", 0))
    await repo.save(_key("sp-1", 1))
    assert await repo.get_writer_cert("sp-1", 1) is None
    assert await repo.set_writer_cert("sp-1", 1, '{"epoch":1}') is True
    assert await repo.get_writer_cert("sp-1", 1) == '{"epoch":1}'
    assert await repo.get_writer_cert("sp-1", 0) is None
    # Replacing the cert (authority rotation re-issue) overwrites it.
    assert await repo.set_writer_cert("sp-1", 1, '{"epoch":1,"v":2}') is True
    assert await repo.get_writer_cert("sp-1", 1) == '{"epoch":1,"v":2}'


async def test_writer_cert_needs_the_epoch_key(repo):
    """No key row for the epoch → nothing stored (a cert for an epoch we
    can't read is useless and never creates a row)."""
    assert await repo.set_writer_cert("sp-1", 7, "{}") is False
    assert await repo.get_writer_cert("sp-1", 7) is None


async def test_writer_cert_survives_a_rekey_tiebreak(repo):
    """A same-epoch key replacement (Phase-4b tiebreak) keeps the cert —
    the cert is bound to the epoch, not to the key bytes."""
    await repo.save(_key("sp-1", 2, "b"))
    await repo.set_writer_cert("sp-1", 2, "cert")
    await repo.save(_key("sp-1", 2, "a"))
    assert (await repo.get("sp-1", 2)).rotated_by == "a"
    assert await repo.get_writer_cert("sp-1", 2) == "cert"


async def test_a_baseline_reset_clears_the_cert_at_its_epoch(repo):
    """The v_44 reset installs the owner's key at an epoch: a cert stored
    there was issued under the retired authority key, so it goes."""
    await repo.save(_key("sp-1", 1))
    await repo.set_writer_cert("sp-1", 1, "old-key-cert")
    await repo.reset_to(_key("sp-1", 1, "owner"), authority_epoch=1)
    assert await repo.get_writer_cert("sp-1", 1) is None


async def test_get_previous_skips_gaps(repo):
    for epoch in (0, 3, 9):
        await repo.save(_key("sp-1", epoch))
    assert (await repo.get_previous("sp-1", 9)).epoch == 3
    assert (await repo.get_previous("sp-1", 3)).epoch == 0
    assert await repo.get_previous("sp-1", 0) is None

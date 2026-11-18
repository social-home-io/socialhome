"""A rotated space key reaches a GFS the space was WITHDRAWN from.

End to end over a real GFS app and the real household publisher: the owner
lists a global space (the GFS pins K1), withdraws the listing (the GFS keeps
its row, the pin and its followers), then rotates the authority key because
an admin lost the signing seed. The household's pin-only publish moves the
pin to K2 — the listing stays withdrawn, none of the space's metadata is
sent — and the GFS refuses a K1-signed relay from then on.

Marked ``@pytest.mark.security``.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
)
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import gfs_fed_repo_key, gfs_federation_key
from socialhome.global_server.domain import ClientInstance
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security

_SPACE = "sp-withdrawn"
_GFS = "gfs-e2e"


def _relay(seed: bytes, *, post_id: str) -> dict:
    payload = {
        "space_id": _SPACE,
        "epoch": 0,
        "post_id": post_id,
        "encrypted_payload": "Y2lwaGVy",
    }
    payload.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=_SPACE,
            payload=payload,
            space_seed=seed,
        )
    )
    return payload


@pytest.fixture
async def world(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as gfs:
        owner = generate_identity_keypair()
        owner_id = derive_instance_id(owner.public_key)
        await gfs.server.app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=owner_id,
                display_name="Owner",
                public_key=owner.public_key.hex(),
                status="active",
                auto_accept=True,
            )
        )
        db = AsyncDatabase(tmp_path / "hfs.db", batch_timeout_ms=10)
        await db.startup()
        conn_repo = SqliteGfsConnectionRepo(db)
        await conn_repo.save(
            GfsConnection(
                id=_GFS,
                gfs_instance_id="gfs-inst",
                display_name="E2E GFS",
                public_key="ab" * 32,
                inbox_url=str(gfs.make_url("")).rstrip("/"),
                status="active",
                paired_at="2026-01-01T00:00:00+00:00",
            )
        )
        space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x05" * 32))
        k1 = generate_identity_keypair()
        await space_repo.save(
            Space(
                id=_SPACE,
                name="Listed once",
                owner_instance_id=owner_id,
                owner_username="alice",
                identity_public_key=k1.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.GLOBAL,
                join_mode=JoinMode.OPEN,
            )
        )
        await space_repo.set_space_seed(_SPACE, k1.private_key)
        async with aiohttp.ClientSession() as session:
            svc = GfsConnectionService(
                conn_repo, http_client=session, publish_client=session
            )
            svc.attach_publish_context(
                space_repo=space_repo,
                own_instance_id=owner_id,
                own_signing_key=owner.private_key,
            )
            # The capability probe is not what is under test here (the GFS
            # test-suite covers the signed block): take both as proven.
            svc._authority_rotation[_GFS] = True
            svc._withdrawn_repin[_GFS] = True
            yield svc, gfs, space_repo, k1
        await db.shutdown()


async def test_a_rotation_moves_the_pin_of_a_withdrawn_listing(world):
    svc, gfs, space_repo, k1 = world
    fed = gfs.server.app[gfs_federation_key]
    fed_repo = gfs.server.app[gfs_fed_repo_key]

    await svc.publish_space(_SPACE, _GFS)
    assert svc.pin_confirmed(_SPACE, _GFS)
    await svc.unpublish_space(_SPACE, _GFS)
    assert (await fed_repo.get_space(_SPACE)).withdrawn is True
    # The withdrawn listing still relays under K1 — that is the gap.
    await fed.publish_event(
        _SPACE, AUTHORITY_EVENT_SPACE_POST_PUBLIC, _relay(k1.private_key, post_id="p1")
    )

    # The space goes private and is renamed, then the key rotates.
    space = await space_repo.get(_SPACE)
    await space_repo.save(
        space.__class__(
            **{
                **{f: getattr(space, f) for f in space.__dataclass_fields__},
                "name": "Private now",
                "space_type": SpaceType.PRIVATE,
            }
        )
    )
    k2 = generate_identity_keypair()
    assert await space_repo.rotate_authority_key(
        _SPACE, public_key_hex=k2.public_key.hex(), seed=k2.private_key, key_epoch=7
    )
    assert await svc.authority_key_revocable(_SPACE) is True  # re-pins first

    row = await fed_repo.get_space(_SPACE)
    assert row.identity_public_key == k2.public_key.hex()
    assert row.withdrawn is True
    assert row.name == "Listed once"  # nothing of the private space leaked
    with pytest.raises(PermissionError):
        await fed.publish_event(
            _SPACE,
            AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            _relay(k1.private_key, post_id="p2"),
        )
    await fed.publish_event(
        _SPACE, AUTHORITY_EVENT_SPACE_POST_PUBLIC, _relay(k2.private_key, post_id="p3")
    )
    assert await svc.repin_owed() == 0


async def test_a_gfs_that_lost_the_row_owes_nothing(world):
    svc, gfs, space_repo, _k1 = world
    await svc.publish_space(_SPACE, _GFS)
    await svc.unpublish_space(_SPACE, _GFS)
    k2 = generate_identity_keypair()
    await space_repo.rotate_authority_key(
        _SPACE, public_key_hex=k2.public_key.hex(), seed=k2.private_key, key_epoch=7
    )
    db = gfs.server.app[gfs_fed_repo_key]._db
    await db.enqueue("DELETE FROM global_spaces WHERE space_id=?", (_SPACE,))
    assert await svc.repin_owed(_SPACE) == 0
    assert await svc._repo.list_withdrawn_listings() == []

"""``/api/spaces/{id}/unsent`` (v_97) — the caller's own GFS-space items
that could not be sent: list, Retry, Discard. Only the caller's own."""

import pytest

from socialhome.app_keys import space_item_outbox_key
from socialhome.domain.space_item_outbox import QueuedSpaceItem
from socialhome.repositories.space_item_outbox_repo import SqliteSpaceItemOutboxRepo

from .test_space_read_gate import _add_user, _seat

T0 = "2026-11-27T10:00:00.000000+00:00"


@pytest.fixture
async def people(client):
    """Anna hosts a space here; Ben has a seat in it."""
    hdr = {
        "anna": await _add_user(client, "anna", "uid-anna"),
        "ben": await _add_user(client, "ben", "uid-ben"),
        "guest": await _add_user(client, "guest", "uid-guest"),
    }
    r = await client.post(
        "/api/spaces",
        json={"name": "Garden", "space_type": "private"},
        headers=hdr["anna"],
    )
    assert r.status == 201, await r.text()
    hdr["sid"] = (await r.json())["id"]
    await _seat(client, hdr["sid"], "uid-ben", "member")
    return hdr


async def _failed(client, sid: str, *, author: str, payload: str | None) -> None:
    repo = SqliteSpaceItemOutboxRepo(client._db)
    if payload is None:
        await repo.record_failure(
            space_id=sid,
            event_type="space_task_created",
            author_user_id=author,
            error="too_large",
            now=T0,
        )
        return
    await repo.add(
        [
            QueuedSpaceItem(
                item_id=f"i-{author}",
                gfs_id="g1",
                space_id=sid,
                epoch=1,
                ring_class="durable",
                event_type="space_chat_message_created",
                author_user_id=author,
                payload=payload,
                next_attempt_at=T0,
                created_at=T0,
            )
        ]
    )
    await repo.mark_failed(
        QueuedSpaceItem(
            item_id=f"i-{author}",
            gfs_id="g1",
            space_id=sid,
            epoch=1,
            ring_class="durable",
            event_type="space_chat_message_created",
            author_user_id=author,
            payload=payload,
            next_attempt_at=T0,
            created_at=T0,
        ),
        "refused",
    )


async def test_the_author_lists_retries_and_discards_only_their_own(client, people):
    assert client.server.app.get(space_item_outbox_key) is not None
    await _failed(client, people["sid"], author="uid-anna", payload="ct")
    await _failed(client, people["sid"], author="uid-anna", payload=None)
    await _failed(client, people["sid"], author="uid-ben", payload="ct")

    r = await client.get(f"/api/spaces/{people['sid']}/unsent", headers=people["anna"])
    assert r.status == 200
    items = (await r.json())["items"]
    assert sorted((i["error"], i["retryable"]) for i in items) == [
        ("refused", True),
        ("too_large", False),
    ]

    r = await client.post(f"/api/spaces/{people['sid']}/unsent", headers=people["anna"])
    assert (await r.json()) == {"retried": 1}
    r = await client.get(f"/api/spaces/{people['sid']}/unsent", headers=people["anna"])
    assert [i["error"] for i in (await r.json())["items"]] == ["too_large"]

    r = await client.delete(
        f"/api/spaces/{people['sid']}/unsent", headers=people["anna"]
    )
    assert (await r.json()) == {"discarded": 1}
    r = await client.get(f"/api/spaces/{people['sid']}/unsent", headers=people["anna"])
    assert (await r.json())["items"] == []

    # Ben's item was never touched.
    r = await client.get(f"/api/spaces/{people['sid']}/unsent", headers=people["ben"])
    assert [i["error"] for i in (await r.json())["items"]] == ["refused"]


async def test_unsent_needs_a_login_and_a_seat(client, people):
    url = f"/api/spaces/{people['sid']}/unsent"
    assert (await client.get(url)).status == 401
    assert (await client.get(url, headers=people["guest"])).status == 403


async def test_an_item_naming_nobody_is_shown_to_the_household_admins(client, people):
    """10k review, finding 9: no silent loss — an automation's item (no
    author) reaches the household's admins, never an ordinary member."""
    await client._db.enqueue("UPDATE users SET is_admin=1 WHERE user_id='uid-anna'")
    await _failed(client, people["sid"], author="", payload="ct")
    url = f"/api/spaces/{people['sid']}/unsent"
    r = await client.get(url, headers=people["ben"])
    assert (await r.json())["items"] == []
    r = await client.get(url, headers=people["anna"])
    assert [i["error"] for i in (await r.json())["items"]] == ["refused"]
    r = await client.post(url, headers=people["ben"])
    assert (await r.json()) == {"retried": 0}
    r = await client.delete(url, headers=people["anna"])
    assert (await r.json()) == {"discarded": 1}

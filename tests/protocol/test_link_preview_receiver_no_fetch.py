"""§27.9 release blocker: a household that RECEIVES a post with a link
preview never fetches the linked URL.

Link previews are built once on the author's household and travel inside
the post. Every receive path — the member ``SPACE_POST_CREATED`` payload,
the space-sync record, and the GFS public relay inner — must turn the
wire card into a stored preview without any outbound request (no reader IP
reaches the site, no fetch storm), and must refuse a card that points its
image anywhere but local media.

The guard is structural: the outbound fetcher and the preview builder are
patched to fail loudly, so a future "just re-fetch it on receive" change
breaks this file.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from socialhome.federation.sync.space.receiver import _post_from_record
from socialhome.outbound_fetch import OutboundFetcher
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.services.link_preview_service import LinkPreviewService
from socialhome.services.space_public_inbound import SpacePublicInbound

pytestmark = pytest.mark.security

CARD = {
    "url": "https://linked-site.example/story",
    "title": "Title",
    "description": "Desc",
    "site_name": "Site",
    "thumbnail_url": "api/media/card.webp",
}


@pytest.fixture
def no_outbound_fetch():
    async def boom(*_a, **_kw):
        raise AssertionError("a receiving household must never fetch the URL")

    with (
        patch.object(OutboundFetcher, "fetch", boom),
        patch.object(LinkPreviewService, "preview_for_url", boom),
        patch.object(LinkPreviewService, "preview_for_post", boom),
    ):
        yield


async def test_member_payload_card_is_stored_without_fetching(
    db, bus, no_outbound_fetch
):
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    post = svc._post_from_payload(
        {
            "id": "p1",
            "author": "u",
            "type": "text",
            "content": "x",
            "link_preview": CARD,
        }
    )
    assert post is not None and post.link_preview is not None
    assert post.link_preview.title == "Title"
    assert post.link_preview.thumbnail_url == "api/media/card.webp"


def test_sync_record_card_is_stored_without_fetching(no_outbound_fetch):
    post = _post_from_record(
        {"id": "p1", "author": "u", "type": "text", "link_preview": CARD}
    )
    assert post is not None and post.link_preview is not None


def test_relayed_card_is_parsed_without_fetching(no_outbound_fetch):
    # An unsigned card is dropped, never "verified" by looking at the site.
    post = SpacePublicInbound._post_from_inner(
        "p1", "u", {"type": "text", "content": "x", "link_preview": CARD}
    )
    assert post.link_preview is None


@pytest.mark.parametrize(
    "image",
    [
        "https://linked-site.example/og.png",
        "//linked-site.example/og.png",
        "http://192.168.1.1/x.png",
        "api/media/../../etc/passwd",
        "file:///etc/passwd",
    ],
)
async def test_remote_card_image_never_survives(db, bus, no_outbound_fetch, image):
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    post = svc._post_from_payload(
        {
            "id": "p1",
            "author": "u",
            "type": "text",
            "link_preview": {**CARD, "thumbnail_url": image},
        }
    )
    assert post is not None and post.link_preview is not None
    assert post.link_preview.thumbnail_url is None

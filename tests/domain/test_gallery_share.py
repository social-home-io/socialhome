"""The share-a-copy values (gallery D8): limits per target and the shapes
the routes answer with."""

from __future__ import annotations

from socialhome.domain.gallery_share import (
    SHARE_CHAT_MAX_ITEMS,
    SHARE_MAX_ITEMS,
    SHARE_POST_MAX_ITEMS,
    SHARE_VIDEO_MAX_SECONDS,
    LongVideo,
    ShareChatTarget,
    ShareResult,
    ShareSkip,
    ShareSkipped,
    ShareSpaceTarget,
    ShareTarget,
    ShareTargets,
)
from socialhome.domain.media_constraints import VIDEO_MAX_DURATION_SECONDS
from socialhome.domain.post import FEED_POST_MAX_IMAGES


def test_limits_follow_the_protocol_and_post_shapes():
    assert SHARE_MAX_ITEMS == 50
    assert SHARE_POST_MAX_ITEMS == FEED_POST_MAX_IMAGES
    assert SHARE_VIDEO_MAX_SECONDS == VIDEO_MAX_DURATION_SECONDS == 60
    assert ShareTarget.SPACE_ALBUM.max_items == SHARE_MAX_ITEMS
    assert ShareTarget.FEED.max_items == SHARE_POST_MAX_ITEMS
    assert ShareTarget.SPACE_POST.max_items == SHARE_POST_MAX_ITEMS
    assert ShareTarget.CHAT.max_items == SHARE_CHAT_MAX_ITEMS
    # No target takes more than one share action's cap.
    assert max(t.max_items for t in ShareTarget) == SHARE_MAX_ITEMS


def test_long_video_choices_are_named_on_the_wire():
    assert {v.value for v in LongVideo} == {"trim", "skip"}


def test_result_and_targets_serialise():
    result = ShareResult(
        shared=2,
        skipped=(ShareSkipped("i1", ShareSkip.PRIVATE),),
        trimming=1,
        queued=True,
    )
    assert result.to_dict() == {
        "shared": 2,
        "skipped": [{"item_id": "i1", "reason": "private"}],
        "trimming": 1,
        "queued": True,
    }
    targets = ShareTargets(
        spaces=(
            ShareSpaceTarget(
                space_id="s",
                name="Oma",
                emoji=None,
                space_type="private",
                households=3,
                post=True,
                post_review=False,
                album=True,
            ),
        ),
        chats=(ShareChatTarget("c", "Gisela", group=False, households=2),),
    )
    out = targets.to_dict()
    assert out["feed"] is True
    assert out["spaces"][0]["households"] == 3
    assert out["chats"] == [
        {"id": "c", "name": "Gisela", "group": False, "households": 2}
    ]
    assert out["limits"]["video_seconds"] == 60

"""Sharing household photos out as a copy (gallery D8).

Anyone who may see a household photo or video shares a **copy** of it to
the household feed, a space (as a post or into a space album) or a chat.
The copy is made from the stored file — the WebP (longest edge ≤
``IMAGE_MAX_DIMENSION``) or WebM (≤ ``VIDEO_MAX_DIMENSION``) the gallery
keeps — never an original, and from then on it follows its target's own
rules (a space's members-only, encrypted delivery; a chat's paired-only
media). Deleting the photo at home never deletes the copy.

Pure values: the limits, the targets and what a share did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .media_constraints import VIDEO_MAX_DURATION_SECONDS
from .post import FEED_POST_MAX_IMAGES

#: One share action copies at most this many photos and videos (into a
#: space album). Bounds the federation cost of one tap: items × ~0.55 MB
#: × (member households − 1).
SHARE_MAX_ITEMS: int = 50
#: A post (household feed or space) carries this many at most — the
#: photos as one image post (``FEED_POST_MAX_IMAGES``), each video its own.
SHARE_POST_MAX_ITEMS: int = FEED_POST_MAX_IMAGES
#: A chat gets one message per photo or video, this many at most.
SHARE_CHAT_MAX_ITEMS: int = 5
#: A shared video keeps at most this much (the protocol's 60 s): a longer
#: household video is trimmed to its first minute, or left out.
SHARE_VIDEO_MAX_SECONDS: int = VIDEO_MAX_DURATION_SECONDS
#: A note with the copy (the post text, or a chat message before it).
SHARE_TEXT_MAX: int = 2000


class ShareTarget(StrEnum):
    """Where the copy goes."""

    FEED = "feed"
    SPACE_POST = "space_post"
    SPACE_ALBUM = "space_album"
    CHAT = "chat"

    @property
    def max_items(self) -> int:
        if self is ShareTarget.SPACE_ALBUM:
            return SHARE_MAX_ITEMS
        if self is ShareTarget.CHAT:
            return SHARE_CHAT_MAX_ITEMS
        return SHARE_POST_MAX_ITEMS


class LongVideo(StrEnum):
    """What happens to a video longer than :data:`SHARE_VIDEO_MAX_SECONDS`
    — asked in the sheet, never decided silently."""

    #: Keep its first minute (converted again, still WebM ≤ 1920 px).
    TRIM = "trim"
    #: Leave it out of this share.
    SKIP = "skip"


class ShareSkip(StrEnum):
    """Why a picked item was not shared."""

    #: Unknown, deleted, or not one the person may see.
    NOT_FOUND = "not_found"
    #: From a private album — only its uploader shares it (D8).
    PRIVATE = "private"
    #: A video still converting: nothing to copy yet.
    PROCESSING = "processing"
    #: Longer than a minute and the person chose to leave it out.
    TOO_LONG = "too_long"
    #: Not a household photo (a space's gallery shares nothing out here).
    NOT_HOUSEHOLD = "not_household"


@dataclass(slots=True, frozen=True)
class ShareSkipped:
    item_id: str
    reason: ShareSkip

    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "reason": self.reason.value}


@dataclass(slots=True, frozen=True)
class ShareResult:
    """What one share did: ``shared`` items copied, those ``skipped`` and
    why; ``trimming`` videos being cut to their first minute (they appear
    when ready); ``queued`` when a moderated space holds the post for
    review."""

    shared: int
    skipped: tuple[ShareSkipped, ...] = ()
    trimming: int = 0
    queued: bool = False

    def to_dict(self) -> dict:
        return {
            "shared": self.shared,
            "skipped": [s.to_dict() for s in self.skipped],
            "trimming": self.trimming,
            "queued": self.queued,
        }


@dataclass(slots=True, frozen=True)
class ShareSpaceTarget:
    """A space the person may share into: as a post (``post``; under
    moderated posts it waits for review — ``post_review``) and/or into
    a space album (``album``: the space's gallery is on). ``households``
    is how many households see it (this one included)."""

    space_id: str
    name: str
    emoji: str | None
    space_type: str
    households: int
    post: bool
    post_review: bool
    album: bool

    def to_dict(self) -> dict:
        return {
            "id": self.space_id,
            "name": self.name,
            "emoji": self.emoji,
            "space_type": self.space_type,
            "households": self.households,
            "post": self.post,
            "post_review": self.post_review,
            "album": self.album,
        }


@dataclass(slots=True, frozen=True)
class ShareChatTarget:
    """A chat that takes photos and videos (a 1:1 or group with directly
    paired households only — never a relay-only chat, a space chat or a
    listing thread). ``households``: how many households it reaches."""

    conversation_id: str
    name: str
    group: bool
    households: int

    def to_dict(self) -> dict:
        return {
            "id": self.conversation_id,
            "name": self.name,
            "group": self.group,
            "households": self.households,
        }


@dataclass(slots=True, frozen=True)
class ShareTargets:
    """Where this person may share to — only targets they can already post
    to (a protected child sees fewer, never an error after picking)."""

    spaces: tuple[ShareSpaceTarget, ...]
    chats: tuple[ShareChatTarget, ...]
    feed: bool = True

    def to_dict(self) -> dict:
        return {
            "feed": self.feed,
            "spaces": [s.to_dict() for s in self.spaces],
            "chats": [c.to_dict() for c in self.chats],
            "limits": {
                "max_items": SHARE_MAX_ITEMS,
                "post_items": SHARE_POST_MAX_ITEMS,
                "chat_items": SHARE_CHAT_MAX_ITEMS,
                "video_seconds": SHARE_VIDEO_MAX_SECONDS,
            },
        }

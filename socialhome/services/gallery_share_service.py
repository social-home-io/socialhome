"""Share household photos out as a copy (gallery D8).

Anyone who may see a household photo or video shares a **copy** to the
household feed, a space (as a post or into a space album) or a chat.

* **Who may share.** Every item is checked on its own: a live household
  item the person may see (:meth:`AbstractGalleryRepo.album_visible_to`);
  one from a private album (``audience = chosen``) only by its uploader.
  A space's gallery shares nothing out here.
* **Where to.** Only targets the person can already post to
  (:meth:`targets`) — and every copy goes through the target's own
  service (:class:`FeedService`, :class:`SpaceService`,
  :class:`GalleryService`, :class:`DmService`), so its rules apply as for
  any post: a subscriber or a child's guardian blocks, moderated posts, a
  relay-only chat. No new federation event: a space copy leaves this
  household as the space's existing post / gallery events, to its member
  households only, encrypted under the space key; a chat copy as the
  existing DM media.
* **The stored format, never an original.** A photo copy is the stored
  WebP (≤ ``IMAGE_MAX_DIMENSION``) copied byte for byte — re-encoded
  through :class:`ImageProcessor` only when the stored file is somehow
  not one. A video copy is the stored WebM (≤ ``VIDEO_MAX_DIMENSION``);
  one longer than a minute is cut to its first minute by the existing
  transcode queue (still VP9/Opus WebM ≤ 1920 px) or left out — the
  person chose which in the sheet (:class:`LongVideo`).
* **The uploader is told.** One :class:`GalleryItemsShared` per share
  action and uploader (never for the sharer's own photos).

A chat copy of a trimmed video is sent once the first minute is ready (a
DM's media must exist when it is sent). That wait is held in memory: if
the household restarts before then, the trimmed video is not sent (the
rest of the share was) and a warning is logged.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.events import (
    GalleryItemsShared,
    MediaTranscodeFailed,
    MediaTranscodeReady,
)
from ..domain.gallery import AUDIENCE_CHOSEN, GalleryAlbum, GalleryItem
from ..domain.gallery_share import (
    SHARE_TEXT_MAX,
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
from ..domain.media_constraints import IMAGE_MAX_DIMENSION
from ..domain.post import PostType
from ..domain.space import (
    WRITER_ROLES,
    AccessDecision,
    ContentAction,
    ContentQueuedForReview,
)
from ..infrastructure.event_bus import EventBus
from ..media.cleanup import media_basename, unlink_unreferenced
from ..media.image_processor import ImageProcessor
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .bus_publisher import BusPublisherMixin
from .gallery_service import GalleryNotFoundError, GalleryPermissionError

if TYPE_CHECKING:
    from .dm_service import DmService
    from .feed_service import FeedService
    from .gallery_service import GalleryService
    from .media_transcode_service import MediaTranscodeService
    from .space_service import SpaceService
    from .storage_quota_service import StorageQuotaService

log = logging.getLogger(__name__)

#: Files are copied in pieces of this size — a 5-minute video is never
#: held in memory.
_COPY_CHUNK_BYTES: int = 1024 * 1024
_IMAGE_MIME = "image/webp"
_VIDEO_MIME = "video/webm"


@dataclass(slots=True, frozen=True)
class _Copy:
    """One shared item's copy on disk: ``url`` / ``thumbnail_url`` are
    fresh files under the media root; ``trimming`` while the transcode
    queue cuts it to its first minute."""

    source: GalleryItem
    url: str
    thumbnail_url: str
    size: int
    trimming: bool = False

    @property
    def is_video(self) -> bool:
        return self.source.item_type == "video"


@dataclass(slots=True, frozen=True)
class _PendingChatVideo:
    """A trimmed video waiting to go into a chat once it is ready."""

    conversation_id: str
    sender_username: str
    media_url: str


class GalleryShareService(BusPublisherMixin):
    """Copies household photos and videos to a feed, space or chat."""

    __slots__ = (
        "_bus",
        "_convos",
        "_dms",
        "_feed",
        "_gallery",
        "_media_dir",
        "_media_refs",
        "_pending_chat",
        "_repo",
        "_space_repo",
        "_spaces",
        "_storage_quota",
        "_transcode_repo",
        "_transcode_service",
        "_users",
    )

    def __init__(
        self,
        *,
        repo: AbstractGalleryRepo,
        gallery: GalleryService,
        feed: FeedService,
        spaces: SpaceService,
        space_repo: AbstractSpaceRepo,
        dms: DmService,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        transcode_repo: AbstractMediaTranscodeRepo,
        transcode_service: MediaTranscodeService,
        bus: EventBus,
        media_dir: pathlib.Path,
        media_refs: AbstractMediaReferenceRepo | None = None,
        storage_quota: StorageQuotaService | None = None,
    ) -> None:
        self._repo = repo
        self._gallery = gallery
        self._feed = feed
        self._spaces = spaces
        self._space_repo = space_repo
        self._dms = dms
        self._convos = conversation_repo
        self._users = user_repo
        self._transcode_repo = transcode_repo
        self._transcode_service = transcode_service
        self._bus = bus
        self._media_dir = media_dir
        self._media_refs = media_refs
        self._storage_quota = storage_quota
        #: Trimmed chat videos waiting for their first minute, by the
        #: transcode's output file name.
        self._pending_chat: dict[str, _PendingChatVideo] = {}

    def attach_space_service(self, spaces: SpaceService) -> None:
        """The space service with this household's real instance id —
        start-up swaps it in after the placeholder this was built with."""
        self._spaces = spaces

    def wire(self) -> None:
        """Send a trimmed chat video once its first minute is ready."""
        if self._bus is None:
            return
        self._bus.subscribe(MediaTranscodeReady, self._on_transcode_ready)
        self._bus.subscribe(MediaTranscodeFailed, self._on_transcode_failed)

    # ─── Targets ──────────────────────────────────────────────────────────

    async def targets(self, *, actor_user_id: str) -> ShareTargets:
        """Where *actor_user_id* may share a copy: the household feed, the
        spaces they write in (as a post where posts are open or reviewed,
        into an album where the gallery is on) and the chats that take
        photos. A child's list follows the rules already on their seats
        and chats — what they can't post to is never offered."""
        return ShareTargets(
            spaces=tuple(await self._space_targets(actor_user_id)),
            chats=tuple(await self._chat_targets(actor_user_id)),
        )

    async def _space_targets(self, actor_user_id: str) -> list[ShareSpaceTarget]:
        spaces: list[ShareSpaceTarget] = []
        for space in await self._space_repo.list_for_user(actor_user_id):
            if space.archived:
                continue
            member = await self._space_repo.get_member(space.id, actor_user_id)
            if member is None or member.role not in WRITER_ROLES:
                continue
            decision = space.features.access_decision(
                "posts",
                role=member.role,
                action=ContentAction.CREATE,
                owns_target=False,
            )
            post = decision is not AccessDecision.DENY and space.features.allows(
                PostType.IMAGE
            )
            album = space.features.gallery
            if not post and not album:
                continue
            spaces.append(
                ShareSpaceTarget(
                    space_id=space.id,
                    name=space.name,
                    emoji=space.emoji,
                    space_type=space.space_type.value,
                    # The member households (``space_instances`` — this one
                    # included once it holds a seat) and the host.
                    households=len(
                        {
                            *await self._space_repo.list_member_instances(space.id),
                            space.owner_instance_id,
                        }
                    ),
                    post=post,
                    post_review=post and decision is AccessDecision.QUEUE,
                    album=album,
                )
            )
        spaces.sort(key=lambda s: s.name.casefold())
        return spaces

    async def _chat_targets(self, actor_user_id: str) -> list[ShareChatTarget]:
        user = await self._users.get_by_user_id(actor_user_id)
        if user is None:
            return []
        out: list[ShareChatTarget] = []
        for conv in await self._dms.list_conversations(user.username):
            if not await self._dms.media_allowed(conv.id):
                continue
            members = await self._convos.list_members(conv.id)
            remote = await self._convos.list_remote_members(conv.id)
            names: list[str] = []
            for m in members:
                if m.username == user.username or m.deleted_at is not None:
                    continue
                other = await self._users.get(m.username)
                names.append(
                    (other.display_name or other.username) if other else m.username
                )
            for rm in remote:
                # The name the chat list shows: the paired household's
                # directory entry, else the group roster's.
                ru = await self._users.get_remote_by_member(
                    rm.instance_id, rm.remote_username
                )
                names.append(
                    (ru.display_name if ru is not None else None)
                    or rm.display_name
                    or rm.remote_username
                    or ""
                )
            label = conv.name or ", ".join(n for n in names if n)
            if not label:
                continue
            out.append(
                ShareChatTarget(
                    conversation_id=conv.id,
                    name=label,
                    group=conv.type.value != "dm",
                    households=1 + len({rm.instance_id for rm in remote}),
                )
            )
        return out

    # ─── Share ────────────────────────────────────────────────────────────

    async def share(
        self,
        item_ids: list[str],
        *,
        actor_user_id: str,
        target: ShareTarget,
        space_id: str | None = None,
        album_id: str | None = None,
        new_album_name: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        long_videos: LongVideo = LongVideo.SKIP,
    ) -> ShareResult:
        """Copy the picked photos and videos to *target*.

        Over the target's limit (:attr:`ShareTarget.max_items`) is refused
        before anything is copied, and so is a target the person can't
        post to. Items they may not share are skipped and named in the
        result, never an error."""
        ids = list(dict.fromkeys(str(i) for i in item_ids if i))
        if not ids or len(ids) > target.max_items:
            raise ValueError(f"1–{target.max_items} items per share to {target.value}")
        note = (text or "").strip() or None
        if note is not None and len(note) > SHARE_TEXT_MAX:
            raise ValueError(f"text must be {SHARE_TEXT_MAX} characters or fewer")
        user = await self._users.get_by_user_id(actor_user_id)
        if user is None:
            raise GalleryPermissionError("unknown person")

        picked, skipped = await self._pick(ids, actor_user_id, long_videos)
        album = await self._check_target(
            target,
            actor_user_id=actor_user_id,
            space_id=space_id,
            album_id=album_id,
            new_album_name=new_album_name,
            conversation_id=conversation_id,
            videos=any(i.item_type == "video" for i, _ in picked),
        )
        if not picked:
            return ShareResult(shared=0, skipped=tuple(skipped))
        if self._storage_quota is not None:
            await self._storage_quota.check_can_store(
                sum([await self._file_size(i.url) for i, _ in picked])
            )

        copies: list[_Copy] = []
        try:
            for item, trim in picked:
                copies.append(
                    await self._copy(
                        item,
                        trim=trim,
                        # A space album's grid needs a thumbnail; a post or
                        # a chat shows the picture itself.
                        thumbnail=target is ShareTarget.SPACE_ALBUM,
                    )
                )
            if target is ShareTarget.SPACE_ALBUM and album is None and space_id:
                created = await self._gallery.create_album(
                    space_id=space_id,
                    owner_user_id=actor_user_id,
                    name=(new_album_name or "").strip(),
                )
                album = await self._gallery.album_for_copy(
                    created.id, actor_user_id=actor_user_id
                )
            queued = await self._deliver(
                target,
                copies,
                actor_user_id=actor_user_id,
                username=user.username,
                space_id=space_id,
                album=album,
                conversation_id=conversation_id,
                text=note,
            )
        except BaseException:
            await self._remove_unused(copies)
            raise
        await self._tell_uploaders(
            copies,
            actor_user_id=actor_user_id,
            target=target,
            space_id=space_id,
        )
        return ShareResult(
            shared=len(copies),
            skipped=tuple(skipped),
            trimming=sum(1 for c in copies if c.trimming),
            queued=queued,
        )

    async def _pick(
        self, ids: list[str], actor_user_id: str, long_videos: LongVideo
    ) -> tuple[list[tuple[GalleryItem, bool]], list[ShareSkipped]]:
        """The items this person may share — each with whether it must be
        trimmed — and the ones left out, with why."""
        picked: list[tuple[GalleryItem, bool]] = []
        skipped: list[ShareSkipped] = []
        albums: dict[str, GalleryAlbum | None] = {}
        for iid in ids:
            reason = None
            item = await self._repo.get_item(iid)
            album = None
            if item is not None:
                if item.album_id not in albums:
                    albums[item.album_id] = await self._repo.get_album(item.album_id)
                album = albums[item.album_id]
            if item is None or album is None:
                reason = ShareSkip.NOT_FOUND
            elif album.space_id is not None:
                reason = ShareSkip.NOT_HOUSEHOLD
            else:
                reason = await self._may_share(item, album, actor_user_id)
            if reason is None and item is not None and item.item_type == "video":
                if not await self._video_ready(item):
                    reason = ShareSkip.PROCESSING
                elif (
                    item.duration_s or 0
                ) > SHARE_VIDEO_MAX_SECONDS and long_videos is LongVideo.SKIP:
                    reason = ShareSkip.TOO_LONG
            if reason is not None or item is None:
                skipped.append(ShareSkipped(iid, reason or ShareSkip.NOT_FOUND))
                continue
            trim = (
                item.item_type == "video"
                and (item.duration_s or 0) > SHARE_VIDEO_MAX_SECONDS
            )
            picked.append((item, trim))
        return picked, skipped

    async def _may_share(
        self, item: GalleryItem, album: GalleryAlbum, actor_user_id: str
    ) -> ShareSkip | None:
        """``None`` when *actor_user_id* may share this household item: the
        album is one they may see, and a private album's photos are shared
        by their uploader only (D8)."""
        if album.is_system:
            return None
        if not await self._repo.album_visible_to(album.id, actor_user_id):
            return ShareSkip.NOT_FOUND
        audience = await self._repo.get_album_audience(album.id)
        if (
            audience is not None
            and audience.audience == AUDIENCE_CHOSEN
            and item.uploaded_by != actor_user_id
        ):
            return ShareSkip.PRIVATE
        return None

    async def _video_ready(self, item: GalleryItem) -> bool:
        name = media_basename(item.url)
        if name is None:
            return False
        if await self._transcode_repo.status_for([name]):
            return False
        return bool(await aiofiles.os.path.isfile(self._media_dir / name))

    async def _check_target(
        self,
        target: ShareTarget,
        *,
        actor_user_id: str,
        space_id: str | None,
        album_id: str | None,
        new_album_name: str | None,
        conversation_id: str | None,
        videos: bool,
    ) -> GalleryAlbum | None:
        """Refuse a target the person can't post to before anything is
        copied. Returns the space album for :attr:`ShareTarget.SPACE_ALBUM`
        (``None`` when a new one is to be made)."""
        if target is ShareTarget.FEED:
            return None
        if target is ShareTarget.CHAT:
            if not conversation_id or not await self._dms.media_allowed(
                conversation_id
            ):
                raise GalleryNotFoundError("chat not found")
            return None
        if not space_id:
            raise ValueError("space_id is required")
        offered = {s.space_id: s for s in await self._space_targets(actor_user_id)}
        space = offered.get(space_id)
        if space is None:
            raise GalleryPermissionError("You can't share into this space")
        if target is ShareTarget.SPACE_POST:
            if not space.post:
                raise GalleryPermissionError("This space takes no posts from you")
            full = await self._space_repo.get(space_id)
            if videos and full is not None and not full.features.allows(PostType.VIDEO):
                raise GalleryPermissionError("This space takes no video posts")
            return None
        if not space.album:
            raise GalleryPermissionError("This space's gallery is off")
        if album_id:
            album = await self._gallery.album_for_copy(
                album_id, actor_user_id=actor_user_id
            )
            if album.space_id != space_id:
                raise GalleryNotFoundError("album not found")
            return album
        if not (new_album_name or "").strip():
            raise ValueError("album_id or new_album_name is required")
        return None

    # ─── Copies ───────────────────────────────────────────────────────────

    async def _copy(self, item: GalleryItem, *, trim: bool, thumbnail: bool) -> _Copy:
        if item.item_type == "video":
            return await self._copy_video(item, trim=trim)
        return await self._copy_photo(item, thumbnail=thumbnail)

    async def _copy_photo(self, item: GalleryItem, *, thumbnail: bool) -> _Copy:
        """The stored WebP, copied — re-encoded by :class:`ImageProcessor`
        only when the stored file is not a WebP ≤ ``IMAGE_MAX_DIMENSION``
        (it always is: every gallery photo went through it)."""
        source = self._path(item.url)
        async with aiofiles.open(source, "rb") as f:
            data = await f.read()
        proc = ImageProcessor()
        name = f"{uuid.uuid4().hex}.webp"
        if not await proc.is_valid_webp(data, max_dimension=IMAGE_MAX_DIMENSION):
            data, name = await proc.process(data, "share")
        await self._write(name, data)
        url = f"api/media/{name}"
        thumb_url = url
        if thumbnail:
            thumb_name = f"{uuid.uuid4().hex}.webp"
            thumb_src = self._maybe_path(item.thumbnail_url)
            if (
                thumb_src is not None
                and item.thumbnail_url != item.url
                and await aiofiles.os.path.isfile(thumb_src)
            ):
                await self._copy_file(thumb_src, self._media_dir / thumb_name)
            else:
                await self._write(thumb_name, await proc.generate_thumbnail(data))
            thumb_url = f"api/media/{thumb_name}"
        return _Copy(source=item, url=url, thumbnail_url=thumb_url, size=len(data))

    async def _copy_video(self, item: GalleryItem, *, trim: bool) -> _Copy:
        """The stored WebM and its poster under one fresh stem — or, to
        keep only the first minute, a transcode job on a copy of the
        stored WebM (the existing queue: VP9/Opus WebM ≤ 1920 px, the
        poster written with it)."""
        source = self._path(item.url)
        stem = uuid.uuid4().hex
        url = f"api/media/{stem}.webm"
        poster_url = f"api/media/{stem}.webp"
        size = (await aiofiles.os.stat(source)).st_size
        if trim:
            temp_dir = self._media_dir / "transcode_src"
            await aiofiles.os.makedirs(temp_dir, exist_ok=True)
            temp = temp_dir / f"{uuid.uuid4().hex}.bin"
            await self._copy_file(source, temp)
            await self._transcode_repo.enqueue(
                output_filename=f"{stem}.webm",
                source_path=str(temp),
                thumbnail_filename=f"{stem}.webp",
                owner_user_id=item.uploaded_by,
                duration_s=float(SHARE_VIDEO_MAX_SECONDS),
            )
            self._transcode_service.nudge()
            copy = _Copy(item, url, poster_url, size, trimming=True)
            return replace(
                copy,
                source=replace(item, duration_s=float(SHARE_VIDEO_MAX_SECONDS)),
            )
        await self._copy_file(source, self._media_dir / f"{stem}.webm")
        poster = self._maybe_path(item.thumbnail_url)
        if poster is not None and await aiofiles.os.path.isfile(poster):
            await self._copy_file(poster, self._media_dir / f"{stem}.webp")
        return _Copy(source=item, url=url, thumbnail_url=poster_url, size=size)

    # ─── Delivery ─────────────────────────────────────────────────────────

    async def _deliver(
        self,
        target: ShareTarget,
        copies: list[_Copy],
        *,
        actor_user_id: str,
        username: str,
        space_id: str | None,
        album: GalleryAlbum | None,
        conversation_id: str | None,
        text: str | None,
    ) -> bool:
        """Hand the copies to the target's own service. ``True`` when a
        moderated space holds the post for review."""
        if target is ShareTarget.SPACE_ALBUM:
            if album is None:
                raise ValueError("no album to share into")
            for c in copies:
                await self._gallery.add_copy(
                    album,
                    replace(
                        c.source,
                        uploaded_by=actor_user_id,
                        url=c.url,
                        thumbnail_url=c.thumbnail_url,
                        # The copy goes out under the sharer's name: only
                        # their own words go with it, never someone else's.
                        caption=(
                            c.source.caption
                            if c.source.uploaded_by == actor_user_id
                            else None
                        ),
                    ),
                )
            return False
        if target is ShareTarget.CHAT:
            if conversation_id is None:
                raise ValueError("conversation_id is required")
            first = True
            for c in copies:
                if c.trimming:
                    self._pending_chat[media_basename(c.url) or ""] = _PendingChatVideo(
                        conversation_id, username, c.url
                    )
                    continue
                await self._send_chat(
                    conversation_id,
                    username,
                    c.url,
                    video=c.is_video,
                    size=c.size,
                    text=text if first else None,
                )
                first = False
            if first and text:
                # Every copy is a video still being trimmed: the note goes now.
                await self._dms.send_message(
                    conversation_id, sender_username=username, content=text
                )
            return False
        return await self._post(
            target, copies, actor_user_id=actor_user_id, space_id=space_id, text=text
        )

    async def _post(
        self,
        target: ShareTarget,
        copies: list[_Copy],
        *,
        actor_user_id: str,
        space_id: str | None,
        text: str | None,
    ) -> bool:
        """The photos as one image post, each video as its own post; the
        note goes with the first."""
        photos = tuple(c.url for c in copies if not c.is_video)
        posts: list[tuple[PostType, dict]] = []
        if photos:
            posts.append((PostType.IMAGE, {"image_urls": photos}))
        posts.extend(
            (PostType.VIDEO, {"media_url": c.url}) for c in copies if c.is_video
        )
        queued = False
        for n, (ptype, media) in enumerate(posts):
            content = text if n == 0 else None
            if target is ShareTarget.FEED:
                await self._feed.create_post(
                    author_user_id=actor_user_id, type=ptype, content=content, **media
                )
                continue
            if space_id is None:
                raise ValueError("space_id is required")
            try:
                await self._spaces.create_post(
                    space_id,
                    author_user_id=actor_user_id,
                    type=ptype,
                    content=content,
                    **media,
                )
            except ContentQueuedForReview:
                queued = True
        return queued

    async def _send_chat(
        self,
        conversation_id: str,
        username: str,
        media_url: str,
        *,
        video: bool,
        size: int,
        text: str | None = None,
    ) -> None:
        await self._dms.send_message(
            conversation_id,
            sender_username=username,
            content=text or "",
            type="video" if video else "image",
            media_url=media_url,
            file_name=media_basename(media_url),
            mime_type=_VIDEO_MIME if video else _IMAGE_MIME,
            file_size_bytes=size,
        )

    async def _on_transcode_ready(self, event: MediaTranscodeReady) -> None:
        pending = self._pending_chat.pop(event.output_filename, None)
        if pending is None:
            return
        path = self._media_dir / event.output_filename
        try:
            size = (await aiofiles.os.stat(path)).st_size
            await self._send_chat(
                pending.conversation_id,
                pending.sender_username,
                pending.media_url,
                video=True,
                size=size,
            )
        except Exception:
            log.warning(
                "gallery share: a trimmed video could not be sent to the chat",
                exc_info=True,
            )

    async def _on_transcode_failed(self, event: MediaTranscodeFailed) -> None:
        if self._pending_chat.pop(event.output_filename, None) is not None:
            log.warning(
                "gallery share: trimming a video for a chat failed; it was not sent"
            )

    # ─── The uploader is told ─────────────────────────────────────────────

    async def _tell_uploaders(
        self,
        copies: list[_Copy],
        *,
        actor_user_id: str,
        target: ShareTarget,
        space_id: str | None,
    ) -> None:
        """One :class:`GalleryItemsShared` per uploader who isn't the
        sharer — "Anna shared 2 of your photos in “Grandparents”"."""
        share_id = uuid.uuid4().hex
        by_uploader: dict[str, list[GalleryItem]] = {}
        for c in copies:
            if c.source.uploaded_by and c.source.uploaded_by != actor_user_id:
                by_uploader.setdefault(c.source.uploaded_by, []).append(c.source)
        where = (
            "feed"
            if target is ShareTarget.FEED
            else "chat"
            if target is ShareTarget.CHAT
            else "space"
        )
        for uploader, items in by_uploader.items():
            videos = sum(1 for i in items if i.item_type == "video")
            await self._emit(
                GalleryItemsShared(
                    share_id=share_id,
                    sharer=actor_user_id,
                    uploader=uploader,
                    photos=len(items) - videos,
                    videos=videos,
                    target=where,
                    album_id=items[0].album_id,
                    space_id=space_id if where == "space" else None,
                )
            )

    # ─── Files ────────────────────────────────────────────────────────────

    def _maybe_path(self, url: str | None) -> pathlib.Path | None:
        name = media_basename(url)
        return None if name is None else self._media_dir / name

    def _path(self, url: str) -> pathlib.Path:
        path = self._maybe_path(url)
        if path is None:
            raise GalleryNotFoundError("the stored file is missing")
        return path

    async def _file_size(self, url: str) -> int:
        path = self._maybe_path(url)
        if path is None or not await aiofiles.os.path.isfile(path):
            raise GalleryNotFoundError("the stored file is missing")
        return int((await aiofiles.os.stat(path)).st_size)

    async def _write(self, name: str, data: bytes) -> None:
        await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
        async with aiofiles.open(self._media_dir / name, "wb") as f:
            await f.write(data)

    async def _copy_file(self, source: pathlib.Path, dest: pathlib.Path) -> None:
        async with aiofiles.open(source, "rb") as src, aiofiles.open(dest, "wb") as out:
            while chunk := await src.read(_COPY_CHUNK_BYTES):
                await out.write(chunk)

    async def _remove_unused(self, copies: Iterable[_Copy]) -> None:
        """A share that failed part-way leaves no file nobody names: a file
        a delivered post, item or message already names stays (its row
        does), the rest go (:func:`unlink_unreferenced`)."""
        await unlink_unreferenced(
            self._media_dir,
            self._media_refs,
            [u for c in copies for u in (c.url, c.thumbnail_url)],
        )

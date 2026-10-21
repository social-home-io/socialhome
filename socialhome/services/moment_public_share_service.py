"""Per-moment share links for public moments (§Momentum-public).

Owner decision 2026-10-20: a stranger sees a public moment only through a
link its author shares — the GFS's ``/moments/<person>`` page shows the
profile and a Follow button, nothing else.

A link opens the **pinned viewer** (``socialhome/pinned_viewer.py`` — a
static, SRI-pinned page no GFS serves) with everything in the fragment,
which never reaches a server::

    {viewer}/v/<hash>/#g=<GFS>&a=<author>&m=<moment>&b=<share copy>&k=<item key>

``k`` is the moment's **item key** (``moment_public_seal.item_key``) — the
key that moment's envelope and file are sealed under, and nothing else: not
the author's generation key, so a link opens exactly one moment. ``b`` is a
**share copy** (``moment_public_media.seal_share``: text, timestamps, the
author's name, the media descriptor — sealed under the item key, padded to
4 KB) uploaded to that GFS on the first request; the viewer fetches it and
the moment's file from the GFS, checks their addresses and opens them in the
browser. The author's household is never contacted, and never streams to a
viewer.

The item key comes from the day's ratchet step, which the household
overwrites daily, so it is kept per public moment at publish
(:meth:`record`, ``moment_public_shares``, KEK-sealed) — and goes with the
moment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..crypto import b64url_encode
from ..domain.errors import CodedError
from ..domain.moment import Moment
from ..domain.moment_public import MomentPublicShare
from ..moment_public_media import blob_id_of, seal_share
from ..pinned_viewer import PinnedViewer, viewer_base_url, viewer_gfs_base
from .moment_public_media_outbound import PreparedMedia
from .moment_service import MomentNotFoundError

if TYPE_CHECKING:
    from ..infrastructure.key_manager import KeyManager
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicRegistrationRepo,
        AbstractMomentPublicShareRepo,
    )
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_public_media_outbound import MomentPublicMediaOutbound

log = logging.getLogger(__name__)

#: A share copy lives no longer than a moment can (the GFS refuses more).
SHARE_KEEP_AT_MOST_S: int = 7 * 86_400


class ShareLinkUnavailableError(CodedError):
    """No link can be minted right now: no pinned viewer is configured, or
    no GFS this moment went through keeps encrypted media (or it refused
    the share copy)."""

    status = 409
    code = "SHARE_LINK_UNAVAILABLE"
    detail = "A share link can't be made for this moment right now."


class MomentPublicShareService:
    """Keep a public moment's link material; mint its share link."""

    __slots__ = (
        "_base_url",
        "_gfs_repo",
        "_kek",
        "_media",
        "_moments",
        "_regs",
        "_shares",
        "_users",
        "_viewer",
    )

    def __init__(
        self,
        *,
        share_repo: AbstractMomentPublicShareRepo,
        moment_repo: AbstractMomentRepo,
        user_repo: AbstractUserRepo,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        media_outbound: MomentPublicMediaOutbound,
    ) -> None:
        self._shares = share_repo
        self._moments = moment_repo
        self._users = user_repo
        self._regs = registration_repo
        self._gfs_repo = gfs_repo
        self._media = media_outbound
        self._kek: KeyManager | None = None
        self._base_url = ""
        self._viewer: PinnedViewer | None = None

    def attach_identity(self, *, key_manager: KeyManager) -> None:
        self._kek = key_manager

    def attach_viewer(self, *, base_url: str, viewer: PinnedViewer | None) -> None:
        self._base_url = base_url
        self._viewer = viewer

    async def record(
        self, moment: Moment, *, item_key: bytes, media: dict | None
    ) -> None:
        """Keep what a later link needs — called once when a public moment
        is published. The item key is KEK-sealed, bound to the moment id."""
        if self._kek is None:
            return
        await self._shares.save(
            MomentPublicShare(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                item_key_sealed=self._kek.encrypt(
                    item_key, associated_data=_context(moment.id)
                ),
                media_json=json.dumps(media, sort_keys=True) if media else None,
            )
        )

    async def link(
        self, *, user_id: str, moment_id: str, gfs_id: str | None = None
    ) -> str:
        """The share link for *user_id*'s public moment *moment_id*.

        :raises MomentNotFoundError: not the caller's live public moment.
        :raises ShareLinkUnavailableError: no viewer / GFS can carry it.
        """
        moment = await self._moments.get(moment_id)
        share = await self._shares.get(moment_id)
        if (
            moment is None
            or share is None
            or self._kek is None
            or not moment.is_public
            or moment.author_user_id != user_id
            or share.author_user_id != user_id
            or _unix(moment.expires_at) <= time.time()
        ):
            raise MomentNotFoundError(moment_id)
        base = viewer_base_url(self._base_url)
        if base is None or self._viewer is None:
            raise ShareLinkUnavailableError()
        target, gfs_base = await self._target(user_id, share, gfs_id)
        item_key = self._kek.decrypt(
            share.item_key_sealed, associated_data=_context(moment_id)
        )
        blob_id = share.share_blob_id if share.share_gfs_id == target else None
        if blob_id is None:
            blob_id = await self._upload_copy(moment, share, item_key, target)
        fragment = urlencode(
            [
                ("g", gfs_base),
                ("a", user_id),
                ("m", moment_id),
                ("b", blob_id),
                ("k", b64url_encode(item_key)),
            ]
        )
        return f"{base}/{self._viewer.path}#{fragment}"

    async def _target(
        self, user_id: str, share: MomentPublicShare, gfs_id: str | None
    ) -> tuple[str, str]:
        """The GFS the link names: the one asked for, else the one already
        holding the share copy, else the first that keeps encrypted media —
        always one this author publishes through and the viewer accepts."""
        regs = [r.gfs_id for r in await self._regs.list_for_user(user_id)]
        if gfs_id is not None:
            candidates = [gfs_id] if gfs_id in regs else []
        elif share.share_gfs_id in regs:
            candidates = [share.share_gfs_id, *regs]
        else:
            candidates = regs
        for candidate in candidates:
            if candidate is None or not await self._media.supported(candidate):
                continue
            conn = await self._gfs_repo.get(candidate)
            gfs_base = viewer_gfs_base(conn.inbox_url) if conn is not None else None
            if conn is not None and conn.status == "active" and gfs_base:
                return candidate, gfs_base
        raise ShareLinkUnavailableError()

    async def _upload_copy(
        self, moment: Moment, share: MomentPublicShare, item_key: bytes, gfs_id: str
    ) -> str:
        author = await self._users.get_by_user_id(moment.author_user_id)
        media = json.loads(share.media_json) if share.media_json else None
        body = {
            "v": 1,
            "content": moment.content,
            "created_at": moment.created_at,
            "expires_at": moment.expires_at,
            "author_name": author.display_name if author is not None else None,
            "media_type": moment.media_type if media else None,
            "duration_ms": moment.duration_ms if media else None,
            "media": media,
        }
        blob = await asyncio.to_thread(
            seal_share,
            item_key=item_key,
            author_user_id=moment.author_user_id,
            moment_id=moment.id,
            body=body,
        )
        blob_id = blob_id_of(blob)
        prepared = PreparedMedia(blobs={blob_id: blob})
        expires = min(_unix(moment.expires_at), int(time.time()) + SHARE_KEEP_AT_MOST_S)
        if not await self._media.upload(
            gfs_id, prepared, author_user_id=moment.author_user_id, expires_at=expires
        ):
            raise ShareLinkUnavailableError()
        self._media.remember(moment.author_user_id, moment.id, gfs_id, prepared)
        await self._shares.set_share_copy(moment.id, blob_id=blob_id, gfs_id=gfs_id)
        log.info(
            "moment_public.share: share copy of moment %s on %s", moment.id, gfs_id
        )
        return blob_id


def _context(moment_id: str) -> bytes:
    return f"moment-public-share|{moment_id}".encode()


def _unix(iso: str) -> int:
    when = datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp())

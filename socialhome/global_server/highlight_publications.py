"""GFS highlight-publication registry (§highlights_public).

The GFS-side counterpart to :class:`HighlightPublicationService`. Its job
is narrow: when a SH instance posts a signed publish envelope, this
service records the row + mints share tokens. When the public landing
page resolves an ``(instance_id, highlight_id, token)`` URL triple, it
returns enough metadata for the page to bootstrap a WebRTC offer.

The registry **never** receives or stores highlight content. It holds only:
- one row in ``gfs_highlight_publications`` per ``(highlight_id, instance_id)``
  with the absolute ``expires_at`` and a cached publish signature.
- N rows in ``gfs_highlight_tokens`` per publication — each a revocable
  share link with an optional human label.

Author-online detection reads :class:`GfsWebSocketRegistry`: a
publication is only servable while the author's instance has a live
WS to this GFS, since that is the same WS we use to push the WebRTC
signalling offer to the author when a viewer arrives.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from time import time

from .domain import GfsHighlightPublication, GfsHighlightToken
from .repositories import (
    AbstractGfsHighlightPublicationRepo,
    AbstractGfsHighlightTokenRepo,
)
from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ResolvedToken:
    """Result of a public landing-page token lookup."""

    token: GfsHighlightToken
    publication: GfsHighlightPublication


class HighlightPublicationRegistry:
    """Records publish rows + mints / revokes share tokens."""

    __slots__ = ("_pubs", "_tokens", "_ws", "_base_url")

    def __init__(
        self,
        pubs: AbstractGfsHighlightPublicationRepo,
        tokens: AbstractGfsHighlightTokenRepo,
        ws: GfsWebSocketRegistry,
        *,
        base_url: str,
    ) -> None:
        self._pubs = pubs
        self._tokens = tokens
        self._ws = ws
        # Trim a trailing slash so URL building stays a single template.
        self._base_url = base_url.rstrip("/")

    # ── Publish flow ─────────────────────────────────────────────────────

    async def record_publish(
        self,
        *,
        highlight_id: str,
        instance_id: str,
        expires_at: int,
        publish_signature: str,
        label: str | None = None,
    ) -> tuple[GfsHighlightToken, str]:
        """Upsert the publication row, mint a fresh token, return the URL.

        The first publish creates the publication; later calls keep the
        same row but mint additional tokens. Returns ``(token_row, url)``.
        """
        pub = GfsHighlightPublication(
            highlight_id=highlight_id,
            instance_id=instance_id,
            expires_at=expires_at,
            published_at=int(time()),
            publish_signature=publish_signature,
        )
        await self._pubs.upsert(pub)
        return await self.mint_token(
            highlight_id=highlight_id,
            instance_id=instance_id,
            label=label,
        )

    async def mint_token(
        self,
        *,
        highlight_id: str,
        instance_id: str,
        label: str | None = None,
    ) -> tuple[GfsHighlightToken, str]:
        """Mint another share token under an existing publication."""
        existing = await self._pubs.get(highlight_id, instance_id)
        if existing is None:
            raise LookupError(
                f"Publication {highlight_id=}/{instance_id=} not found — "
                "publish first before minting extra tokens.",
            )
        token = GfsHighlightToken(
            token=_new_token(),
            highlight_id=highlight_id,
            instance_id=instance_id,
            label=label,
            created_at=int(time()),
            revoked_at=None,
        )
        await self._tokens.insert(token)
        return token, self._build_url(instance_id, highlight_id, token.token)

    # ── Revoke / unpublish flow ──────────────────────────────────────────

    async def revoke_token(self, token: str, instance_id: str) -> bool:
        """Mark a single share token revoked. Returns True if anything
        actually changed (so the route can return 404 on stale tokens)."""
        n = await self._tokens.revoke(token, instance_id, now=int(time()))
        return n > 0

    async def remove_publish(self, highlight_id: str, instance_id: str) -> bool:
        """Drop the publication row. CASCADE drops every token under it.
        Returns True if a row was actually deleted."""
        n = await self._pubs.delete(highlight_id, instance_id)
        return n > 0

    # ── Public landing-page resolution ───────────────────────────────────

    async def resolve_token(self, token: str) -> ResolvedToken | None:
        """Land an opaque token on its (publication, token row) pair.

        Returns ``None`` when the token is unknown, has been revoked, or
        the publication's ``expires_at`` has passed. The anonymous
        offer / relay routes fold ``None`` into the uniform 503.
        """
        hit = await self._tokens.lookup_active(token, now=int(time()))
        if hit is None:
            return None
        tok, pub = hit
        return ResolvedToken(token=tok, publication=pub)

    async def get_publication(
        self,
        highlight_id: str,
        instance_id: str,
    ) -> GfsHighlightPublication | None:
        """Read-only fetch of the publication row.

        Routes that need the absolute ``expires_at`` query through this
        rather than poking the repo directly so the registry stays the
        single point of truth for the §highlights_public storage state.
        """
        return await self._pubs.get(highlight_id, instance_id)

    async def author_online(self, instance_id: str) -> bool:
        """True iff the author's SH has a live WS to this GFS.

        A publication is only servable when the author is online, since
        the viewer's offer / relay asks the GFS to push a signalling frame
        to that exact WS. Callers MUST fold ``False`` into the same uniform
        reply as an unknown or withdrawn publication
        (:mod:`.public_unavailable`) — never surface it on its own, or the
        anonymous routes become an author-presence oracle.
        """
        return self._ws.is_connected(instance_id)

    # ── URL helper ───────────────────────────────────────────────────────

    def _build_url(self, instance_id: str, highlight_id: str, token: str) -> str:
        return f"{self._base_url}/highlight/{instance_id}/{highlight_id}/{token}"


def _new_token() -> str:
    """32-byte urlsafe-base64 token. Roughly the same entropy a peer
    pairing token carries."""
    return secrets.token_urlsafe(32)

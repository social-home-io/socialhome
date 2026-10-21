"""Highlight public-publication service (§highlights_public).

Author opts a single highlight into public sharing via a paired GFS. The
GFS mints a one-shot share token and returns a URL the author can copy
into Twitter / email / SMS. Anyone visiting that URL gets a public
landing page; the page bootstraps a WebRTC DataChannel directly to the
author's instance, so the actual highlight bytes never transit GFS.

This service runs on the **author's SH** and handles only the
HTTP-signed publish / revoke / unpublish exchange with the GFS — the
WebRTC streaming layer arrives in PR2 (``HighlightSignalingHandler``).

Per :class:`GfsConnectionService`, every body is canonicalised
(``json.dumps(sort_keys=True, separators=(",",":"))``) and signed with
the local instance's Ed25519 key. The signature lands in a
``"signature"`` field appended to the body; the GFS-side
``_rtc_authenticate`` middleware strips and verifies it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Protocol

import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..domain.child_protection import ProtectedCapability
from ..domain.errors import CodedError
from ..domain.federation import GfsConnection
from ..pinned_viewer import (
    PinnedViewer,
    pinned_link,
    viewer_base_url,
    viewer_gfs_base,
)
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.highlight_repo import AbstractHighlightRepo
from .highlight_public_seal import (
    link_secret,
    link_with_secret,
    token_digest,
    usable_token,
)
from .protection_gate import ProtectionGateMixin

log = logging.getLogger(__name__)


class HighlightPublicationError(Exception):
    """Raised when a publish / revoke / unpublish round-trip fails.

    Mapped centrally by :class:`BaseView._iter` to HTTP 502 so the SPA
    surfaces a "GFS unavailable" message rather than a generic 500.
    """

    __slots__ = ()


class GfsTooOldError(CodedError, HighlightPublicationError):
    """The GFS has not proved, under a valid signature, that its public
    highlight page opens the end-to-end sealed stream
    (``sealed_highlight_stream`` on ``GET /gfs/info``). This household streams
    only sealed frames, so a link minted there would be dead for every
    viewer: nothing is minted until that GFS is updated."""

    status = 409
    code = "GFS_TOO_OLD"
    detail = "This GFS needs an update before it can share highlights publicly."


class SealedHighlightCapability(Protocol):
    """The capability check :class:`GfsConnectionService` answers."""

    async def sealed_highlight_stream_supported(self, conn: GfsConnection) -> bool: ...


class HighlightNotFoundError(KeyError):
    """Raised when the highlight id is unknown OR not owned by the caller.

    Mirrors :class:`MomentNotFoundError` — same shape avoids leaking
    "exists but not yours" via 403 vs 404 split.
    """

    __slots__ = ()


class HighlightPublicationService(ProtectionGateMixin):
    """SH-side orchestrator for the publish / revoke / unpublish flow."""

    __slots__ = (
        "_child_protection",
        "_gfs",
        "_gfs_caps",
        "_highlights",
        "_http_client",
        "_own_instance_id",
        "_pinned_viewer",
        "_signing_key",
        "_viewer_base_url",
    )

    def __init__(
        self,
        highlight_repo: AbstractHighlightRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
        gfs_capabilities: SealedHighlightCapability | None = None,
        public_viewer_base_url: str = "",
        pinned_viewer: PinnedViewer | None = None,
    ) -> None:
        self._highlights = highlight_repo
        # Links open the pinned viewer (a page no GFS serves) when both are
        # set; otherwise the GFS-served page (see :meth:`_share_link`).
        self._viewer_base_url = public_viewer_base_url
        self._pinned_viewer = pinned_viewer
        self._gfs = gfs_repo
        self._gfs_caps = gfs_capabilities
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        self._child_protection = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        signing_key: bytes,
    ) -> None:
        """Late-bound identity wiring — same shape as
        :meth:`ReportService.attach_gfs`. Both inputs come online during
        ``app._on_startup`` once federation identity has loaded."""
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    # ── Public API ───────────────────────────────────────────────────────

    async def publish(
        self,
        highlight_id: str,
        author_user_id: str,
        *,
        gfs_id: str,
        label: str | None = None,
    ) -> dict:
        """Mint a fresh public-share token for ``highlight_id``.

        Returns ``{"token", "url", "label"}``. Idempotent across repeated
        calls — each call mints a *new* token (one per platform pattern),
        but the parent ``gfs_highlight_publications`` row is upserted, so
        the second call doesn't double-up that row.
        """
        # §CP.R: a protected account can't put a highlight on the open web.
        await self._require_unrestricted(
            author_user_id, ProtectedCapability.PUBLIC_LINKS
        )
        highlight = await self._require_owned(highlight_id, author_user_id)
        if not highlight.expires_at:
            # Should never happen — every highlight carries an expires_at —
            # but bail noisily rather than POSTing a meaningless cap.
            raise HighlightPublicationError(
                "Highlight has no expiry — refusing publish."
            )

        body = {
            "highlight_id": highlight.id,
            "instance_id": self._require_instance_id(),
            "expires_at": _expires_to_unix(highlight.expires_at),
            "label": label,
        }
        signed = self._sign_body(body)
        conn = await self._require_active_gfs(gfs_id)
        client = self._client()
        await self._require_sealed_viewer(conn)
        if highlight.public_gfs_id is not None and highlight.public_gfs_id != gfs_id:
            # One publication per highlight: the other GFS's links would
            # otherwise go dead here and come back on a later publish there.
            # Pull it (its tokens end here at once, whatever that GFS says).
            try:
                await self.unpublish(highlight.id, author_user_id)
            except HighlightPublicationError as exc:
                log.warning(
                    "highlight %s: pulling the publication on another GFS failed: %s",
                    highlight.id,
                    exc,
                )
        try:
            async with client.post(
                f"{conn.inbox_url}/gfs/highlights/{highlight.id}/publish",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 300:
                    raise HighlightPublicationError(
                        f"GFS rejected publish: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise HighlightPublicationError(
                f"GFS publish request failed: {exc}"
            ) from exc

        token = payload.get("token")
        url = str(payload.get("url") or "")
        if not isinstance(token, str) or not usable_token(token) or not url:
            raise HighlightPublicationError(
                "GFS publish response missing token / url, or token too long."
            )
        # Mark the highlight as published *after* the GFS confirms — a network
        # failure should leave the local flag clear so the SPA doesn't
        # show "published" for a publication that never landed.
        await self._highlights.mark_published(
            highlight.id,
            gfs_id=gfs_id,
            published_at=datetime.now(UTC).isoformat(),
        )
        # Only a token we minted (and haven't revoked) is ever streamed —
        # revocation is ours to enforce, not the GFS's.
        await self._highlights.add_public_token(
            highlight.id, gfs_id=gfs_id, token_hash=token_digest(token)
        )
        if self._signing_key is None:
            raise HighlightPublicationError("Instance identity not loaded.")
        # The link secret rides in the fragment, which browsers never send
        # to a server: the GFS that minted the token can't open the stream.
        secret = link_secret(self._signing_key, highlight_id=highlight.id, token=token)
        link = self._share_link(
            conn, gfs_url=url, highlight_id=highlight.id, token=token, secret=secret
        )
        return {"token": token, "url": link, "label": label}

    async def revoke_token(
        self,
        highlight_id: str,
        author_user_id: str,
        *,
        token: str,
    ) -> None:
        """Revoke a single share token.

        Other tokens under the same publication keep working; the parent
        row stays. To pull *all* tokens, call :meth:`unpublish`.
        """
        highlight = await self._require_owned(highlight_id, author_user_id)
        gfs_id = highlight.public_gfs_id
        if gfs_id is None:
            raise HighlightPublicationError("Highlight is not currently published.")
        # Ends the link here first: this household streams only for its own
        # live tokens, so the revoke holds even if the GFS call below fails.
        await self._highlights.remove_public_token(highlight.id, token_digest(token))
        conn = await self._require_active_gfs(gfs_id)

        body = {"token": token, "instance_id": self._require_instance_id()}
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/highlight_tokens/{token}/revoke",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    raise HighlightPublicationError(
                        f"GFS rejected revoke: HTTP {resp.status} {await resp.text()}"
                    )
        except aiohttp.ClientError as exc:
            raise HighlightPublicationError(
                f"GFS revoke request failed: {exc}"
            ) from exc

    async def unpublish(self, highlight_id: str, author_user_id: str) -> None:
        """Pull the publication entirely. CASCADE on the GFS side wipes
        every token under it; the local flag clears regardless of GFS
        response so a confused author can always recover the SPA state."""
        highlight = await self._require_owned(highlight_id, author_user_id)
        gfs_id = highlight.public_gfs_id
        if gfs_id is None:
            return  # Idempotent — already unpublished.
        try:
            # Inside the ``try``: an inactive or vanished GFS connection must
            # not leave the tokens live here (they would stream again if it
            # came back).
            conn = await self._require_active_gfs(gfs_id)
            body = {
                "highlight_id": highlight.id,
                "instance_id": self._require_instance_id(),
            }
            signed = self._sign_body(body)
            async with self._client().post(
                f"{conn.inbox_url}/gfs/highlights/{highlight.id}/unpublish",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    # 404 means the GFS row already vanished — fine.
                    raise HighlightPublicationError(
                        f"GFS rejected unpublish: HTTP {resp.status} "
                        f"{await resp.text()}"
                    )
        except aiohttp.ClientError as exc:
            # Log but still clear the local flag — author has explicitly
            # asked to unpublish; we honour that locally even if the GFS
            # is unreachable. The retention scheduler on the GFS side
            # will eventually prune the orphan row.
            log.warning("GFS unpublish request failed: %s", exc)
        finally:
            await self._highlights.mark_unpublished(highlight.id)

    async def on_account_protected(self, user_id: str) -> None:
        """§CP.R retroactive: pull every public link the account published.

        The local flag is what the streaming handler checks before serving,
        so it is cleared even when the connection server can't be reached.
        """
        for highlight in await self._highlights.list_published_for(user_id):
            try:
                await self.unpublish(highlight.id, user_id)
            except HighlightPublicationError as exc:
                log.warning(
                    "highlight %s: unpublish on protection failed: %s",
                    highlight.id,
                    exc,
                )
                await self._highlights.mark_unpublished(highlight.id)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _share_link(
        self,
        conn: GfsConnection,
        *,
        gfs_url: str,
        highlight_id: str,
        token: str,
        secret: bytes,
    ) -> str:
        """The link a viewer opens.

        Pinned (default): ``{public_viewer_base_url}/v/<hash>/#g=…&k=…`` —
        a static page no GFS serves, so no GFS can rewrite the script that
        reads the secret. ``g`` is the address this household itself talks
        to that GFS on (its pinned ``inbox_url``), never one the GFS's
        reply suggests.

        Fallback, less safe (the GFS serves the script): the GFS's own
        ``url`` plus ``#k``, when ``public_viewer_base_url`` is empty, the
        manifest is unusable, or the viewer would refuse that GFS address
        (plain http to a non-localhost host).
        """
        if self._viewer_base_url and self._pinned_viewer is not None:
            base = viewer_base_url(self._viewer_base_url)
            if base is None:
                log.warning(
                    "highlights.public_viewer_base_url %r is not an https base "
                    "a pinned link may name; minting the GFS-served link instead",
                    self._viewer_base_url,
                )
                return link_with_secret(gfs_url, secret)
            gfs_base = viewer_gfs_base(conn.inbox_url)
            if gfs_base is not None:
                return pinned_link(
                    base,
                    self._pinned_viewer,
                    gfs_base=gfs_base,
                    instance_id=self._require_instance_id(),
                    highlight_id=highlight_id,
                    token=token,
                    secret=secret,
                )
            log.warning(
                "GFS %s: address %r can't be named in a pinned viewer link "
                "(https only); minting the GFS-served link instead",
                conn.id,
                conn.inbox_url,
            )
        return link_with_secret(gfs_url, secret)

    async def _require_owned(
        self,
        highlight_id: str,
        author_user_id: str,
    ):
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None or highlight.author_user_id != author_user_id:
            raise HighlightNotFoundError(highlight_id)
        return highlight

    async def _require_active_gfs(self, gfs_id: str):
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            raise HighlightPublicationError(
                f"GFS connection {gfs_id!r} not paired or inactive.",
            )
        return conn

    async def _require_sealed_viewer(self, conn: GfsConnection) -> None:
        """Refuse a GFS whose viewer page can't open the sealed stream."""
        if self._gfs_caps is None:
            raise HighlightPublicationError(
                "HighlightPublicationService used before its GFS capability "
                "check was wired",
            )
        if not await self._gfs_caps.sealed_highlight_stream_supported(conn):
            raise GfsTooOldError(params={"feature": "public_highlight"})

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise HighlightPublicationError(
                "HighlightPublicationService used before attach_identity",
            )
        return self._own_instance_id

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise HighlightPublicationError(
                "HighlightPublicationService used before attach_session",
            )
        return self._http_client

    def _sign_body(self, body: dict) -> dict:
        if self._signing_key is None:
            raise HighlightPublicationError(
                "HighlightPublicationService used before attach_identity",
            )
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return signed


def _expires_to_unix(iso_or_unix: str) -> int:
    """Coerce ``highlights.expires_at`` (ISO-8601) into a unix epoch.

    GFS persists the cap as an integer epoch so its CHECK + index
    queries can use the same ``strftime('%s','now')`` clock the rest
    of the GFS schema does.
    """
    try:
        # Parse the ISO; Python 3.11+ accepts a trailing 'Z'.
        normalised = iso_or_unix.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalised).timestamp())
    except TypeError, ValueError:
        # Some legacy rows store unix epoch as a string.
        return int(iso_or_unix)

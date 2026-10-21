"""Public-Momentum service (§Momentum-public).

Author-facing CRUD on registrations and follows. Issues signed HTTP
requests to the GFS for register / unregister / follow / unfollow;
registers a local row so the inbox can render "via GFS" chips and so
the inbound WS handler can verify per-moment signatures locally.

The fan-out itself rides the persistent WS — see
:mod:`moment_public_outbound` (author-side push) and
:mod:`moment_public_inbound` (recipient-side verify + persist).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..crypto import b64url_encode, sign_ed25519
from ..domain.child_protection import ProtectedCapability
from ..domain.moment_public import (
    MomentPublicFollow,
    MomentPublicRegistration,
    public_author_bio,
    public_author_name,
    public_picture_digest,
)
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.moment_public_repo import (
    AbstractMomentPublicFollowRepo,
    AbstractMomentPublicRegistrationRepo,
)
from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
from ..repositories.user_repo import AbstractUserRepo
from .moment_public_key_inbound import OWN_MOMENT_SUITES
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from .gfs_connection_service import GfsConnectionService
    from .moment_public_key_inbound import MomentPublicKeyInbound
    from .moment_public_key_outbound import MomentPublicKeyOutbound

log = logging.getLogger(__name__)


class MomentPublicError(Exception):
    """Raised on any GFS round-trip failure. Mapped to HTTP 502."""

    __slots__ = ()


class MomentPublicService(ProtectionGateMixin):
    """Author/follower bookkeeping + GFS round-trips."""

    __slots__ = (
        "_capabilities",
        "_child_protection",
        "_follows",
        "_gfs",
        "_http_client",
        "_key_inbound",
        "_key_outbound",
        "_own_instance_id",
        "_pictures",
        "_regs",
        "_signing_key",
        "_suites_declared",
        "_users",
    )

    def __init__(
        self,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        follow_repo: AbstractMomentPublicFollowRepo,
        user_repo: AbstractUserRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._regs = registration_repo
        self._follows = follow_repo
        self._users = user_repo
        self._gfs = gfs_repo
        self._pictures = profile_picture_repo
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        self._child_protection = None
        #: Follower-side moment keys (v_62) — dropped for an author once
        #: nobody here follows them any more.
        self._key_inbound: MomentPublicKeyInbound | None = None
        #: Author-side moment keys (v_62) — a registration ending marks the
        #: author's key for rotation at the next publish.
        self._key_outbound: MomentPublicKeyOutbound | None = None
        #: The GFS capability proofs (v_62 ``encrypted_moments``) the
        #: sharing settings report per server.
        self._capabilities: GfsConnectionService | None = None
        #: GFS ids this process already declared its moment suites to.
        self._suites_declared: set[str] = set()

    def attach_capabilities(self, gfs_service: GfsConnectionService) -> None:
        self._capabilities = gfs_service

    async def encrypted_delivery(self, gfs_id: str) -> bool:
        """Whether this household delivers public moments through *gfs_id*:
        only to a server that proved ``encrypted_moments`` (v_62) — any
        other gets none, never plaintext. ``False`` until wired."""
        if self._capabilities is None:
            return False
        return await self._capabilities.encrypted_moments_supported(gfs_id)

    async def follower_households_unable(
        self, *, user_id: str, gfs_id: str
    ) -> int | None:
        """How many of *user_id*'s follower households on *gfs_id* can't
        open encrypted moments (an older Social Home — they are sent none),
        from the GFS's counts. ``None`` when the server can't say."""
        try:
            conn = await self._require_active_gfs(gfs_id)
            signed = self._sign_body({"instance_id": self._require_instance_id()})
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/{user_id}/follower_stats",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 300:
                    return None
                body = await resp.json()
        except (MomentPublicError, aiohttp.ClientError, ValueError) as exc:
            log.info("moment_public follower stats from %s: %s", gfs_id, exc)
            return None
        unable = body.get("households_unable") if isinstance(body, dict) else None
        return unable if isinstance(unable, int) and unable >= 0 else None

    async def declare_suites(self, gfs_id: str) -> bool:
        """Tell *gfs_id* what this household can open (v_62), once per
        process and only when a local user follows someone through it:
        follows made before the upgrade declared nothing, so the GFS sent
        them no public moments. One small signed request per connection;
        missing keys are then asked for when moments arrive. ``True`` when
        the GFS took it. Never raises."""
        if gfs_id in self._suites_declared or self._signing_key is None:
            return False
        if not await self._follows.any_through(gfs_id):
            return False
        if not await self.encrypted_delivery(gfs_id):
            return False
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active" or self._http_client is None:
            return False
        signed = self._sign_body(
            {
                "instance_id": self._require_instance_id(),
                "moment_suites": OWN_MOMENT_SUITES,
            }
        )
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}/gfs/moments/suites",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment_public: GFS %s refused our moment suites — HTTP %d",
                        gfs_id,
                        resp.status,
                    )
                    return False
        except aiohttp.ClientError as exc:
            log.warning("moment_public: declaring suites to %s failed: %s", gfs_id, exc)
            return False
        self._suites_declared.add(gfs_id)
        return True

    def attach_key_outbound(self, key_outbound: MomentPublicKeyOutbound) -> None:
        self._key_outbound = key_outbound

    def attach_key_inbound(self, key_inbound: MomentPublicKeyInbound) -> None:
        self._key_inbound = key_inbound

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    # ── Registrations ───────────────────────────────────────────────────

    async def register(
        self,
        *,
        user_id: str,
        gfs_id: str,
        default_share: bool = True,
    ) -> MomentPublicRegistration:
        # §CP.R: a protected account never lists itself in a connection
        # server's public directory.
        await self._require_unrestricted(user_id, ProtectedCapability.PUBLIC_MOMENTS)
        user = await self._users.get_by_user_id(user_id)
        if user is None:
            raise LookupError(f"user {user_id!r} not found")
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "user_id": user_id,
            "instance_id": self._require_instance_id(),
            "username": user.username,
            "display_name": user.display_name,
            "picture_url": _picture_url(user),
            "bio": user.bio,
            "home_instance_pk": _hex_signing_pubkey(self._require_signing_key()),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/register",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS rejected register: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS register failed: {exc}") from exc
        reg = await self._regs.upsert(
            user_id=user_id, gfs_id=gfs_id, default_share=default_share
        )
        # Push the avatar bytes when (a) the user has one and (b) we
        # haven't pushed this exact hash to this GFS yet. Best-effort —
        # a failure here doesn't roll back the registration.
        if user.picture_hash and user.picture_hash != reg.last_picture_digest:
            try:
                await self._push_picture(user_id=user_id, gfs_id=gfs_id, conn=conn)
            except MomentPublicError as exc:
                log.warning("moment_public picture push failed: %s", exc)
        return reg

    async def push_profile_to_gfs(self, *, user_id: str, gfs_id: str) -> None:
        """Re-register + (maybe) re-push avatar for an already-active reg.

        Used by :class:`ProfileSyncService` when a household profile
        changes. Errors propagate as :class:`MomentPublicError` so the
        sync scheduler can decide whether to retry.
        """
        await self.register(user_id=user_id, gfs_id=gfs_id)

    async def deregister(self, *, user_id: str, gfs_id: str) -> None:
        conn = await self._require_active_gfs(gfs_id)
        body = {"user_id": user_id, "instance_id": self._require_instance_id()}
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/{user_id}/deregister",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    raise MomentPublicError(
                        f"GFS rejected deregister: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment_public deregister: %s", exc)
        finally:
            await self._regs.delete(user_id=user_id, gfs_id=gfs_id)
            # Followers on that server keep the key they hold; a later
            # registration (here or anywhere) must not let them read on.
            if self._key_outbound is not None:
                await self._key_outbound.mark_rotation_due(
                    user_id, reason="a registration ended"
                )

    async def list_registrations(self, user_id: str) -> list[MomentPublicRegistration]:
        return await self._regs.list_for_user(user_id)

    async def set_default_share(
        self, *, user_id: str, gfs_id: str, default_share: bool
    ) -> None:
        await self._regs.set_default_share(
            user_id=user_id, gfs_id=gfs_id, default_share=default_share
        )

    async def is_registered(self, *, user_id: str, gfs_id: str) -> bool:
        got = await self._regs.get(user_id=user_id, gfs_id=gfs_id)
        return got is not None

    async def default_share(self, *, user_id: str, gfs_id: str) -> bool:
        got = await self._regs.get(user_id=user_id, gfs_id=gfs_id)
        return bool(got and got.default_share)

    # ── Follows ─────────────────────────────────────────────────────────

    async def follow(
        self,
        *,
        follower_user_id: str,
        gfs_id: str,
        followed_user_id: str,
    ) -> MomentPublicFollow:
        # §CP.R: nor announces itself to a stranger as a follower.
        await self._require_unrestricted(
            follower_user_id, ProtectedCapability.PUBLIC_MOMENTS
        )
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "follower_user_id": follower_user_id,
            "follower_instance_id": self._require_instance_id(),
            # The GFS authenticates every signed body by its ``instance_id``
            # (``_rtc_authenticate``) — without it the follow is refused 422.
            "instance_id": self._require_instance_id(),
            "followed_user_id": followed_user_id,
            # v_62: what this household can open — an author seals its
            # moment key only to a household that declared it.
            "moment_suites": OWN_MOMENT_SUITES,
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/{followed_user_id}/follow",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS rejected follow: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS follow failed: {exc}") from exc

        directory = payload.get("user") or {}
        # The directory names an author without a name by their id — an
        # id is never a name, so it is stored as unknown ("").
        name = public_author_name(
            directory.get("display_name"), user_id=followed_user_id
        )
        return await self._follows.upsert(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
            gfs_id=gfs_id,
            followed_instance_pk=str(directory.get("home_instance_pk", "")),
            followed_username=str(directory.get("username", "")),
            followed_display_name=name or "",
            followed_bio=public_author_bio(directory.get("bio")),
            followed_picture_digest=public_picture_digest(
                directory.get("picture_digest")
            ),
        )

    async def unfollow(
        self,
        *,
        follower_user_id: str,
        gfs_id: str,
        followed_user_id: str,
    ) -> None:
        conn = await self._require_active_gfs(gfs_id)
        body = {
            "follower_user_id": follower_user_id,
            "follower_instance_id": self._require_instance_id(),
            # The GFS authenticates every signed body by its ``instance_id``
            # (``_rtc_authenticate``) — without it the follow is refused 422.
            "instance_id": self._require_instance_id(),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/{followed_user_id}/unfollow",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300 and resp.status != 404:
                    raise MomentPublicError(
                        f"GFS rejected unfollow: HTTP {resp.status}"
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment_public unfollow: %s", exc)
        finally:
            await self._follows.delete(
                follower_user_id=follower_user_id,
                followed_user_id=followed_user_id,
                gfs_id=gfs_id,
            )
            if self._key_inbound is not None:
                await self._key_inbound.forget_if_unfollowed(followed_user_id)

    async def list_follows(self, follower_user_id: str) -> list[MomentPublicFollow]:
        return await self._follows.list_for_follower(follower_user_id)

    async def on_account_protected(self, user_id: str) -> None:
        """§CP.R retroactive: leave every public directory and drop every
        public follow. The local rows go even when a connection server
        can't be reached (nothing is published or pulled without them)."""
        for reg in await self._regs.list_for_user(user_id):
            try:
                await self.deregister(user_id=user_id, gfs_id=reg.gfs_id)
            except MomentPublicError as exc:
                log.warning("moment_public deregister on protection: %s", exc)
                await self._regs.delete(user_id=user_id, gfs_id=reg.gfs_id)
        for follow in await self._follows.list_for_follower(user_id):
            try:
                await self.unfollow(
                    follower_user_id=user_id,
                    gfs_id=follow.gfs_id,
                    followed_user_id=follow.followed_user_id,
                )
            except MomentPublicError as exc:
                log.warning("moment_public unfollow on protection: %s", exc)
                await self._follows.delete(
                    follower_user_id=user_id,
                    followed_user_id=follow.followed_user_id,
                    gfs_id=follow.gfs_id,
                )

    async def fetch_directory(self, gfs_id: str, *, q: str | None = None) -> list[dict]:
        """Fetch the GFS public-user directory (anon GET).

        Optional ``q`` substring filter on display_name + username; the
        server-side filter is delegated to the GFS so big directories
        don't ship in full.
        """
        conn = await self._require_active_gfs(gfs_id)
        url = f"{conn.inbox_url}/gfs/moments/users"
        if q:
            url = f"{url}?q={q}"
        try:
            async with self._client().get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS directory fetch failed: HTTP {resp.status}"
                    )
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS directory request failed: {exc}") from exc
        users = payload.get("users")
        if not isinstance(users, list):
            return []
        return users

    async def fetch_picture(
        self, gfs_id: str, user_id: str
    ) -> tuple[bytes, str, str] | None:
        """Fetch GFS-mirrored avatar bytes for *user_id* on *gfs_id*.

        Returns ``(bytes, mime, digest)`` or ``None`` when the GFS
        replies 404. Used by the SH-side picture-proxy route to serve
        avatars in the Discover UI without the browser needing to know
        the GFS host.
        """
        conn = await self._require_active_gfs(gfs_id)
        try:
            async with self._client().get(
                f"{conn.inbox_url}/gfs/moments/users/{user_id}/picture",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 300:
                    raise MomentPublicError(
                        f"GFS picture fetch failed: HTTP {resp.status}"
                    )
                raw = await resp.read()
                mime = resp.headers.get("Content-Type", "application/octet-stream")
                etag = resp.headers.get("ETag", '""').strip('"')
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS picture request failed: {exc}") from exc
        return raw, mime, etag

    # ── Internal ────────────────────────────────────────────────────────

    async def _push_picture(self, *, user_id: str, gfs_id: str, conn) -> None:
        if self._pictures is None:
            return
        got = await self._pictures.get_user_picture(user_id)
        if got is None:
            return
        bytes_webp, picture_hash = got
        body = {
            "user_id": user_id,
            "instance_id": self._require_instance_id(),
            "mime": "image/webp",
            "bytes_b64": base64.b64encode(bytes_webp).decode("ascii"),
        }
        signed = self._sign_body(body)
        try:
            async with self._client().post(
                f"{conn.inbox_url}/gfs/moments/users/{user_id}/picture",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    raise MomentPublicError(f"GFS picture upload: HTTP {resp.status}")
        except aiohttp.ClientError as exc:
            raise MomentPublicError(f"GFS picture request failed: {exc}") from exc
        await self._regs.set_last_picture_digest(
            user_id=user_id, gfs_id=gfs_id, digest=picture_hash
        )

    async def _require_active_gfs(self, gfs_id: str):
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            raise MomentPublicError(
                f"GFS connection {gfs_id!r} not paired or inactive."
            )
        return conn

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise MomentPublicError("MomentPublicService used before attach_identity")
        return self._own_instance_id

    def _require_signing_key(self) -> bytes:
        if self._signing_key is None:
            raise MomentPublicError("MomentPublicService used before attach_identity")
        return self._signing_key

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise MomentPublicError("MomentPublicService used before attach_session")
        return self._http_client

    def _sign_body(self, body: dict) -> dict:
        signing_key = self._require_signing_key()
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))
        return signed


def _picture_url(user) -> str | None:
    """Best-effort canonical picture URL for the directory listing."""
    pic_hash = getattr(user, "picture_hash", None)
    user_id = getattr(user, "user_id", None)
    if pic_hash and user_id:
        return f"api/users/{user_id}/picture?v={pic_hash}"
    return None


def _hex_signing_pubkey(signing_key: bytes) -> str:
    """Derive the Ed25519 public key (hex) from the seed.

    The federation identity already exposes ``own_public_key`` on the
    encoder; we keep this helper local so the service doesn't have to
    pull a second dependency just to advertise the verifier key in the
    register payload.
    """
    sk = Ed25519PrivateKey.from_private_bytes(signing_key[:32])
    pub_bytes = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return pub_bytes.hex()


# pyright: reportUnusedImport=false
_ = datetime, timezone  # kept for forward-compat helpers

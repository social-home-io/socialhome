"""Author-side fan-out for public Momentum (§Momentum-public).

Bus subscriber on :class:`MomentCreated` and :class:`MomentDeleted`.
For each event whose author is local on this instance AND has at
least one :class:`MomentPublicRegistration`, build a signed, ENCRYPTED
envelope and POST it to every registered GFS so the broker can fan it
out to each follower's instance.

**Encrypted from the GFS (v_62).** Everything but the routing fields —
the text, the media reference and the author's profile — is sealed under
the moment's item key (derived from the author's current step key,
:class:`~socialhome.services.moment_public_key_outbound
.MomentPublicKeyOutbound`, :mod:`socialhome.moment_public_seal`); the GFS
sees ``{author_user_id, instance_id, moment_id, key_gen, key_step,
seal_suite, ciphertext, signature}`` and nothing else. A moment is sealed
and signed ONCE and the same bytes go to every registered GFS, which fans
the same ciphertext out to every follower household — nothing per
follower. The author's profile (name, username, bio, picture digest) rides
inside only when it changed since the author's last moment (or once a
day); every moment carries its ``profile_rev``. A GFS that has not proved the
signed ``encrypted_moments`` capability gets **no public moments at
all** — never a plaintext fallback — and the author's sharing settings
say so (``GET /api/moments/public/registrations`` → ``delivery``).

Runs alongside :class:`MomentFederationOutbound`: the original
moment also still goes to paired households via the existing relay,
so a follower that is *both* household-paired and a GFS-follower
receives the same moment twice. Receivers dedupe by ``moment.id``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from ..domain.child_protection import ProtectedCapability
from ..domain.events import (
    MediaTranscodeFailed,
    MediaTranscodeReady,
    MomentCreated,
    MomentDeleted,
    UserDeprovisioned,
    UserProvisioned,
)
from ..domain.moment_public import public_author_bio, public_profile_rev
from ..infrastructure.event_bus import EventBus
from ..moment_public_seal import item_key, seal_body, sign_wire
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicRegistrationRepo,
    )
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_connection_service import GfsConnectionService
    from .moment_public_key_outbound import MomentPublicKeyOutbound
    from .moment_public_media_outbound import MomentPublicMediaOutbound
    from .moment_public_share_service import MomentPublicShareService

log = logging.getLogger(__name__)

#: A remote author's moment re-reads the local user list at most this often.
LOCAL_USERS_REREAD_S: float = 60.0

#: Longest a moment's media blob is kept on a GFS (a moment lives ≤ 7 days).
MEDIA_KEEP_AT_MOST_S: int = 7 * 86_400

#: Public moments held for a video still transcoding, at most.
MAX_WAITING_FOR_MEDIA: int = 200

#: The author's profile rides a moment at least this often even unchanged,
#: so a follower that missed a change catches up within a day.
PROFILE_RESEND_S: float = 86_400.0


class MomentPublicOutbound(ProtectionGateMixin):
    __slots__ = (
        "_bus",
        "_child_protection",
        "_gfs",
        "_gfs_service",
        "_http_client",
        "_keys",
        "_media",
        "_moments",
        "_own_instance_id",
        "_own_users",
        "_own_users_read_at",
        "_profile_sent",
        "_regs",
        "_shares",
        "_signing_key",
        "_unsupported_warned",
        "_users",
        "_waiting_media",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        moment_repo: AbstractMomentRepo,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        user_repo: AbstractUserRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        key_outbound: MomentPublicKeyOutbound,
        gfs_service: GfsConnectionService,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._bus = bus
        self._keys = key_outbound
        self._gfs_service = gfs_service
        #: GFS ids already warned about (no ``encrypted_moments``) — one
        #: WARNING per server per process.
        self._unsupported_warned: set[str] = set()
        self._moments = moment_repo
        self._regs = registration_repo
        self._users = user_repo
        self._gfs = gfs_repo
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        # Cached set of locally-hosted user_ids, re-read on a miss (a user
        # added since) so we don't fan out for events whose author lives
        # on another instance.
        self._own_users: set[str] | None = None
        #: Monotonic time of the last full read of local users — a miss
        #: (a remote author's moment) re-reads at most once a minute.
        self._own_users_read_at: float = 0.0
        #: author → (profile_rev, monotonic time) last sent inside a moment.
        self._profile_sent: dict[str, tuple[str, float]] = {}
        self._child_protection = None
        #: Encrypted media (v_63). Unwired: moments go without their file.
        self._media: MomentPublicMediaOutbound | None = None
        #: Per-moment share links. Unwired: no link material kept.
        self._shares: MomentPublicShareService | None = None
        #: media file name → the moment waiting for its transcode.
        self._waiting_media: dict[str, MomentCreated] = {}

    def attach_shares(self, shares: MomentPublicShareService) -> None:
        self._shares = shares

    def attach_media(self, media: MomentPublicMediaOutbound) -> None:
        self._media = media

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    def wire(self) -> None:
        self._bus.subscribe(MomentCreated, self._on_created)
        self._bus.subscribe(MomentDeleted, self._on_deleted)
        self._bus.subscribe(UserProvisioned, self._on_user_provisioned)
        self._bus.subscribe(UserDeprovisioned, self._on_user_deprovisioned)
        self._bus.subscribe(MediaTranscodeReady, self._on_transcode_ready)
        self._bus.subscribe(MediaTranscodeFailed, self._on_transcode_failed)

    async def _on_transcode_ready(self, event: MediaTranscodeReady) -> None:
        waiting = self._waiting_media.pop(event.output_filename, None)
        if waiting is not None:
            await self._on_created(waiting)

    async def _on_transcode_failed(self, event: MediaTranscodeFailed) -> None:
        waiting = self._waiting_media.pop(event.output_filename, None)
        if waiting is not None:
            await self._on_created(waiting, without_media=True)

    async def _on_user_provisioned(self, event: UserProvisioned) -> None:
        if self._own_users is not None:
            self._own_users.add(event.user_id)

    async def _on_user_deprovisioned(self, event: UserDeprovisioned) -> None:
        if self._own_users is not None:
            self._own_users.discard(event.user_id)

    # ── Subscribers ─────────────────────────────────────────────────────

    async def _on_created(
        self, event: MomentCreated, *, without_media: bool = False
    ) -> None:
        if not await self._is_local(event.author_user_id):
            return
        moment = await self._moments.get(event.moment_id)
        if moment is None or not moment.is_public:
            return
        # GFS-received moments are NEVER re-fanned (no-redistribute rule).
        if moment.received_via == "gfs":
            return
        regs = await self._regs.list_for_user(event.author_user_id)
        if not regs:
            return
        # §CP.R at use time: a registration made before the account was
        # protected must not keep publishing its moments to strangers.
        # Deletes (below) still flow so earlier moments can be retracted.
        if await self._is_restricted(
            event.author_user_id, ProtectedCapability.PUBLIC_MOMENTS
        ):
            log.info(
                "public moment %s not published: author account is protected",
                event.moment_id,
            )
            return
        author = await self._users.get_by_user_id(event.author_user_id)
        if author is None:
            return
        regs = [r for r in regs if await self._delivers_to(r.gfs_id)]
        if not regs:
            return
        gen, step, key = await self._keys.publish_key(event.author_user_id)
        # v_63: the file sealed ONCE under a key derived from the moment's
        # item key; the same blob goes to every GFS. The author's local
        # ``api/media/…`` reference never travels — it names nothing on a
        # follower's side.
        prepared = None
        if self._media is not None and moment.media_url and not without_media:
            prepared = await self._media.prepare(
                moment, item_key=item_key(key, moment.id)
            )
            if prepared is not None and not prepared.ready:
                self._hold_for_media(moment.media_url, event)
                return
        has_media = prepared is not None
        if self._shares is not None:
            # What a per-moment link needs later (the item key is gone from
            # the ratchet by tomorrow).
            await self._shares.record(
                moment,
                item_key=item_key(key, moment.id),
                media=prepared.body if prepared is not None else None,
            )
        # Sealed under the moment's item key — the GFS reads none of it.
        body: dict[str, Any] = {
            "op": "create",
            "author_user_id": moment.author_user_id,
            "content": moment.content,
            "media_type": moment.media_type if has_media else None,
            "duration_ms": moment.duration_ms if has_media else None,
            "parent_moment_id": moment.parent_moment_id,
            "origin_instance_id": moment.origin_instance_id,
            "created_at": moment.created_at,
            "expires_at": moment.expires_at,
        }
        # One ciphertext per picture the servers mirror — normally exactly
        # one for every GFS. A follower's picture proxy fetches by digest
        # from its own GFS, so a GFS that doesn't mirror the current picture
        # is told none (a removed or not yet pushed picture is not shown).
        variants: dict[str | None, list[str]] = {}
        for reg in regs:
            digest = (
                author.picture_hash
                if author.picture_hash
                and author.picture_hash == reg.last_picture_digest
                else None
            )
            variants.setdefault(digest, []).append(reg.gfs_id)
        if prepared is not None:
            body["media"] = prepared.body
            assert self._media is not None
            # A blob lives as long as its moment — never past the longest a
            # moment lives (a GFS refuses more).
            expires = min(
                _unix(moment.expires_at), int(time.time()) + MEDIA_KEEP_AT_MOST_S
            )
            for reg in regs:
                # Uploaded before the moment, so followers can fetch at once.
                if await self._media.upload(
                    reg.gfs_id,
                    prepared,
                    author_user_id=event.author_user_id,
                    expires_at=expires,
                ):
                    self._media.remember(
                        event.author_user_id, moment.id, reg.gfs_id, prepared
                    )
        sent_rev: str | None = None
        for digest, gfs_ids in variants.items():
            profile = {
                "display_name": author.display_name,
                "username": author.username,
                # The bio the author published in the GFS directory — never
                # longer than a directory holds.
                "bio": public_author_bio(author.bio),
                "picture_digest": digest,
            }
            rev = public_profile_rev(**profile)
            sealed = {**body, "profile_rev": rev}
            if self._profile_due(event.author_user_id, rev):
                sealed["profile"] = profile
                sent_rev = rev
            envelope = self._envelope(
                event.author_user_id, moment.id, gen, step, key, sealed
            )
            for gfs_id in gfs_ids:
                await self._post_to_gfs(gfs_id, envelope, kind="publish")
        if sent_rev is not None:
            self._profile_sent[event.author_user_id] = (sent_rev, time.monotonic())

    async def _on_deleted(self, event: MomentDeleted) -> None:
        if not await self._is_local(event.author_user_id):
            return
        regs = await self._regs.list_for_user(event.author_user_id)
        regs = [r for r in regs if await self._delivers_to(r.gfs_id)]
        if not regs:
            return
        gen, step, key = await self._keys.publish_key(event.author_user_id)
        envelope = self._envelope(
            event.author_user_id,
            event.moment_id,
            gen,
            step,
            key,
            {
                "op": "delete",
                "author_user_id": event.author_user_id,
                "origin_instance_id": self._own_instance_id,
            },
        )
        for reg in regs:
            await self._post_to_gfs(reg.gfs_id, envelope, kind="delete")
        if self._media is not None:
            await self._media.withdraw(event.author_user_id, event.moment_id)

    def _hold_for_media(self, media_url: str, event: MomentCreated) -> None:
        """Publish *event* once its video's transcode finishes (bounded)."""
        name = media_url.rsplit("/", 1)[-1]
        if len(self._waiting_media) >= MAX_WAITING_FOR_MEDIA:
            self._waiting_media.pop(next(iter(self._waiting_media)))
        self._waiting_media[name] = event
        log.info(
            "moment_public.outbound: moment %s waits for its video to finish",
            event.moment_id,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _is_local(self, user_id: str) -> bool:
        """Whether *user_id* lives on this household. The set is read once
        and kept current from ``UserProvisioned`` / ``UserDeprovisioned``; a
        miss (every remote author's moment) re-reads at most once a minute,
        for a user added by a path that publishes no event."""
        if self._own_users is not None:
            if user_id in self._own_users:
                return True
            if time.monotonic() - self._own_users_read_at < LOCAL_USERS_REREAD_S:
                return False
        users = await self._users.list_all()
        self._own_users = {u.user_id for u in users}
        self._own_users_read_at = time.monotonic()
        return user_id in self._own_users

    def _profile_due(self, author_user_id: str, rev: str) -> bool:
        """Send the profile inside this moment: it changed since the last
        moment that carried it, or that was a day ago."""
        last = self._profile_sent.get(author_user_id)
        return (
            last is None
            or last[0] != rev
            or time.monotonic() - last[1] >= PROFILE_RESEND_S
        )

    async def _delivers_to(self, gfs_id: str) -> bool:
        """Only a GFS that proved ``encrypted_moments`` gets public moments.
        Any other server gets nothing (no plaintext fallback), with one
        WARNING."""
        if await self._gfs_service.encrypted_moments_supported(gfs_id):
            return True
        if gfs_id not in self._unsupported_warned:
            self._unsupported_warned.add(gfs_id)
            log.warning(
                "moment_public.outbound: GFS %s has not proved encrypted_moments "
                "— public moments are NOT delivered through it (never in "
                "plaintext) until it upgrades",
                gfs_id,
            )
        return False

    def _envelope(
        self,
        author_user_id: str,
        moment_id: str,
        gen: int,
        step: int,
        key: bytes,
        body: dict,
    ) -> dict:
        if self._signing_key is None:
            raise RuntimeError("MomentPublicOutbound used before attach_identity")
        return sign_wire(
            self._signing_key,
            {
                "author_user_id": author_user_id,
                # ``instance_id`` is the field the GFS-side
                # ``_rtc_authenticate`` middleware reads to look up the
                # sender's pubkey.
                "instance_id": self._own_instance_id,
                **seal_body(
                    step_key=key,
                    author_user_id=author_user_id,
                    key_gen=gen,
                    key_step=step,
                    moment_id=moment_id,
                    body=body,
                ),
            },
        )

    async def _post_to_gfs(self, gfs_id: str, signed: dict, *, kind: str) -> None:
        if self._http_client is None or self._signing_key is None:
            return
        conn = await self._gfs.get(gfs_id)
        if conn is None or conn.status != "active":
            return
        path = "/gfs/moments/publish" if kind == "publish" else "/gfs/moments/delete"
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}{path}",
                allow_redirects=False,
                json=signed,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment_public.outbound: GFS %s rejected %s — HTTP %d",
                        gfs_id,
                        kind,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning(
                "moment_public.outbound: GFS %s %s failed: %s",
                gfs_id,
                kind,
                exc,
            )


def _unix(iso: str) -> int:
    """Unix seconds of an ISO 8601 time (``Z`` or an offset; naive = UTC)."""
    when = datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp())

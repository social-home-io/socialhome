"""Owner-minted space invite links — the connection server's bulletin board.

An owner mints an invite for a space that is already listed in this server's
public directory. A visitor opens the link in a BROWSER, sees the space name
this server already publishes on ``/spaces/{id}``, and copies a
``socialhome://invite#<blob>`` code into their own Social Home — which
redeems it through the opaque envelope relay (:mod:`.envelope_relay`).

The server's whole job is to hold a string and hand it back:

* **It never parses the blob.** Only its SIZE and ALPHABET are checked. The
  blob is base64url of a JSON payload the household composes and the SPA's
  ``client/src/lib/spaceInviteCode.ts`` decodes; growing a field in it must
  never require redeploying a connection server.
* **It never learns who redeemed.** ``GET /join/{token}`` writes NOTHING —
  no use counter, no fetch log, no per-visitor row. The ``uses`` /
  ``max_uses`` columns migration 0001 shipped stay unused forever; see
  ``migrations/0012_gfs_invite_tokens_blob.sql``. Whether an invite may still
  be redeemed is decided by the issuing household, the only party that can
  decide it without building a record of who joined what.
* **The blob must never carry a household address.** It is served to anyone
  holding the link. Addresses are exactly what the invite bootstrap exists to
  keep out of a stranger's hands — the redeem travels by instance id through
  ``POST /gfs/envelope``.

Mint and revoke are signed by the owning household's registered instance key
(:meth:`.federation.GfsFederationService.verify_signed_request`), with an
``action`` discriminator inside the signed bytes so a mint signature can never
be replayed as a revoke, or the reverse.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import GfsInviteToken
    from .federation import GfsFederationService
    from .public import SlidingWindowCounter
    from .repositories import AbstractGfsInviteRepo

log = logging.getLogger(__name__)


#: URI scheme + fragment separator of the code a visitor copies. Byte-for-byte
#: the ``URI_PREFIX`` ``client/src/lib/spaceInviteCode.ts`` decodes; the blob
#: rides in the FRAGMENT so a stray paste into a browser address bar never
#: sends it to anyone's server logs.
INVITE_CODE_PREFIX: str = "socialhome://invite#"

#: Hard cap on the opaque blob, in bytes of its base64url text.
#:
#: Sized from the fields the household packs today: the invite token (32 hex),
#: the space id (~40), a display hint capped at 64 chars, the issuer instance
#: id (32), the issuer identity and key-wrap public keys (64 hex each), the
#: base64url key-wrap binding signature (86), a protocol version, an ISO-8601
#: expiry (~25) and this server's base URL (~100). With JSON framing that is
#: ~600 bytes, ~800 once base64url'd. 4 KiB is therefore ~5x headroom — room
#: for the Phase-2 PQ suites to concatenate ML-KEM / ML-DSA material into the
#: same fields without a server redeploy — while keeping one row's cost, and
#: one anonymous ``/join`` render's cost, trivial.
INVITE_BLOB_MAX_BYTES: int = 4096

#: Longest life an invite may be minted with, in seconds.
#:
#: 30 days. An invite link is pasted into a chat and forgotten there, so it
#: outlives the conversation; a month covers "I'll get to it next weekend"
#: twice over. Past that the link is likelier to be a stale artefact in
#: someone's message history than an intention — and every extra day is
#: another day this server holds a string on behalf of a household that has
#: moved on. The issuing household may always mint a fresh one.
INVITE_MAX_TTL_SECONDS: int = 30 * 24 * 60 * 60

#: Per-INSTANCE mint cap per minute. Unlike ``/gfs/publish`` and
#: ``/gfs/envelope``, the mint is signature-authenticated, so the accountable
#: identity is known before anything is written and the limiter keys on the
#: household rather than on an address it can rotate. 20/min is far above a
#: human minting invites (one per person invited) while bounding how many rows
#: one household can park here per minute. Like every limiter on this server
#: it sheds ONE noisy source; it is not DDoS protection.
INVITE_MINT_MAX_PER_MINUTE: int = 20


class InvalidInvite(ValueError):
    """The mint request is malformed (bad blob, bad expiry). Maps to 400."""


class InviteRateLimited(Exception):
    """This household has minted too many invites this minute. Maps to 429."""


#: base64url alphabet, with optional ``=`` padding. The ONLY thing checked
#: about the blob besides its size: this server must not be able to tell a
#: well-formed invite payload from a garbage one, because being able to would
#: mean it had parsed it.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def validate_blob(value: object) -> str:
    """Return *value* as an accepted opaque blob, or raise :class:`InvalidInvite`.

    SIZE and ALPHABET only — never structure. The alphabet check is not an
    attempt to understand the payload; it keeps a raw byte string, a JSON
    object or an HTML fragment out of a column that is rendered into a public
    page and copied by humans.
    """
    if not isinstance(value, str) or not value:
        raise InvalidInvite("invalid field: blob")
    if len(value.encode("utf-8")) > INVITE_BLOB_MAX_BYTES:
        raise InvalidInvite(f"blob exceeds {INVITE_BLOB_MAX_BYTES} bytes")
    if not _B64URL_RE.match(value):
        raise InvalidInvite("blob is not base64url text")
    return value


def validate_expires_at(value: object, *, now: int) -> int:
    """Return *value* as a bounded absolute expiry (unix seconds).

    Required — an invite with no end is a permanent public row this server
    would carry until the space is delisted. Must be in the future and at most
    :data:`INVITE_MAX_TTL_SECONDS` out.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInvite("invalid field: expires_at")
    if value <= now:
        raise InvalidInvite("expires_at is already in the past")
    if value - now > INVITE_MAX_TTL_SECONDS:
        raise InvalidInvite(
            f"expires_at is more than {INVITE_MAX_TTL_SECONDS} seconds out",
        )
    return value


def build_invite_code(blob: str) -> str:
    """The exact string a visitor copies: ``socialhome://invite#<blob>``."""
    return f"{INVITE_CODE_PREFIX}{blob}"


class GfsInviteService:
    """Mint / revoke / look up invite rows.

    Every mutation is authenticated as the space's ``owning_instance``: a
    signature proves only WHICH registered household is calling, so the owner
    check is separate and mandatory — space ids travel inside discovery links,
    and without it any paired household that saw one could park a blob on
    someone else's space page.
    """

    __slots__ = ("_fed", "_mint_limiter", "_repo")

    def __init__(
        self,
        *,
        federation: "GfsFederationService",
        invite_repo: "AbstractGfsInviteRepo",
        mint_limiter: "SlidingWindowCounter",
    ) -> None:
        # The limiter is INJECTED rather than built here: the public pages
        # module owns :class:`~.public.SlidingWindowCounter` and imports this
        # module for the invite-code scheme, so constructing one here would
        # close an import cycle. ``server.py`` wires the same window every
        # other limiter on this server uses.
        self._fed = federation
        self._repo = invite_repo
        self._mint_limiter = mint_limiter

    async def mint(
        self,
        *,
        space_id: str,
        owning_instance: str,
        blob: object,
        expires_at: object,
        ts: str,
        signature: str,
    ) -> "GfsInviteToken":
        """Park *blob* on *space_id*'s invite board and return the row.

        Order matters and is fail-closed: the signature is verified FIRST
        (against the registered instance key, over the canonical
        ``{action: "mint_invite", owning_instance, space_id, ts}`` JSON,
        replay-guarded ±300 s), then the rate limit, then ownership and the
        space's listing state, and only then is anything written. Verifying
        first means an unsigned caller can never learn from a 400-vs-403
        whether a space exists here.

        Raises :class:`PermissionError` (unknown instance, bad / stale
        signature, not the owner, space not actively listed),
        :class:`InviteRateLimited`, or :class:`InvalidInvite`.
        """
        await self._fed.verify_signed_request(
            owning_instance,
            {
                "action": "mint_invite",
                "owning_instance": owning_instance,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )
        if not self._mint_limiter.allow(owning_instance):
            raise InviteRateLimited("too many invites minted; retry shortly")
        await self._assert_listed_owner(space_id, owning_instance)

        now = int(time.time())
        checked_blob = validate_blob(blob)
        checked_expiry = validate_expires_at(expires_at, now=now)
        # 24 bytes of entropy → a 32-char URL-safe token. The link is public
        # and unguessable is its only protection, so this is sized like a
        # bearer credential rather than like a database id.
        gfs_token = secrets.token_urlsafe(24)
        row = await self._repo.create(
            gfs_token=gfs_token,
            space_id=space_id,
            source_instance_id=owning_instance,
            blob=checked_blob,
            created_at=now,
            expires_at=checked_expiry,
        )
        # Space id + household id only. NEVER the token (it is the credential
        # the link carries) and never the blob.
        log.info("GFS: %s minted an invite for space %s", owning_instance, space_id)
        return row

    async def revoke(
        self,
        *,
        space_id: str,
        gfs_token: str,
        owning_instance: str,
        ts: str,
        signature: str,
    ) -> None:
        """Remove one invite at its owner's signed request. Idempotent.

        The signed bytes are ``{action: "revoke_invite", gfs_token,
        owning_instance, space_id, ts}`` — both the ``action`` and the token
        are inside them, so a captured mint signature can never be replayed as
        a revoke (nor the reverse), and a revoke for one token can't be
        replayed against another.

        An unknown / already-revoked token is a silent success — but only
        AFTER the signature verifies and the caller is confirmed as the owner,
        so token existence is never leaked to an unauthenticated prober.
        """
        await self._fed.verify_signed_request(
            owning_instance,
            {
                "action": "revoke_invite",
                "gfs_token": gfs_token,
                "owning_instance": owning_instance,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )
        space = await self._fed.get_space(space_id)
        if space is None or space.owning_instance != owning_instance:
            raise PermissionError("not the owner of this space")
        await self._repo.delete(gfs_token)
        log.info("GFS: %s revoked an invite for space %s", owning_instance, space_id)

    async def get_live(self, gfs_token: str) -> "GfsInviteToken | None":
        """The unexpired invite row for *gfs_token*, or ``None``.

        A pure read: the public page calls this and NOTHING else, so a fetch
        leaves no trace in the database at all.
        """
        return await self._repo.get_live(gfs_token, now=int(time.time()))

    async def _assert_listed_owner(self, space_id: str, owning_instance: str) -> None:
        """Fail closed unless *owning_instance* owns an actively-listed space.

        An invite page shows the space's public directory metadata, so a space
        that is not in the directory — never published, moderator-banned, or
        owner-withdrawn — must not get one. Otherwise a withdrawn listing
        would keep a working public side door.
        """
        space = await self._fed.get_space(space_id)
        if space is None or space.owning_instance != owning_instance:
            raise PermissionError("not the owner of this space")
        if space.status != "active" or space.withdrawn:
            raise PermissionError("space is not publicly listed")

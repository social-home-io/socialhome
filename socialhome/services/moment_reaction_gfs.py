"""Reactions reach authors you follow through a GFS — follower side (v_68).

Owner decision 9, sign-off 1 (2026-10-25), GFS part. A public moment that
came through a GFS has no path back to its author's household: the author
and the follower are not paired, and households met through a GFS never talk
directly. This is the transport a reaction to such a moment takes — the
reaction itself is the v_67 sealed reaction
(:mod:`socialhome.federation.moment_reaction_route`), built, signed and
sealed by :class:`~.moment_reaction_router.MomentReactionRouter`; only how
it travels differs:

* **Where to.** :meth:`MomentReactionGfs.target` — the moment arrived on a
  GFS (``received_via_gfs_id``) whose signed ``/gfs/info`` proves
  ``moment_reactions``, and the author's household named its key-wrap key in
  a signed envelope (kept on the follow row,
  ``moment_public_follows.followed_keywrap_pk``, migration 0126). Only that
  GFS: never another one, never the author's household directly.
* **The post.** ``POST /gfs/moments/users/{author}/react`` with exactly
  ``{instance_id, sealed}``, signed by this household
  (:func:`~socialhome.moment_public_seal.sign_wire`). The GFS checks the
  follow and its limits and pushes ``{from_instance, sealed}`` to the
  author's household only.
* **Retry.** No outbox: it is keyed by a paired household and re-seals per
  peer, and a GFS is neither. A post that fails for a reason a later try can
  fix (the GFS restarting, a 5xx, a 429, an old node mid-upgrade answering
  404) is retried in memory with backoff for up to a day — the same bounded
  queue the delete certificates use (:mod:`.moment_delete_cert_retry`) —
  latest state per (moment, person): a newer change replaces the pending
  one. The same bytes are re-posted, never re-signed; the author's household
  refuses a replay and anything over 25 h old on its own.

**Privacy (signed off 2026-10-25).** The GFS learns that this household sent
the author's household a reaction or a removal, and when and how often —
never who, with what, to which moment, or whether it was an add or a
removal (the seal is padded to one size). ``docs/principles.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import aiohttp

from ..domain.moment import Moment, MomentGfsReactionTarget
from ..domain.moment_public import is_path_safe_id
from ..moment_public_seal import sign_wire
from .gfs_publish_retry import PublishOutcome, parse_retry_after_s
from .moment_delete_cert_retry import DeleteCertRetryQueue

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import AbstractMomentPublicFollowRepo

log = logging.getLogger(__name__)

#: Where a sealed reaction is posted on a GFS (``{author}`` path-safe).
REACT_PATH: str = "/gfs/moments/users/{author}/react"


class ReactionCapabilities(Protocol):
    """``GfsConnectionService.moment_reactions_supported`` — whether a GFS
    proved ``moment_reactions`` under its signed ``/gfs/info``."""

    async def moment_reactions_supported(self, gfs_id: str) -> bool: ...


def classify_react_status(status: int, retry_after: str | None) -> PublishOutcome:
    """Map a react post's status to an outcome.

    2xx delivered. 408 / 429 / 5xx transient (429 with its ``Retry-After``),
    and so is 404: a household posts only to a server that advertised
    ``moment_reactions``, so a 404 is an older node of a cluster mid-upgrade.
    Anything else (403 not following, 422 malformed, a 3xx never followed)
    is the same answer on every try."""
    if 200 <= status < 300:
        return PublishOutcome.delivered()
    if status in (404, 408, 429) or status >= 500:
        return PublishOutcome.transient(parse_retry_after_s(retry_after))
    return PublishOutcome.permanent()


class MomentReactionGfs:
    """Post sealed reactions to the GFS a public moment came through."""

    __slots__ = (
        "_follows",
        "_gfs",
        "_gfs_repo",
        "_http_client",
        "_own_instance_id",
        "_retry",
        "_signing_key",
    )

    def __init__(
        self,
        *,
        gfs_repo: AbstractGfsConnectionRepo,
        follow_repo: AbstractMomentPublicFollowRepo,
        capabilities: ReactionCapabilities,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._gfs_repo = gfs_repo
        self._follows = follow_repo
        self._gfs = capabilities
        self._http_client = http_client
        self._signing_key: bytes | None = None
        self._own_instance_id = ""
        #: Failed posts, retried for a day (latest state per moment+person).
        self._retry = DeleteCertRetryQueue(self._send_queued, what="moment reaction")

    # ── Wiring ─────────────────────────────────────────────────────────

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    async def start(self) -> None:
        """Start retrying failed posts."""
        await self._retry.start()

    async def stop(self) -> None:
        await self._retry.stop()

    # ── Reactor side ───────────────────────────────────────────────────

    async def target(self, moment: Moment) -> MomentGfsReactionTarget | None:
        """Where a reaction to *moment* goes through a GFS, or ``None`` when
        it can't: not a public moment that came through a GFS, that GFS gone
        or not proving ``moment_reactions``, or no key-wrap key from the
        author's household (an older one)."""
        gfs_id = moment.received_via_gfs_id
        if (
            moment.received_via != "gfs"
            or not moment.is_public
            or not gfs_id
            or self._signing_key is None
            or not is_path_safe_id(moment.author_user_id)
        ):
            return None
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return None
        keywrap = next(
            (
                f.followed_keywrap_pk
                for f in await self._follows.followers_of(moment.author_user_id)
                if f.gfs_id == gfs_id and f.followed_keywrap_pk
            ),
            None,
        )
        if keywrap is None:
            return None
        if not await self._gfs.moment_reactions_supported(gfs_id):
            return None
        return MomentGfsReactionTarget(gfs_id=gfs_id, keywrap_pk=keywrap)

    async def send(
        self,
        target: MomentGfsReactionTarget,
        *,
        author_user_id: str,
        sealed: dict[str, str],
        retry_key: str,
        occurred_at_s: float,
    ) -> None:
        """Post *sealed* to the target GFS for *author_user_id*'s household;
        on a transient failure queue it for retry under *retry_key* (one per
        moment + person — a newer change replaces it)."""
        if self._signing_key is None:
            return
        body = sign_wire(
            self._signing_key, {"instance_id": self._own_instance_id, "sealed": sealed}
        )
        item = {"author": author_user_id, "body": body}
        # A newer change of the same person on the same moment supersedes
        # whatever is still waiting for a retry.
        self._retry.discard(target.gfs_id, retry_key)
        outcome = await self._send_queued(target.gfs_id, item)
        if outcome.kind == "transient":
            self._retry.enqueue(
                target.gfs_id,
                retry_key,
                item,
                deleted_at=occurred_at_s,
                retry_after_s=outcome.retry_after_s,
            )

    async def _send_queued(self, gfs_id: str, item: dict) -> PublishOutcome:
        """POST one queued react *item* (``{author, body}``) to *gfs_id* —
        the first try and every retry. A GFS no longer active (or no
        session) is a permanent miss; a transport error a transient one."""
        if self._http_client is None:
            return PublishOutcome.permanent()
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return PublishOutcome.permanent()
        path = REACT_PATH.format(author=item["author"])
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}{path}",
                allow_redirects=False,
                json=item["body"],
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                outcome = classify_react_status(
                    resp.status, resp.headers.get("Retry-After")
                )
                if outcome.kind != "delivered":
                    log.warning(
                        "moment reaction: GFS %s refused a reaction — HTTP %d (%s)",
                        gfs_id,
                        resp.status,
                        "will retry" if outcome.kind == "transient" else "final",
                    )
                return outcome
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("moment reaction: post to GFS %s failed: %s", gfs_id, exc)
            return PublishOutcome.transient()

    def pending_count(self) -> int:
        """Posts waiting for a retry."""
        return self._retry.pending_count()


__all__ = [
    "REACT_PATH",
    "MomentReactionGfs",
    "ReactionCapabilities",
    "classify_react_status",
]

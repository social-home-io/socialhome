"""Common sync machinery: :class:`ResourceExporter` Protocol +
:class:`ChunkBuilder` helper.

Size budget: chunks target ≤ 8 KB encoded (JSON UTF-8). Individual
pages that exceed the budget get split — :class:`ChunkBuilder` halves
the record list and retries until it fits or lands at a single
record.

Encryption: each chunk body is encrypted with the space content key
(AES-256-GCM, AAD = ``space_id:epoch:sync_id``) before the outer
envelope is signed. The signature covers the encrypted payload, not
the plaintext, so a man-in-the-middle can't swap ciphertexts.

v1 scope: exporters return the full record list for a space, no
pagination complexity. Household-scale spaces (dozens of posts, a
handful of tasks, etc.) fit in a couple of chunks. A follow-up pass
can add keyset pagination when a real operator hits the budget.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Protocol, runtime_checkable
from typing import TYPE_CHECKING

import orjson as _orjson

from ...encoder import FederationEncoder

if TYPE_CHECKING:
    from ....services.space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


#: Outbound streaming order. Bans + members go first so the receiver
#: can apply membership/moderation rules as content arrives — dropping
#: banned-member posts on read is cheaper than purging after the fact.
RESOURCE_ORDER: tuple[str, ...] = (
    "bans",
    "members",
    # F6: avatar bytes ship right after members so the receiver has the
    # ``space_member_profile_pictures`` row populated by the time the SPA
    # renders the members list. Without it ``picture_hash`` resolves to
    # a 404 on the joiner's own host (the bytes lived only on the
    # originating user's instance).
    "member_pictures",
    "posts",
    "comments",
    # Task lists (v_40) ship BEFORE tasks: a space task is only filed under
    # a list the receiver already holds in that space.
    "task_lists",
    # List tombstones (migration 0069) right after the live lists: a
    # household that missed a list delete drops its copy (and the copy's
    # tasks) before ``tasks`` streams. An older receiver drops the unknown
    # resource.
    "task_lists_deleted",
    # Task tombstones (migration 0071) after the lists, before the live
    # tasks: a household that missed a task delete drops its copy, and a
    # stub for one never held needs its list held here already. An older
    # receiver drops the unknown resource.
    "tasks_deleted",
    "tasks",
    "tasks_archived",
    # Page tombstones (migration 0073) before the live pages: a household
    # that missed a page delete drops its copy, and a host stub keeps a
    # stale copy streamed later out. An older receiver drops the unknown
    # resource.
    "pages_deleted",
    "pages",
    "stickies",
    "calendar",
    "gallery",
    "polls",
    # Schedules ship AFTER posts because ``space_schedule_poll_meta``
    # has an FK to ``space_posts(id)``. (F5)
    "schedules",
    "space_zones",
    # Bazaar listings ship AFTER posts because the BazaarListing row
    # has an FK to space_posts(id) — the wrapper post must already be
    # persisted on the receiver side before the listing INSERT lands.
    "bazaar",
    # Space timetables (v_39). Self-contained rows; an older receiver drops
    # the unknown resource.
    "timetables",
)


#: Every resource the exporter framework recognises. The receiver's
#: resource-dispatch table must stay in sync with this.
ALLOWED_RESOURCES: frozenset[str] = frozenset(RESOURCE_ORDER)

#: The sync resources that are the space's **roster**, not its content.
#: Everything else in :data:`RESOURCE_ORDER` is space content, and the
#: receiver refuses it into a space that is archived here — the sync
#: counterpart of the §24.11 ``check_space_archived`` step (see
#: :func:`socialhome.federation.space_scope.archive_refusal`). The roster
#: still converges on an archived space, like the roster events do.
ROSTER_RESOURCES: frozenset[str] = frozenset({"bans", "members", "member_pictures"})

#: Content resources that only ever REMOVE rows. Like the live
#: ``ARCHIVED_ALLOWED_REMOVAL_TYPES``, they still land in a space that is
#: archived here — a delete must not outlive itself on the snapshot.
REMOVAL_RESOURCES: frozenset[str] = frozenset(
    {"task_lists_deleted", "tasks_deleted", "pages_deleted"}
)


#: Sentinel resource sent over the channel after all real chunks.
#: Not encrypted (no payload to hide); only signed.
SENTINEL_RESOURCE: str = "__complete__"


#: Target chunk size in bytes (JSON-encoded envelope incl. encryption
#: overhead). Comfortably below typical WebRTC DataChannel message
#: limits across aiolibdatachannel backends.
CHUNK_SIZE_BUDGET_BYTES: int = 8 * 1024


@runtime_checkable
class ResourceExporter(Protocol):
    """Read-only view of one resource type for sync.

    v1 returns the full record list; :class:`ChunkBuilder` handles
    splitting by size budget.
    """

    resource: str

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        """Return every record for ``space_id`` as a list of
        JSON-serialisable dicts, in a stable order."""
        ...


class ChunkBuilder:
    """Turn a :class:`ResourceExporter` into a stream of encrypted,
    signed chunks ready to send over the DataChannel.

    One builder instance per federation service. Holds no DB state —
    defers to the injected exporter for reads.
    """

    __slots__ = ("_encoder", "_crypto")

    def __init__(
        self,
        encoder: FederationEncoder,
        crypto: "SpaceContentEncryption",
    ) -> None:
        self._encoder = encoder
        self._crypto = crypto

    async def build_chunks(
        self,
        *,
        exporter: ResourceExporter,
        space_id: str,
        sync_id: str,
        sig_suite: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield encrypted + signed chunk envelopes for ``exporter``.

        Each yielded dict can be serialised with :func:`serialise_chunk`
        and sent via ``SyncRtcSession.send_chunk``.
        """
        records = await exporter.list_records(space_id)
        if not records:
            return
        # Size-budget: start with the full list, halve until fits.
        pending = list(records)
        cursor = 0
        while pending:
            chunk_records = pending
            envelope = await self._build_one(
                exporter.resource,
                chunk_records,
                space_id,
                sync_id,
                sig_suite,
                seq_start=cursor,
                seq_end=cursor + len(chunk_records),
                is_last=False,
            )
            encoded = _orjson.dumps(envelope)
            while len(encoded) > CHUNK_SIZE_BUDGET_BYTES and len(chunk_records) > 1:
                chunk_records = chunk_records[: max(1, len(chunk_records) // 2)]
                envelope = await self._build_one(
                    exporter.resource,
                    chunk_records,
                    space_id,
                    sync_id,
                    sig_suite,
                    seq_start=cursor,
                    seq_end=cursor + len(chunk_records),
                    is_last=False,
                )
                encoded = _orjson.dumps(envelope)
            yield envelope
            cursor += len(chunk_records)
            pending = pending[len(chunk_records) :]

    async def build_sentinel(
        self,
        *,
        space_id: str,
        sync_id: str,
        sig_suite: str,
    ) -> dict[str, Any]:
        """Build the final ``__complete__`` envelope for the session.

        Not encrypted (no payload), but signed so the receiver can
        trust the session-end signal.
        """
        envelope: dict[str, Any] = {
            "sync_id": sync_id,
            "resource": SENTINEL_RESOURCE,
            "space_id": space_id,
            "is_last": True,
        }
        bytes_to_sign = _orjson.dumps(envelope)
        envelope["signatures"] = self._encoder.sign_envelope_all(
            bytes_to_sign,
            suite=sig_suite,
        )
        return envelope

    async def _build_one(
        self,
        resource: str,
        records: list[dict[str, Any]],
        space_id: str,
        sync_id: str,
        sig_suite: str,
        *,
        seq_start: int,
        seq_end: int,
        is_last: bool,
    ) -> dict[str, Any]:
        if resource not in ALLOWED_RESOURCES:
            raise ValueError(f"resource {resource!r} not in ALLOWED_RESOURCES")
        plaintext = _orjson.dumps({"records": records})
        epoch, encrypted_payload = await self._crypto.encrypt_chunk(
            space_id=space_id,
            sync_id=sync_id,
            plaintext=plaintext,
        )
        envelope: dict[str, Any] = {
            "sync_id": sync_id,
            "resource": resource,
            "space_id": space_id,
            "epoch": epoch,
            "seq_start": seq_start,
            "seq_end": seq_end,
            "is_last": is_last,
            "encrypted_payload": encrypted_payload,
        }
        bytes_to_sign = _orjson.dumps(envelope)
        envelope["signatures"] = self._encoder.sign_envelope_all(
            bytes_to_sign,
            suite=sig_suite,
        )
        return envelope


def serialise_chunk(envelope: dict[str, Any]) -> bytes:
    """Serialise a chunk envelope to wire bytes."""
    return _orjson.dumps(envelope)


def parse_chunk(raw: bytes | str) -> dict[str, Any]:
    """Inverse of :func:`serialise_chunk`. Raises :class:`ValueError`
    on malformed JSON."""
    try:
        return _orjson.loads(raw)
    except Exception as exc:
        raise ValueError(f"malformed sync chunk: {exc}") from exc

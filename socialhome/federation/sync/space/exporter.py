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
    "posts",
    "comments",
    "tasks",
    "tasks_archived",
    "pages",
    "stickies",
    "calendar",
    "gallery",
    "polls",
)


#: Every resource the exporter framework recognises. The receiver's
#: resource-dispatch table must stay in sync with this.
ALLOWED_RESOURCES: frozenset[str] = frozenset(RESOURCE_ORDER)


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

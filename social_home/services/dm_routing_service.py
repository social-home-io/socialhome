"""DM relay routing (§12.5).

Routes DMs between instances that are not directly paired by walking
the shared-peer graph.  The user's content is end-to-end encrypted by
the browser before it enters the relay network; this service only
looks at routing metadata (destination instance id, hop count,
message id, sender sequence).

Key data stores:
* ``remote_instances`` — our own direct pairings (1-hop neighbours).
* ``network_discovery`` — peers of peers we learned about via
  ``NETWORK_SYNC``. Rows are ``(instance_id, discovered_via)`` where
  ``discovered_via`` is the neighbour that told us about
  ``instance_id``.
* ``conversation_relay_paths`` — sticky per-(conv, target) primary
  path plus fallbacks.
* ``dm_relay_seen`` — 1-hour dedup ring so we don't bounce the same
  envelope forever.

Security invariants enforced here (§CP):
* **§CP.F3** — a protected minor's DMs must travel over a direct
  pairing only.  Multi-hop relay is blocked for that sender.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..domain.federation import (
    FederationEventType,
    PairingStatus,
)
from ..repositories.dm_routing_repo import (
    AbstractDmRoutingRepo,
    normalize_peers,
    utcnow_iso,
)
from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


#: Maximum relay hops (§12.5 spec). Beyond this the envelope is dropped.
MAX_HOPS: int = 3

#: BFS exploration cap — a malicious peer could advertise a huge fake
#: graph to DoS us. The cap matches §12.5.
MAX_SEARCH_NODES: int = 200

#: How long a ``dm_relay_seen`` entry stays fresh.
DEDUP_TTL_SECONDS: int = 3600


# ─── Errors ──────────────────────────────────────────────────────────────


class DmRoutingError(Exception):
    """Base error class."""


class NoRouteError(DmRoutingError):
    """No relay path could be found within :data:`MAX_HOPS`."""


class RelayBlockedError(DmRoutingError):
    """§CP.F3 — protected minor cannot relay."""


# ─── Envelope ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class RelayEnvelope:
    """The outer envelope relay hops see.

    Inner ``encrypted_payload`` is opaque AES-256-GCM ciphertext the
    relay cannot decrypt. Relays forward by ``destination_instance_id``.
    """

    destination_instance_id: str
    destination_user_id: str
    hop_count: int
    inner_event_type: str
    message_id: str
    sender_seq: int
    created_at: str
    sender_ephemeral_pk: str
    encrypted_payload: str
    payload_iv: str
    return_path: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "destination_instance_id": self.destination_instance_id,
            "destination_user_id": self.destination_user_id,
            "hop_count": self.hop_count,
            "inner_event_type": self.inner_event_type,
            "message_id": self.message_id,
            "sender_seq": self.sender_seq,
            "created_at": self.created_at,
            "sender_ephemeral_pk": self.sender_ephemeral_pk,
            "encrypted_payload": self.encrypted_payload,
            "payload_iv": self.payload_iv,
            "return_path": list(self.return_path),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RelayEnvelope":
        return cls(
            destination_instance_id=str(data["destination_instance_id"]),
            destination_user_id=str(data["destination_user_id"]),
            hop_count=int(data.get("hop_count", 0)),
            inner_event_type=str(data.get("inner_event_type", "dm_message")),
            message_id=str(data["message_id"]),
            sender_seq=int(data.get("sender_seq", 0)),
            created_at=str(
                data.get("created_at") or datetime.now(timezone.utc).isoformat()
            ),
            sender_ephemeral_pk=str(data.get("sender_ephemeral_pk", "")),
            encrypted_payload=str(data.get("encrypted_payload", "")),
            payload_iv=str(data.get("payload_iv", "")),
            return_path=tuple(data.get("return_path") or ()),
        )


# ─── Service ─────────────────────────────────────────────────────────────


class DmRoutingService:
    """BFS-based DM relay routing across the paired instance graph."""

    __slots__ = (
        "_repo",
        "_fed_repo",
        "_federation",
        "_own_instance_id",
        "_child_protection",
    )

    def __init__(
        self,
        repo: AbstractDmRoutingRepo,
        federation_repo: AbstractFederationRepo,
        *,
        federation_service=None,
        own_instance_id: str = "",
        child_protection_service=None,
    ) -> None:
        self._repo = repo
        self._fed_repo = federation_repo
        self._federation = federation_service
        self._own_instance_id = own_instance_id
        self._child_protection = child_protection_service

    def attach_federation(
        self,
        federation_service,
        *,
        own_instance_id: str,
    ) -> None:
        self._federation = federation_service
        self._own_instance_id = own_instance_id

    # ─── Graph queries ────────────────────────────────────────────────────

    async def get_own_peers(self) -> list[str]:
        """Confirmed direct pairings (1-hop neighbours)."""
        instances = await self._fed_repo.list_instances(
            status=PairingStatus.CONFIRMED.value,
        )
        return [i.id for i in instances]

    async def get_known_peers(self, source_instance_id: str) -> list[str]:
        """Peers of *source_instance_id* that we have learned about.

        Self is handled specially — we return our confirmed direct
        pairings. For other instances we read ``network_discovery``:
        rows with ``discovered_via = source`` are the peers that
        source introduced to us.
        """
        if source_instance_id == self._own_instance_id:
            return await self.get_own_peers()
        return await self._repo.list_known_peers(source_instance_id)

    async def record_network_sync(
        self,
        *,
        source_instance_id: str,
        peer_ids: Iterable[str],
        hop_count: int = 1,
    ) -> int:
        """Persist a batch of peer announcements from ``NETWORK_SYNC``.

        Cap at 50 peers per source (S-17-ish: limits malicious graph
        inflation). Returns the number of rows inserted/updated.
        """
        unique = normalize_peers(peer_ids, cap=50)
        now = utcnow_iso()
        for pid in unique:
            await self._repo.upsert_network_discovery(
                peer_instance_id=pid,
                discovered_via=source_instance_id,
                seen_at=now,
                hop_count=hop_count,
            )
        return len(unique)

    # ─── BFS ──────────────────────────────────────────────────────────────

    async def find_relay_paths(
        self,
        target_instance_id: str,
    ) -> list[list[str]]:
        """Return every valid relay path to ``target`` within MAX_HOPS.

        Direct pairing returns a single-element path. Multi-hop paths
        strip the leading self-id so each path starts at the first
        relay.
        """
        # Direct connection is the fast path.
        direct = await self._fed_repo.get_instance(target_instance_id)
        if direct is not None and direct.status == PairingStatus.CONFIRMED:
            return [[target_instance_id]]

        own = self._own_instance_id or "self"
        queue: deque[list[str]] = deque([[own]])
        searched = 0
        paths: list[list[str]] = []
        while queue and searched < MAX_SEARCH_NODES:
            path = queue.popleft()
            searched += 1
            if len(path) - 1 >= MAX_HOPS:
                continue
            peers = await self.get_known_peers(path[-1])
            for peer in peers:
                if peer in path:
                    continue
                new_path = path + [peer]
                if peer == target_instance_id:
                    paths.append(new_path[1:])  # drop leading own_id
                    continue
                queue.append(new_path)

        # Sort shortest first + dedup.
        seen: set[tuple[str, ...]] = set()
        out: list[list[str]] = []
        for p in sorted(paths, key=len):
            key = tuple(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    async def find_relay_path(self, target_instance_id: str) -> list[str] | None:
        """Legacy single-path wrapper — shortest relay path or None."""
        paths = await self.find_relay_paths(target_instance_id)
        return paths[0] if paths else None

    # ─── Path selection + persistence ─────────────────────────────────────

    async def select_conversation_path(
        self,
        conversation_id: str,
        sender_user_id: str,
        target_instance_id: str,
    ) -> list[str]:
        """Pick a sticky relay path for a (conversation, sender) pair.

        Deterministic: ``hash(conversation + sender) % len(paths)`` — so
        every send uses the same relay while the primary is alive, but
        different senders may take different paths across the same
        conversation (traffic-analysis resistance).
        """
        paths = await self.find_relay_paths(target_instance_id)
        if not paths:
            raise NoRouteError(
                f"No relay path to {target_instance_id!r} within {MAX_HOPS} hops"
            )
        idx = _hash_mod(f"{conversation_id}:{sender_user_id}", len(paths))
        chosen = paths[idx]

        # Persist the shortest path (paths[0]) as the sticky primary —
        # only one row per (conv, target) due to the PK; the caller
        # re-selects on fallback.
        primary = paths[0]
        await self._repo.upsert_conversation_path(
            conversation_id=conversation_id,
            target_instance=target_instance_id,
            relay_via=primary[0],
            hop_count=len(primary),
            last_used_at=utcnow_iso(),
        )
        return chosen

    # ─── Outbound send ────────────────────────────────────────────────────

    async def send_relay_envelope(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        target_instance_id: str,
        target_user_id: str,
        inner_event_type: str,
        sender_ephemeral_pk: str,
        encrypted_payload: str,
        payload_iv: str,
    ) -> RelayEnvelope:
        """Build + dispatch an outbound DM_RELAY envelope.

        §CP.F3: if the sender is a protected minor we refuse to relay
        unless the target is directly paired (the 1-hop path).
        """
        if self._child_protection is not None:
            allowed = await self._child_protection.is_dm_allowed(
                sender_user_id=sender_user_id,
                target_instance_id=target_instance_id,
            )
            if not allowed:
                raise RelayBlockedError(
                    "DM routing blocked for protected minor (§CP.F3)"
                )

        path = await self.select_conversation_path(
            conversation_id,
            sender_user_id,
            target_instance_id,
        )
        next_hop = path[0]
        seq = await self._next_sender_seq(conversation_id, sender_user_id)

        envelope = RelayEnvelope(
            destination_instance_id=target_instance_id,
            destination_user_id=target_user_id,
            hop_count=0,
            inner_event_type=inner_event_type,
            message_id=str(uuid.uuid4()),
            sender_seq=seq,
            created_at=datetime.now(timezone.utc).isoformat(),
            sender_ephemeral_pk=sender_ephemeral_pk,
            encrypted_payload=encrypted_payload,
            payload_iv=payload_iv,
            return_path=(self._own_instance_id,) if self._own_instance_id else (),
        )

        # Mark seen so our own gc picks it up if it bounces back.
        await self._mark_seen(envelope.message_id)

        if self._federation is not None:
            await self._federation.send_event(
                to_instance_id=next_hop,
                event_type=FederationEventType.DM_RELAY,
                payload=envelope.to_dict(),
            )
        else:
            log.warning(
                "DM relay skipped: federation service not attached "
                "(msg_id=%s, next_hop=%s)",
                envelope.message_id,
                next_hop,
            )
        return envelope

    # ─── Inbound forwarding ───────────────────────────────────────────────

    async def handle_inbound_relay(self, event) -> str:
        """Forward / terminate an inbound ``DM_RELAY`` event.

        Returns one of:
          * ``"dropped:duplicate"`` — already seen this message_id.
          * ``"dropped:too_many_hops"`` — MAX_HOPS exceeded.
          * ``"dropped:no_route"`` — no path onward.
          * ``"delivered"`` — destination is this instance.
          * ``"forwarded"`` — pushed to next hop.
        """
        try:
            envelope = RelayEnvelope.from_dict(event.payload or {})
        except (KeyError, ValueError) as exc:
            log.debug("DM_RELAY: malformed envelope: %s", exc)
            return "dropped:malformed"

        if await self._has_seen(envelope.message_id):
            return "dropped:duplicate"
        await self._mark_seen(envelope.message_id)

        if envelope.hop_count >= MAX_HOPS:
            log.warning(
                "DM_RELAY dropped: %d hops exceeded for msg=%s",
                envelope.hop_count,
                envelope.message_id,
            )
            return "dropped:too_many_hops"

        # Terminal case — we're the destination.
        if envelope.destination_instance_id == self._own_instance_id:
            return "delivered"

        # Find a path from here.
        path = await self.find_relay_path(envelope.destination_instance_id)
        if path is None:
            log.debug(
                "DM_RELAY no route from %s to %s",
                self._own_instance_id,
                envelope.destination_instance_id,
            )
            return "dropped:no_route"

        next_hop = path[0]
        forwarded_dict = envelope.to_dict()
        forwarded_dict["hop_count"] = envelope.hop_count + 1
        # Accumulate return path for delivery-receipt routing.
        rp = list(envelope.return_path)
        if event.from_instance and event.from_instance not in rp:
            rp.append(event.from_instance)
        forwarded_dict["return_path"] = rp

        if self._federation is not None:
            await self._federation.send_event(
                to_instance_id=next_hop,
                event_type=FederationEventType.DM_RELAY,
                payload=forwarded_dict,
            )
        else:
            log.warning(
                "DM relay forward skipped: federation not attached "
                "(msg_id=%s, next_hop=%s)",
                envelope.message_id,
                next_hop,
            )
        return "forwarded"

    # ─── Dedup ring ───────────────────────────────────────────────────────

    async def _mark_seen(self, message_id: str) -> None:
        await self._repo.mark_seen(message_id)

    async def _has_seen(self, message_id: str) -> bool:
        return await self._repo.has_seen(message_id)

    async def prune_seen(self, *, cutoff: datetime | None = None) -> int:
        """Delete dedup rows older than ``DEDUP_TTL_SECONDS``."""
        cutoff_dt = cutoff or (
            datetime.now(timezone.utc) - timedelta(seconds=DEDUP_TTL_SECONDS)
        )
        return await self._repo.prune_seen(cutoff_iso=cutoff_dt.isoformat())

    # ─── Sender sequence ─────────────────────────────────────────────────

    async def _next_sender_seq(
        self,
        conversation_id: str,
        sender_user_id: str,
    ) -> int:
        """Atomically increment + return the next sender_seq."""
        return await self._repo.next_sender_seq(
            conversation_id=conversation_id,
            sender_user_id=sender_user_id,
        )


# ─── Helpers ─────────────────────────────────────────────────────────────


def _hash_mod(key: str, n: int) -> int:
    """Deterministic non-cryptographic hash mod *n*.

    SHA-256 is overkill for this — we just want a stable distribution
    across conversations. Truncating the first 8 bytes gives plenty of
    distribution with zero dependency.
    """
    if n <= 0:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % n

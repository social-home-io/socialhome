"""Outbound HTTP client for the §11 peer-pairing bootstrap handshake.

Separate from :class:`FederationService.send_event` because pairing
bootstrap runs on a **plaintext, Ed25519-signed** wire format — not
encrypted + paired. The §24.11 pipeline would reject it at the
instance-lookup step (the pair doesn't exist yet on the receiver's
side). See `graceful-weaving-dahl.md` for the full design.

Two one-way messages, both carried on the peer's federation inbox URL
as :data:`FederationEventType.PAIRING_PEER_ACCEPT` /
:data:`FederationEventType.PAIRING_PEER_CONFIRM`:

* **peer-accept** — B → A. Delivers B's pairing material
  (``identity_pk``, ``dh_pk``, ``inbox_url``, ``display_name``,
  ``verification_code``) so A can materialise its local
  ``RemoteInstance`` for B and surface the SAS code to its admin.
* **peer-confirm** — A → B. Signals that A's admin entered the
  matching SAS, letting B flip its local ``PENDING_RECEIVED`` status
  to ``CONFIRMED``.

Riding the inbox URL keeps the handshake reachable in HA / HAOS, where
the HA integration only proxies the federation inbox path —
Supervisor Ingress blocks every other route to remote callers.

Both are best-effort sends: network errors are logged and returned as
``ok=False`` so the caller can surface a retry hint in the UI without
corrupting local state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import orjson

from ..crypto import sign_ed25519
from ..domain.federation import FederationEventType
from ..peer_url import InvalidPeerUrlError, validate_peer_url

log = logging.getLogger(__name__)

#: How long we wait for the remote endpoint before giving up.
_TIMEOUT_S = 10.0


@dataclass(slots=True, frozen=True)
class PeerPairingResult:
    """Outcome of a single peer-pairing POST. ``ok`` iff 2xx."""

    ok: bool
    status_code: int | None
    error: str | None = None


def _canonical_body_bytes(body: dict) -> bytes:
    """Canonical bytes of ``body`` (without ``signature``) for signing.

    Uses orjson's SORT_KEYS option so both sides compute the same
    digest regardless of dict ordering. The ``signature`` field — if
    present — is omitted before serialising; callers sign the
    unsigned body and add ``signature`` afterwards. ``event_type`` is
    included in the canonical bytes so the signature covers the
    dispatch marker too.
    """
    signed_view = {k: v for k, v in body.items() if k != "signature"}
    return orjson.dumps(signed_view, option=orjson.OPT_SORT_KEYS)


def sign_peer_body(body: dict, *, own_identity_seed: bytes) -> dict:
    """Return ``body`` with an ``Ed25519`` signature appended.

    Public helper so the coordinator (which already holds the signing
    seed via the encoder) can drive signing without pulling in this
    module's HTTP concerns.
    """
    signature = sign_ed25519(own_identity_seed, _canonical_body_bytes(body))
    return {**body, "signature": signature.hex()}


class PeerPairingClient:
    """Thin outbound client for the §11 peer-pairing bootstrap.

    Both ``send_peer_accept`` and ``send_peer_confirm`` POST a signed
    body directly to the peer's federation ``inbox_url`` (no path
    rewriting). The body carries an ``event_type`` of
    ``PAIRING_PEER_ACCEPT`` / ``PAIRING_PEER_CONFIRM`` and the
    receiving federation-inbox view dispatches it ahead of the §24.11
    pipeline.

    Construction parameters:

    * ``own_identity_seed`` — 32-byte Ed25519 private-key seed used to
      sign outbound bodies.
    * ``client_factory`` — async zero-arg factory yielding a shared
      :class:`aiohttp.ClientSession`. Matches the transport pattern in
      :class:`socialhome.federation.transport.HttpsInboxTransport` so
      the SSL context / DNS cache / keep-alive pool are all shared
      across outbound federation traffic.
    """

    __slots__ = ("_client_factory", "_client", "_own_identity_seed", "_timeout_s")

    def __init__(
        self,
        *,
        own_identity_seed: bytes,
        client_factory: Callable[[], Awaitable[Any]],
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        self._own_identity_seed = own_identity_seed
        self._client_factory = client_factory
        self._client: Any | None = None
        self._timeout_s = timeout_s

    async def _client_once(self) -> Any:
        if self._client is None:
            self._client = await self._client_factory()
        return self._client

    async def send_peer_accept(
        self,
        *,
        peer_inbox_url: str,
        body: dict,
    ) -> PeerPairingResult:
        """POST a signed ``peer-accept`` body to the inviter (A)."""
        return await self._post(
            peer_inbox_url,
            FederationEventType.PAIRING_PEER_ACCEPT.value,
            body,
        )

    async def send_peer_confirm(
        self,
        *,
        peer_inbox_url: str,
        body: dict,
    ) -> PeerPairingResult:
        """POST a signed ``peer-confirm`` body to the scanner (B)."""
        return await self._post(
            peer_inbox_url,
            FederationEventType.PAIRING_PEER_CONFIRM.value,
            body,
        )

    async def _post(
        self,
        peer_inbox_url: str,
        event_type: str,
        body: dict,
    ) -> PeerPairingResult:
        """Sign ``body`` (with ``event_type`` woven in) and POST it.

        The URL is re-validated here even though every entry point already
        checked it: this is the last point before a signed body leaves the
        household, and a stored row may predate the entry-point check.
        Redirects are not followed — a ``3xx`` would hand the body to a
        target that never went through :func:`validate_peer_url`.
        """
        if not peer_inbox_url:
            log.warning("peer-pairing: empty inbox URL for event_type=%s", event_type)
            return PeerPairingResult(
                ok=False,
                status_code=None,
                error="empty peer inbox URL",
            )
        try:
            validate_peer_url(peer_inbox_url, field="peer inbox URL")
        except InvalidPeerUrlError as exc:
            log.warning("peer-pairing: refusing %s: %s", event_type, exc)
            return PeerPairingResult(ok=False, status_code=None, error=str(exc))

        envelope_body: dict = {"event_type": event_type, **body}
        signed = sign_peer_body(
            envelope_body, own_identity_seed=self._own_identity_seed
        )
        try:
            client = await self._client_once()
            async with client.post(
                peer_inbox_url,
                data=orjson.dumps(signed),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                allow_redirects=False,
            ) as resp:
                status = resp.status
                ok = 200 <= status < 300
                if not ok:
                    detail = await _read_brief(resp)
                    log.warning(
                        "peer-pairing: %s %s returned %d: %s",
                        event_type,
                        peer_inbox_url,
                        status,
                        detail,
                    )
                return PeerPairingResult(ok=ok, status_code=status)
        except Exception as exc:
            log.warning(
                "peer-pairing: %s %s failed: %s",
                event_type,
                peer_inbox_url,
                exc,
            )
            return PeerPairingResult(ok=False, status_code=None, error=str(exc))


async def _read_brief(resp: Any) -> str:
    """Read up to 256 bytes of a failure response, for logging only."""
    try:
        raw = await resp.content.read(256)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")

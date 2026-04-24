"""Outbound HTTP client for the §11 peer-pairing bootstrap handshake.

Separate from :class:`FederationService.send_event` because pairing
bootstrap runs on a **plaintext, Ed25519-signed** wire format — not
encrypted + paired. The §24.11 pipeline would reject it at the
instance-lookup step (the pair doesn't exist yet on the receiver's
side). See `graceful-weaving-dahl.md` for the full design.

Two one-way messages:

* **peer-accept** — B → A. Delivers B's pairing material
  (``identity_pk``, ``dh_pk``, ``inbox_url``, ``display_name``,
  ``verification_code``) so A can materialise its local
  ``RemoteInstance`` for B and surface the SAS code to its admin.
* **peer-confirm** — A → B. Signals that A's admin entered the
  matching SAS, letting B flip its local ``PENDING_RECEIVED`` status
  to ``CONFIRMED``.

Both are best-effort sends: network errors are logged and returned as
``ok=False`` so the caller can surface a retry hint in the UI without
corrupting local state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

import aiohttp
import orjson

from ..crypto import sign_ed25519

log = logging.getLogger(__name__)

#: How long we wait for the remote endpoint before giving up.
_TIMEOUT_S = 10.0


@dataclass(slots=True, frozen=True)
class PeerPairingResult:
    """Outcome of a single peer-pairing POST. ``ok`` iff 2xx."""

    ok: bool
    status_code: int | None
    error: str | None = None


def _derive_peer_base(peer_inbox_url: str) -> str:
    """From a peer inbox URL like ``https://host/federation/inbox/xyz``,
    return ``https://host`` — the host/port the peer-pairing routes
    live under.
    """
    parsed = urlparse(peer_inbox_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"peer inbox URL is malformed: {peer_inbox_url!r}")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _canonical_body_bytes(body: dict) -> bytes:
    """Canonical bytes of ``body`` (without ``signature``) for signing.

    Uses orjson's SORT_KEYS option so both sides compute the same
    digest regardless of dict ordering. The ``signature`` field — if
    present — is omitted before serialising; callers sign the
    unsigned body and add ``signature`` afterwards.
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
    """Thin outbound client for ``POST /api/pairing/peer-{accept,confirm}``.

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
        return await self._post(peer_inbox_url, "/api/pairing/peer-accept", body)

    async def send_peer_confirm(
        self,
        *,
        peer_inbox_url: str,
        body: dict,
    ) -> PeerPairingResult:
        """POST a signed ``peer-confirm`` body to the scanner (B)."""
        return await self._post(peer_inbox_url, "/api/pairing/peer-confirm", body)

    async def _post(
        self,
        peer_inbox_url: str,
        path: str,
        body: dict,
    ) -> PeerPairingResult:
        """Sign ``body`` and POST it to ``{peer_host}{path}``."""
        try:
            base = _derive_peer_base(peer_inbox_url)
        except ValueError as exc:
            log.warning("peer-pairing: bad URL %r: %s", peer_inbox_url, exc)
            return PeerPairingResult(ok=False, status_code=None, error=str(exc))

        signed = sign_peer_body(body, own_identity_seed=self._own_identity_seed)
        url = base + path
        try:
            client = await self._client_once()
            async with client.post(
                url,
                data=orjson.dumps(signed),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as resp:
                status = resp.status
                ok = 200 <= status < 300
                if not ok:
                    detail = await _read_brief(resp)
                    log.warning(
                        "peer-pairing: %s %s returned %d: %s",
                        path,
                        url,
                        status,
                        detail,
                    )
                return PeerPairingResult(ok=ok, status_code=status)
        except Exception as exc:
            log.warning("peer-pairing: %s %s failed: %s", path, url, exc)
            return PeerPairingResult(ok=False, status_code=None, error=str(exc))


async def _read_brief(resp: Any) -> str:
    """Read up to 256 bytes of a failure response, for logging only."""
    try:
        raw = await resp.content.read(256)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")

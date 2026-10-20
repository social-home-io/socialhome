"""Shared ICE-server / TURN-credential helpers.

The same coturn HMAC-credential scheme (REST API spec §6) is needed by
both the SPA's ``/api/calls/ice-servers`` endpoint (per-user
credentials, short TTL) and the server-to-server federation transport
(per-instance credentials, also short TTL). Extracted here so the two
call sites stay consistent and a future refactor — e.g. swapping
HMAC-SHA1 for SHA-256 — happens in one place.

This module is also the place to put any future ICE-server heuristics
(``iceTransportPolicy=relay`` toggle, dynamic STUN-server probing,
diagnostic "no TURN configured" warnings) so the wiring stays close
to its docs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import aiolibdatachannel as rtc

log = logging.getLogger(__name__)

#: ``(entry index, reason)`` pairs :func:`rtc_configuration` already warned
#: about. It runs once per peer (every highlight viewer), so a lasting
#: misconfiguration warns once per process, not once per viewer. Bounded:
#: past :data:`_ICE_WARNED_MAX` distinct pairs it stops remembering (and
#: stops warning) — a list that broken has long been reported.
_ICE_WARNED: set[tuple[int, str]] = set()
_ICE_WARNED_MAX = 64


def _warn_skipped_ice_entry(index: int, reason: str) -> None:
    """WARNING about a skipped ICE entry, once per ``(index, reason)``."""
    key = (index, reason)
    if key in _ICE_WARNED or len(_ICE_WARNED) >= _ICE_WARNED_MAX:
        return
    _ICE_WARNED.add(key)
    log.warning("WebRTC: ICE server entry #%d skipped: %s.", index, reason)


def make_turn_credential(
    secret: str,
    user_id: str,
    *,
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Return ``(username, password)`` for coturn's time-limited TURN
    credential scheme (TURN REST API spec §6, also see coturn's
    ``--use-auth-secret``).

    ``username = "<expiry>:<user_id>"`` where ``expiry`` is a unix
    timestamp in the future. ``password`` is the base64 of an
    HMAC-SHA1 of ``username`` keyed by ``secret``. The TURN server
    recomputes the HMAC at allocation time and checks the expiry —
    so a credential leak is bounded by ``ttl_seconds``.

    Used by:

    * :class:`socialhome.routes.calls.IceServersView` — per-user
      credentials for the SPA's calls / live-highlight WebRTC.
    * :func:`socialhome.app._default_ice_servers` — per-instance
      credentials for the federation transport (server-to-server
      WebRTC). ``user_id`` is the local ``instance_id`` there.
    """
    expiry = int(time.time()) + max(60, int(ttl_seconds))
    username = f"{expiry}:{user_id}"
    digest = hmac.new(
        secret.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return username, credential


def build_ice_servers(
    *,
    stun_url: str | None,
    turn_url: str | None,
    turn_user: str | None,
    turn_cred: str | None,
    turn_secret: str | None,
    turn_ttl_seconds: int,
    hmac_user_id: str | None,
) -> list[dict]:
    """Compose the ``ice_servers`` list passed to libdatachannel /
    RTCPeerConnection. Returns the Chrome-style list shape:

    ``[{"urls": [...]}, {"urls": [...], "username": ..., "credential": ...}]``

    The ``turn_secret`` + ``hmac_user_id`` combination wins over the
    static ``turn_user`` / ``turn_cred`` pair when both are
    configured — HMAC credentials expire, static ones don't. When
    only the static creds are set, those are used as-is.

    When **neither** TURN nor a TURN secret is configured, the
    return list contains only the STUN entry — and the caller
    (federation transport) should log a hint if WebRTC fails to
    establish (the typical real-world failure mode is symmetric NAT
    or strict firewalls where STUN-only deployments can never
    complete ICE pair-checks; only TURN as a relay rescues them).
    """
    servers: list[dict] = []
    if stun_url:
        servers.append({"urls": [stun_url]})
    if turn_url:
        entry: dict = {"urls": [turn_url]}
        if turn_secret and hmac_user_id:
            username, credential = make_turn_credential(
                turn_secret,
                hmac_user_id,
                ttl_seconds=turn_ttl_seconds,
            )
            entry["username"] = username
            entry["credential"] = credential
        else:
            if turn_user:
                entry["username"] = turn_user
            if turn_cred:
                entry["credential"] = turn_cred
        servers.append(entry)
    return servers


def warn_if_no_turn(ice_servers: list[dict]) -> None:
    """Log a one-shot operator hint when the ICE list contains no
    TURN entry. Two households that can't NAT-traverse each other
    directly need a TURN relay; without one the WebRTC handshake
    will fail at ICE pair-checks ("Connectivity timer expired" in
    libjuice logs). See ``docs/operations/turn.md`` for setup.
    """
    has_turn = any(
        any(u.startswith(("turn:", "turns:")) for u in srv.get("urls", []))
        for srv in ice_servers
    )
    if not has_turn:
        log.warning(
            "WebRTC: no TURN server configured. STUN alone can't traverse "
            "symmetric NAT or strict firewalls; if federation peers fail "
            "to establish RTC (transport stays on 'https' indefinitely), "
            "deploy a TURN server (coturn is easy; see "
            "docs/operations/turn.md) and set webrtc_turn_url + "
            "webrtc_turn_secret (or webrtc_turn_user/cred) in "
            "socialhome.toml.",
        )


def warn_if_turn_unusable(ice_servers: list[dict]) -> None:
    """Warn when a TURN server is configured but carries no credentials.

    A ``turn:``/``turns:`` entry without ``username`` + ``credential`` (and
    no HMAC secret to mint them) authenticates anonymously — coturn rejects
    it, so the relay silently never works and RTC falls back to slow HTTPS.
    This is worse than no TURN at all: :func:`warn_if_no_turn` stays quiet
    because a TURN entry *is* present. Surface the misconfig so the operator
    can fix the half-wired setup.
    """
    for srv in ice_servers:
        urls = srv.get("urls", [])
        if not any(u.startswith(("turn:", "turns:")) for u in urls):
            continue
        if not srv.get("username") or not srv.get("credential"):
            log.warning(
                "WebRTC: TURN server %s is configured WITHOUT usable "
                "credentials. Set webrtc_turn_secret (HMAC, recommended) or "
                "a static webrtc_turn_user / webrtc_turn_cred pair, or coturn "
                "rejects the relay and RTC falls back to slow HTTPS. See "
                "docs/operations/turn.md.",
                urls[0] if urls else "?",
            )
        # Deliberately NOT ``return`` here. An earlier revision bailed out
        # after the first TURN entry, which was harmless while the only
        # caller was ``build_ice_servers`` (it emits at most one), but this
        # is now also handed HA Core's pulled list, which can carry several
        # — and a bad entry behind a good one must still be reported.


def rtc_configuration(ice_servers: list[dict]) -> rtc.RTCConfiguration:
    """Flatten a Chrome-style ``ice_servers`` list (:func:`build_ice_servers`)
    into an :class:`aiolibdatachannel.RTCConfiguration` — the one builder
    every peer uses (federation transport, space sync, the public
    highlight / moment answerers).

    Each entry carries ``urls`` as a string or a list (``url`` accepted as
    an alias) plus optional TURN ``username`` / ``credential``; every URL
    becomes its own :class:`~aiolibdatachannel.IceServer` with the
    credentials as first-class fields. A malformed entry is skipped rather
    than passed through — libdatachannel refuses the whole configuration
    (``rtcCreatePeerConnection: invalid argument``) for one bad URL — and
    each skip is a WARNING naming the entry's position and why, never its
    values (a TURN entry carries credentials), once per process for each
    distinct ``(position, reason)``.
    """
    servers: list[rtc.IceServer] = []
    for index, srv in enumerate(ice_servers):
        if not isinstance(srv, dict):
            _warn_skipped_ice_entry(index, f"not an object ({type(srv).__name__})")
            continue
        url_field = srv.get("urls", srv.get("url"))
        if url_field is None or url_field == []:
            _warn_skipped_ice_entry(index, "no URL ('urls' / 'url')")
            continue
        raw_urls = url_field if isinstance(url_field, list) else [url_field]
        username = srv.get("username") or None
        credential = srv.get("credential") or None
        bad: list[str] = []
        for url in raw_urls:
            if isinstance(url, str) and url:
                servers.append(
                    rtc.IceServer(url=url, username=username, credential=credential)
                )
            else:
                bad.append("empty str" if url == "" else type(url).__name__)
        if bad:
            _warn_skipped_ice_entry(
                index,
                f"{len(bad)} URL(s) not a non-empty string ({', '.join(bad)})",
            )
    return rtc.RTCConfiguration(ice_servers=servers)

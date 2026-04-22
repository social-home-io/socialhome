"""Security invariants shared across the service / route layers.

Two responsibilities:

* :data:`SENSITIVE_FIELDS` — the frozenset of field names that MUST NEVER
  appear in any API response or federation payload. :func:`sanitise_for_api`
  applies the rule to a plain dict.
* :func:`error_response` — the canonical JSON error shape used by every
  aiohttp route handler.

Nothing else lives here. Business-logic error types live next to their
service. This module has no dependency on aiohttp at import time so that it
can be imported from unit tests without bringing the web stack along.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web


#: Fields that MUST NEVER appear in any API response or federation payload.
#:
#: The route layer calls :func:`sanitise_for_api` on any dict derived from a
#: database row before returning it to a client. The federation layer does
#: the equivalent filter before encrypting event payloads.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        # Cryptographic material
        "identity_private_key",
        "key_self_to_remote",
        "key_remote_to_self",
        "private_key",
        "api_token_hash",
        "routing_secret",
        "kek",
        "content_key_hex",
        "own_dh_private_key",
        "identity_dh_sk",
        # Authentication
        "password_hash",
        "bcrypt_hash",
        "token_hash",
        "session_token",
        # Personal identifiers — privacy (§25.3)
        "email",
        "phone",
        "date_of_birth",
        # Device / push tokens — never leave the instance
        "push_subscription",
        "push_subscription_json",
        "device_token",
        "p256dh",
        "auth_secret",
        "endpoint",  # Web Push endpoint URL is sensitive too
        # Federation envelope material — never echoed in API responses
        "encrypted_payload",
        "signature",
        "signatures",
        "session_key",
        # Precise GPS — approximate (4dp-truncated ``lat``/``lon``) is OK
        "location_lat",
        "location_lon",
        # Child-protection flags (§CP) — never shared
        "declared_age",
        "is_minor",
        "guardian_ids",
        "child_protection_enabled",
    }
)


def sanitise_for_api(data: dict[str, Any]) -> dict[str, Any]:
    """Remove :data:`SENSITIVE_FIELDS` from a plain dict.

    Call this on any dict derived from a DB row before returning it to a
    client. Nested dicts are filtered recursively; lists / tuples have their
    dict elements filtered in place.
    """
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key in SENSITIVE_FIELDS:
            continue
        if isinstance(value, dict):
            clean[key] = sanitise_for_api(value)
        elif isinstance(value, list):
            clean[key] = [
                sanitise_for_api(v) if isinstance(v, dict) else v for v in value
            ]
        elif isinstance(value, tuple):
            clean[key] = tuple(
                sanitise_for_api(v) if isinstance(v, dict) else v for v in value
            )
        else:
            clean[key] = value
    return clean


def error_response(
    status: int,
    code: str,
    detail: str = "",
    *,
    extra: dict | None = None,
) -> web.Response:
    """Canonical error shape for every HTTP route handler.

    Wire format::

        {"error": {"code": "SPACE_NOT_FOUND", "detail": "...", ...extras}}

    ``detail`` MUST be a human-readable string safe to display in the client
    UI. Never pass raw exception text, SQL errors, stack traces, internal IDs
    or filesystem paths. Pass a fixed string literal — never ``str(exc)``
    directly. Unhandled exceptions are caught by the aiohttp error middleware
    and returned as ``500 INTERNAL_ERROR`` with a generic message; the real
    stack trace is logged server-side only.

    ``extra`` merges additional structured fields into the ``error``
    object — used for machine-readable hints like the disabled feature
    section. Keys must stay flat + JSON-safe; never leak internals here.
    """
    body: dict = {"code": code, "detail": detail}
    if extra:
        body.update(extra)
    return web.json_response({"error": body}, status=status)

"""Push notification dispatcher (§25.3 — Web Push).

Sends Web Push notifications to subscriptions registered via
:class:`SqlitePushSubscriptionRepo`.  Privacy: we only send the
notification *title* and a small set of routing fields (e.g.
``space_id`` so the SW can cluster notifications per surface).  The
body is **never** included for DMs, location messages, or any other
user-generated content per §25.3.

Two delivery paths:

* If the optional ``pywebpush`` dependency is installed, real Web Push
  is used (VAPID-signed, encrypted to the subscription's
  ``p256dh`` / ``auth`` keys).
* Otherwise, the dispatcher logs and returns success — useful for
  tests and for instances that haven't configured VAPID keys yet.

VAPID keys live alongside other secrets in ``data_dir/.vapid_*`` files;
they are generated automatically on first start.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ..crypto import b64url_encode
from ..repositories.push_subscription_repo import (
    AbstractPushSubscriptionRepo,
    PushSubscription,
)

log = logging.getLogger(__name__)

try:  # pragma: no cover
    from pywebpush import WebPushException, webpush

    _PYWEBPUSH_AVAILABLE = True
except ImportError:
    webpush = None
    WebPushException = Exception
    _PYWEBPUSH_AVAILABLE = False


@dataclass(slots=True, frozen=True)
class VapidKeyPair:
    """A persisted VAPID keypair used to authenticate Web Push payloads."""

    private_pem: str
    public_b64url: str


@dataclass(slots=True, frozen=True)
class PushPayload:
    """The minimal fields that travel inside a push notification.

    Body is intentionally absent — see §25.3.
    """

    title: str
    tag: str | None = None  # surface clustering
    click_url: str | None = None
    space_id: str | None = None
    icon_url: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                k: v
                for k, v in {
                    "title": self.title,
                    "tag": self.tag,
                    "click_url": self.click_url,
                    "space_id": self.space_id,
                    "icon_url": self.icon_url,
                }.items()
                if v is not None
            }
        )


class PushService:
    """Dispatch Web Push notifications.

    Parameters
    ----------
    sub_repo:
        Where to look up subscriptions per user.
    vapid:
        Operator's VAPID keypair; built via :func:`load_or_create_vapid`.
    contact_email:
        Inserted into the VAPID JWT ``sub`` claim. Push services use this
        to contact the operator if their endpoint is being abused.
    """

    __slots__ = ("_sub_repo", "_vapid", "_contact_email")

    def __init__(
        self,
        *,
        sub_repo: AbstractPushSubscriptionRepo,
        vapid: VapidKeyPair,
        contact_email: str = "admin@social-home.local",
    ) -> None:
        self._sub_repo = sub_repo
        self._vapid = vapid
        self._contact_email = contact_email

    @property
    def vapid_public_key(self) -> str:
        """Base64url public VAPID key — handed to the browser at subscription time."""
        return self._vapid.public_b64url

    async def push_to_user(self, user_id: str, payload: PushPayload) -> int:
        """Push to every subscription of *user_id*. Returns delivery count."""
        subs = await self._sub_repo.list_for_user(user_id)
        delivered = 0
        for sub in subs:
            ok = await self._push_one(sub, payload)
            if ok:
                delivered += 1
        return delivered

    async def push_to_users(
        self,
        user_ids: list[str] | set[str],
        payload: PushPayload,
    ) -> int:
        delivered = 0
        for uid in user_ids:
            delivered += await self.push_to_user(uid, payload)
        return delivered

    async def notify_missed_call(
        self,
        *,
        recipient_user_ids: list[str] | set[str],
        caller_user_id: str,
        call_id: str,
        conversation_id: str,
    ) -> int:
        """Send a missed-call notification (§26.8, §25.3).

        Title-only per the privacy rule; the body is intentionally
        omitted so no caller name leaks to a locked-screen preview. The
        ``click_url`` deep-links the service worker to the DM thread so
        the user can "Call back" in one tap.
        """
        payload = PushPayload(
            title="Missed call",
            tag=f"call-missed:{call_id}",
            click_url=f"/dms/{conversation_id}?missed={call_id}",
        )
        return await self.push_to_users(recipient_user_ids, payload)

    async def _push_one(self, sub: PushSubscription, payload: PushPayload) -> bool:
        if not _PYWEBPUSH_AVAILABLE:  # pragma: no cover
            log.debug(
                "push: pywebpush not installed; would send %r to %s",
                payload.title,
                sub.endpoint[:40],
            )
            return True
        try:  # pragma: no cover
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth_secret},
                },
                data=payload.to_json(),
                vapid_private_key=self._vapid.private_pem,
                vapid_claims={"sub": f"mailto:{self._contact_email}"},
            )
            return True
        except WebPushException as exc:  # pragma: no cover
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                # Subscription is dead — clean it out.
                await self._sub_repo.delete_by_endpoint(sub.endpoint)
            log.warning("push: failed status=%s endpoint=%s", status, sub.endpoint[:40])
            return False
        except Exception as exc:  # pragma: no cover
            log.warning("push: unexpected error: %s", exc)
            return False


# ─── VAPID key persistence ────────────────────────────────────────────────

VAPID_PRIVATE_FILENAME: str = ".vapid_private.pem"
VAPID_PUBLIC_FILENAME: str = ".vapid_public.txt"


def load_or_create_vapid(data_dir: str | Path) -> VapidKeyPair:
    """Load the operator's VAPID keypair, generating it on first call.

    The private key is stored in ``{data_dir}/.vapid_private.pem`` with
    permissions ``0o600``; the base64url public key is also persisted for
    convenience.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    priv_path = data_dir / VAPID_PRIVATE_FILENAME
    pub_path = data_dir / VAPID_PUBLIC_FILENAME

    if priv_path.exists() and pub_path.exists():
        return VapidKeyPair(
            private_pem=priv_path.read_text(),
            public_b64url=pub_path.read_text().strip(),
        )

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = b64url_encode(public_bytes)

    priv_path.write_text(private_pem)
    priv_path.chmod(0o600)
    pub_path.write_text(public_b64)
    return VapidKeyPair(private_pem=private_pem, public_b64url=public_b64)

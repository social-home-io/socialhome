"""GFS public-Momentum routes (§Momentum-public).

Three surfaces:

* **Signed wire endpoints** for instance-side mutations:
  - ``POST /gfs/moments/users/register`` — opt a user into the directory.
  - ``POST /gfs/moments/users/{user_id}/deregister`` — remove a registration.
  - ``POST /gfs/moments/users/{user_id}/picture`` — push avatar bytes.
  - ``POST /gfs/moments/users/{user_id}/follow`` — record a follower.
  - ``POST /gfs/moments/users/{user_id}/unfollow`` — drop a follower.
  - ``POST /gfs/moments/publish`` — fan out a signed, ENCRYPTED moment
    envelope (v_62; any other shape is refused — the GFS never carries a
    public moment in plaintext).
  - ``POST /gfs/moments/delete`` — fan out an encrypted tombstone.
  - ``POST /gfs/moments/delete_cert`` — keep the delete's signed
    certificate 30 days for followers offline when it went out (v_66).
  - ``POST /gfs/moments/keys`` — deliver sealed moment keys, each to the
    one follower household it names (v_62).
  - ``POST /gfs/moments/users/{user_id}/key_request`` — a follower
    household asks the author's household for its current key (v_62).
* **Public discovery** ``GET /gfs/moments/users`` (JSON, w/ ``?q=``),
  ``GET /gfs/moments/users/{user_id}`` (per-user JSON),
  ``GET /gfs/moments/users/{user_id}/picture`` (avatar bytes).
* **Public landing** ``GET /moments`` and ``GET /moments/{user_id}``
  (HTML SPA shells) — anon-browseable directory + per-user pages. The
  per-user page hands off to the pinned viewer when one is configured.

All signed endpoints reuse :func:`_rtc_authenticate` so the same
middleware that gates the Highlights publish flow gates these.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import UTC, datetime

import orjson
from aiohttp import web

from ...csp import media_response_headers
from ...moment_origin import delete_cert_refusal
from ...moment_public_media import MAX_BLOB_BYTES
from ...moment_public_seal import (
    DELIVERY_FIELDS,
    ENVELOPE_FIELDS,
    MAX_CIPHERTEXT_CHARS,
    MAX_MOMENT_ID_CHARS,
    is_wire_int,
)
from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..cluster_relay import AUDIENCE_KEY_MAX_LEN
from ..domain import GfsUserPicture
from ..html_page import html_response
from ..link_preview import person_handoff
from ..moment_media import MediaRefused
from ..moment_public_registry import envelope_step_current
from ..page_models import load_person_page
from .base import GfsBaseView
from .rtc import RELAY_AUTH_MAX_SKEW_SECONDS, _rtc_authenticate

log = logging.getLogger(__name__)

#: Cap for inbound picture bytes (post-resize). 256 KiB matches the
#: SH-side avatar pipeline; reject larger uploads outright.
_MAX_PICTURE_BYTES = 256 * 1024

#: MIME types accepted for avatar upload.
_PICTURE_MIME_ALLOWED = frozenset({"image/jpeg", "image/png", "image/webp"})

#: Bio length cap. Twitter-shaped — short enough to render in a card.
_MAX_BIO_LEN = 280

#: Longest ``user_id`` a registration may carry. The user id is the key of
#: the author's ``followers`` audience in a cluster forward, which every node
#: refuses past this length — a longer id would register fine and then never
#: reach a follower on another node.
MAX_USER_ID_LEN = AUDIENCE_KEY_MAX_LEN

#: Body cap for an encrypted publish / delete: the ciphertext cap plus the
#: routing fields and signature.
_MAX_ENVELOPE_BYTES = MAX_CIPHERTEXT_CHARS + 2048

#: Body cap for ``POST /gfs/moments/delete_cert`` — a certificate is ~0.35 KB
#: (v_66).
_MAX_DELETE_CERT_BYTES = 4096

#: A certificate is posted the moment its delete happens, and retried for
#: up to a day when that fails (adversarial review F3,
#: ``services.moment_delete_cert_retry``): one whose delete time is older
#: than this (a day and an hour of slack), or further ahead than the skew,
#: is refused — so a captured old one can't restart its 30 days here. A
#: replay within it changes nothing (one row per author × moment).
DELETE_CERT_MAX_AGE_S: int = 25 * 3600
DELETE_CERT_MAX_SKEW_S: int = 300

#: Sealed keys one ``POST /gfs/moments/keys`` may carry, and its body cap
#: (a sealed key is ~0.6 KB).
MAX_KEY_DELIVERIES = 100
_MAX_KEYS_BYTES = 128 * 1024

#: Longest ``sealed.ciphertext`` of one delivery (a sealed 32-byte key in
#: its JSON wrapper is ~300 chars).
_MAX_SEALED_KEY_CHARS = 1024

#: A follower's declared ``moment_suites``: at most this many suite names
#: per kind, each at most this long.
_MAX_SUITES = 8
_MAX_SUITE_CHARS = 64


def _suites_json(raw: object) -> str | None | web.Response:
    """The follow body's ``moment_suites`` (v_62) as canonical JSON —
    ``None`` when absent (an older household), a 422 when malformed."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"kem", "seal"}:
        return web.json_response({"error": "moment_suites_invalid"}, status=422)
    out: dict[str, list[str]] = {}
    for kind in ("kem", "seal"):
        names = raw.get(kind, [])
        if (
            not isinstance(names, list)
            or len(names) > _MAX_SUITES
            or not all(
                isinstance(n, str) and 0 < len(n) <= _MAX_SUITE_CHARS for n in names
            )
        ):
            return web.json_response({"error": "moment_suites_invalid"}, status=422)
        out[kind] = sorted(set(names))
    return orjson.dumps(out, option=orjson.OPT_SORT_KEYS).decode()


# ─── Signed wire endpoints ─────────────────────────────────────────────────


class GfsUserRegisterView(GfsBaseView):
    """``POST /gfs/moments/users/register`` — record a public-Momentum opt-in."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)

        user_id = str(body.get("user_id") or "")
        instance_id = str(body.get("instance_id") or "")
        if not user_id or not instance_id:
            return web.json_response({"error": "user_id_required"}, status=422)
        if len(user_id) > MAX_USER_ID_LEN or not user_id.isprintable():
            return web.json_response({"error": "invalid_user_id"}, status=422)
        if instance_id != sender_instance_id:
            # The signing instance must match the registration instance —
            # we don't let one paired SH register a user on behalf of
            # another.
            return web.json_response({"error": "instance_mismatch"}, status=403)
        username = str(body.get("username") or "")
        display_name = str(body.get("display_name") or username or user_id)
        home_pk = str(body.get("home_instance_pk") or "")
        if not home_pk:
            return web.json_response({"error": "home_instance_pk_required"}, status=422)
        picture_url = body.get("picture_url")
        bio_raw = body.get("bio")
        bio = str(bio_raw).strip() if bio_raw else None
        if bio is not None and len(bio) > _MAX_BIO_LEN:
            return web.json_response({"error": "bio_too_long"}, status=422)
        picture_digest_raw = body.get("picture_digest")
        picture_digest = str(picture_digest_raw) if picture_digest_raw else None
        reg = await registry.register_user(
            user_id=user_id,
            instance_id=instance_id,
            username=username,
            display_name=display_name,
            home_instance_pk=home_pk,
            picture_url=str(picture_url) if picture_url else None,
            bio=bio,
            picture_digest=picture_digest,
        )
        return web.json_response(
            {
                "user_id": reg.user_id,
                "registered_at": reg.registered_at,
                "status": reg.status,
            },
            status=201,
        )


class GfsUserDeregisterView(GfsBaseView):
    """``POST /gfs/moments/users/{user_id}/deregister`` — pull a registration."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        _, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        user_id = self.match("user_id")
        existing = await registry.get_registration(user_id)
        if existing is None:
            return web.json_response({"deregistered": False}, status=404)
        if existing.instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        ok = await registry.deregister_user(user_id)
        # v_63: the author's encrypted moment media goes with them.
        await self.svc(K.gfs_moment_media_store_key).purge_author(user_id)
        return web.json_response({"deregistered": ok})


class GfsUserFollowView(GfsBaseView):
    """``POST /gfs/moments/users/{user_id}/follow`` — record a follower."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        followed_user_id = self.match("user_id")
        follower_user_id = str(body.get("follower_user_id") or "")
        follower_instance_id = str(body.get("follower_instance_id") or "")
        if follower_instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        if not follower_user_id:
            return web.json_response({"error": "follower_user_id_required"}, status=422)
        suites = _suites_json(body.get("moment_suites"))
        if isinstance(suites, web.Response):
            return suites
        try:
            follow = await registry.add_follow(
                follower_user_id=follower_user_id,
                follower_instance_id=follower_instance_id,
                followed_user_id=followed_user_id,
                moment_suites=suites,
            )
        except LookupError:
            return web.json_response({"error": "author_not_found"}, status=404)
        # Surface the followed user's directory entry in the response so
        # the follower's instance can cache the home_instance_pk it
        # needs for signature verification on every subsequent moment.
        target = await registry.get_registration(followed_user_id)
        if target is None:  # pragma: no cover — race with deregister
            return web.json_response({"error": "author_not_found"}, status=404)
        return web.json_response(
            {
                "user": {
                    "user_id": target.user_id,
                    "username": target.username,
                    "display_name": target.display_name,
                    "picture_url": target.picture_url,
                    # The same public directory fields
                    # ``GET /gfs/moments/users/{id}`` serves anyone.
                    "picture_digest": target.picture_digest,
                    "bio": target.bio,
                    "home_instance_pk": target.home_instance_pk,
                    "instance_id": target.instance_id,
                },
                "follow": {
                    "follower_user_id": follow.follower_user_id,
                    "followed_user_id": follow.followed_user_id,
                    "created_at": follow.created_at,
                },
            },
            status=201,
        )


class GfsUserUnfollowView(GfsBaseView):
    """``POST /gfs/moments/users/{user_id}/unfollow`` — drop a follower."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        followed_user_id = self.match("user_id")
        follower_user_id = str(body.get("follower_user_id") or "")
        follower_instance_id = str(body.get("follower_instance_id") or "")
        if follower_instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)
        ok = await registry.remove_follow(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
        )
        return web.json_response({"unfollowed": ok})


async def _encrypted_envelope(view: GfsBaseView) -> dict | web.Response:
    """The verified body of a publish / delete — only when it is an
    encrypted envelope from the author's own household (v_62).

    Exactly the routing fields + ciphertext (:data:`ENVELOPE_FIELDS`):
    anything else — a plaintext ``content``, an author name, an older
    household's envelope — is refused ``422 encryption_required`` and
    never fanned out, so a public moment never crosses this server in the
    clear. The signer must be the household the author registered from.
    """
    result = await _rtc_authenticate(
        view, keep_signature=True, max_bytes=_MAX_ENVELOPE_BYTES
    )
    if isinstance(result, web.Response):
        return result
    body, sender = result
    moment_id = body.get("moment_id")
    if (
        set(body) != ENVELOPE_FIELDS
        or not isinstance(body.get("ciphertext"), str)
        or not isinstance(body.get("seal_suite"), str)
        or not isinstance(moment_id, str)
        or not 0 < len(moment_id) <= MAX_MOMENT_ID_CHARS
        or not is_wire_int(body.get("key_gen"))
        or not is_wire_int(body.get("key_step"))
    ):
        return web.json_response({"error": "encryption_required"}, status=422)
    registry = view.svc(K.gfs_moment_public_registry_key)
    if not await registry.is_author_household(
        str(body.get("author_user_id") or ""), sender
    ):
        return web.json_response({"error": "instance_mismatch"}, status=403)
    # An envelope sealed under an earlier day's key step is a re-post of a
    # captured one (an author's household seals under today's): refused, so
    # a replay past the seen-cache window reaches no follower.
    if not envelope_step_current(body["key_gen"], body["key_step"], time.time()):
        return web.json_response({"error": "stale_envelope"}, status=409)
    # One household can't flood the fan-out (or push other authors' moments
    # out of the seen cache): a cluster-wide budget per signing household.
    if not registry.allow_publish(sender):
        resp = web.json_response({"error": "rate_limited"}, status=429)
        resp.headers["Retry-After"] = "60"
        return resp
    return body


class GfsMomentPublicPublishView(GfsBaseView):
    """``POST /gfs/moments/publish`` — fan an encrypted moment to followers."""

    async def post(self) -> web.Response:
        body = await _encrypted_envelope(self)
        if isinstance(body, web.Response):
            return body
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_moment(envelope=body)
        return web.json_response({"delivered": delivered})


class GfsMomentPublicDeleteView(GfsBaseView):
    """``POST /gfs/moments/delete`` — fan an encrypted tombstone."""

    async def post(self) -> web.Response:
        body = await _encrypted_envelope(self)
        if isinstance(body, web.Response):
            return body
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_delete(envelope=body)
        return web.json_response({"delivered": delivered})


class GfsMomentDeleteCertView(GfsBaseView):
    """``POST /gfs/moments/delete_cert`` — keep a delete's signed certificate
    (v_66) for followers offline when the delete went out.

    Signed by the author's household: ``{instance_id, cert}``, where
    ``cert`` is the ``MOMENT_DELETED`` origin certificate
    (:data:`~socialhome.moment_origin.DELETE_CERT_FIELDS`) — checked here
    against the key that household registered with, and again by every
    follower against the key it pinned. Plaintext, but nothing beyond what
    ``/gfs/moments/delete`` already shows this server, save the delete time.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=_MAX_DELETE_CERT_BYTES)
        if isinstance(result, web.Response):
            return result
        body, sender = result
        cert = body.get("cert")
        if set(body) != {"instance_id", "cert"} or not isinstance(cert, dict):
            return web.json_response({"error": "invalid_cert"}, status=422)
        inst = await self.svc(K.gfs_fed_repo_key).get_instance(sender)
        refusal = delete_cert_refusal(
            cert, identity_pk_hex=inst.public_key if inst is not None else ""
        )
        if refusal is not None:
            log.info("moment_public.delete_cert: refused — %s", refusal)
            return web.json_response({"error": "invalid_cert"}, status=422)
        registry = self.svc(K.gfs_moment_public_registry_key)
        author = cert["author_user_id"]
        if cert["origin_instance_id"] != sender or not (
            await registry.is_author_household(author, sender)
        ):
            return web.json_response({"error": "instance_mismatch"}, status=403)
        if not _delete_time_fresh(cert["occurred_at"], time.time()):
            return web.json_response({"error": "stale_cert"}, status=409)
        delete_log = self.svc(K.gfs_moment_delete_log_key)
        if not delete_log.allow_post(sender):
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        outcome = await delete_log.keep(author_user_id=author, cert=cert)
        if outcome == "full":
            return web.json_response({"error": "author_delete_quota"}, status=429)
        return web.json_response({"kept": outcome == "kept"})


def _delete_time_fresh(occurred_at: str, now: float) -> bool:
    """Whether a certificate's signed delete time is now-ish (within
    :data:`DELETE_CERT_MAX_AGE_S` before, :data:`DELETE_CERT_MAX_SKEW_S`
    after)."""
    try:
        when = datetime.fromisoformat(occurred_at)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    at = when.timestamp()
    return now - DELETE_CERT_MAX_AGE_S <= at <= now + DELETE_CERT_MAX_SKEW_S


def _delivery_ok(item: object, sender: str) -> bool:
    """One sealed key in the shape a follower opens — nothing else rides."""
    if not isinstance(item, dict) or set(item) != DELIVERY_FIELDS:
        return False
    sealed = item.get("sealed")
    return (
        item.get("instance_id") == sender
        and isinstance(item.get("to"), str)
        and isinstance(item.get("key_suite"), str)
        and is_wire_int(item.get("key_gen"))
        and is_wire_int(item.get("key_step"))
        and isinstance(sealed, dict)
        and set(sealed) == {"kem_suite", "eph_pk", "ciphertext"}
        and all(isinstance(v, str) for v in sealed.values())
        and len(str(sealed.get("ciphertext"))) <= _MAX_SEALED_KEY_CHARS
    )


class GfsMomentSuitesView(GfsBaseView):
    """``POST /gfs/moments/suites`` — a household declares what it can open
    (v_62) for every follow it holds here. Signed by that household:
    ``{instance_id, moment_suites: {kem, seal}}``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=4096)
        if isinstance(result, web.Response):
            return result
        body, sender = result
        suites = _suites_json(body.get("moment_suites"))
        if isinstance(suites, web.Response):
            return suites
        if suites is None:
            return web.json_response({"error": "moment_suites_invalid"}, status=422)
        registry = self.svc(K.gfs_moment_public_registry_key)
        changed = await registry.declare_suites(
            follower_instance_id=sender, moment_suites=suites
        )
        return web.json_response({"updated": changed})


class GfsMomentKeysView(GfsBaseView):
    """``POST /gfs/moments/keys`` — deliver sealed moment keys (v_62).

    Signed by the author's household. Each item is a delivery the
    follower verifies itself (the author's signature) and opens with its
    own key-wrap key; this server only routes it to the ``to`` household,
    and only while that household follows the author."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=_MAX_KEYS_BYTES)
        if isinstance(result, web.Response):
            return result
        body, sender = result
        deliveries = body.get("deliveries")
        if (
            not isinstance(deliveries, list)
            or not deliveries
            or len(deliveries) > MAX_KEY_DELIVERIES
            or not all(_delivery_ok(d, sender) for d in deliveries)
        ):
            return web.json_response({"error": "invalid_deliveries"}, status=422)
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.deliver_keys(
            sender_instance_id=sender, deliveries=deliveries
        )
        return web.json_response({"delivered": delivered})


class GfsMomentKeyRequestView(GfsBaseView):
    """``POST /gfs/moments/users/{user_id}/key_request`` — a follower
    household asks for the author's current moment key (v_62).

    Signed by the follower household. Always ``202``: whether the author
    is known, followed, online or throttled is not told back."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=4096)
        if isinstance(result, web.Response):
            return result
        body, sender = result
        want = body.get("want_gen")
        registry = self.svc(K.gfs_moment_public_registry_key)
        await registry.request_key(
            follower_instance_id=sender,
            followed_user_id=self.match("user_id"),
            want_gen=want if is_wire_int(want) else 0,
        )
        return web.json_response({"queued": True}, status=202)


class GfsMomentFollowerStatsView(GfsBaseView):
    """``POST /gfs/moments/users/{user_id}/follower_stats`` — counts for the
    author's own sharing settings (v_62): followers, follower households,
    and the households that can't open encrypted moments (sent none).

    Signed by the author's household; any other signer gets 403."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=4096)
        if isinstance(result, web.Response):
            return result
        _body, sender = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        user_id = self.match("user_id")
        if not await registry.is_author_household(user_id, sender):
            return web.json_response({"error": "instance_mismatch"}, status=403)
        return web.json_response(await registry.follower_stats(user_id))


# ─── Public discovery ──────────────────────────────────────────────────────


class GfsUserDirectoryView(GfsBaseView):
    """``GET /gfs/moments/users`` — JSON listing of active registrations.

    Optional query params:
    * ``q`` — substring filter on ``display_name`` / ``username``.
    * ``limit`` — page cap (1..200, default 200).
    """

    async def get(self) -> web.Response:
        registry = self.svc(K.gfs_moment_public_registry_key)
        q = self.request.query.get("q") or None
        try:
            limit = int(self.request.query.get("limit", "200"))
        except ValueError:
            limit = 200
        regs = await registry.list_directory(q=q, limit=limit)
        return web.json_response(
            {
                "users": [_user_dict(r) for r in regs],
                "count": len(regs),
            }
        )


class GfsUserDetailView(GfsBaseView):
    """``GET /gfs/moments/users/{user_id}`` — single-user directory detail JSON."""

    async def get(self) -> web.Response:
        registry = self.svc(K.gfs_moment_public_registry_key)
        user_id = self.match("user_id")
        reg = await registry.get_registration(user_id)
        if reg is None or reg.status != "active":
            return web.json_response({"error": "user_not_found"}, status=404)
        followers = await registry.follower_count(user_id)
        out = _user_dict(reg)
        out["follower_count"] = int(followers)
        return web.json_response(out)


class GfsUserPictureView(GfsBaseView):
    """``/gfs/moments/users/{user_id}/picture`` — avatar bytes.

    * ``POST`` (signed) — instance-side push. Body carries
      ``{mime, digest, bytes_b64}`` plus the standard ``instance_id``
      that ``_rtc_authenticate`` validates.
    * ``GET`` (anon) — public fetch with strong-cache headers so
      directory pages can use ``?v={digest}`` to bust on change.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, sender_instance_id = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        pictures = self.svc(K.gfs_user_picture_repo_key)

        user_id = self.match("user_id")
        reg = await registry.get_registration(user_id)
        if reg is None or reg.status != "active":
            return web.json_response({"error": "user_not_found"}, status=404)
        if reg.instance_id != sender_instance_id:
            return web.json_response({"error": "instance_mismatch"}, status=403)

        mime = str(body.get("mime") or "")
        if mime not in _PICTURE_MIME_ALLOWED:
            return web.json_response({"error": "mime_not_allowed"}, status=422)
        b64 = body.get("bytes_b64")
        if not isinstance(b64, str) or not b64:
            return web.json_response({"error": "bytes_required"}, status=422)
        try:
            raw = base64.b64decode(b64, validate=True)
        except ValueError, TypeError:
            return web.json_response({"error": "bytes_b64_invalid"}, status=422)
        if len(raw) > _MAX_PICTURE_BYTES:
            return web.json_response({"error": "picture_too_large"}, status=413)
        digest = hashlib.sha256(raw).hexdigest()
        claimed = body.get("digest")
        if claimed and claimed != digest:
            return web.json_response({"error": "digest_mismatch"}, status=422)

        await pictures.upsert(
            GfsUserPicture(
                user_id=user_id,
                bytes_=raw,
                mime=mime,
                digest=digest,
                updated_at=int(reg.registered_at),
            )
        )
        await registry.set_picture_digest(user_id=user_id, picture_digest=digest)
        return web.json_response({"digest": digest, "size": len(raw)}, status=201)

    async def get(self) -> web.Response:
        pictures = self.svc(K.gfs_user_picture_repo_key)
        user_id = self.match("user_id")
        pic = await pictures.get(user_id)
        if pic is None:
            return web.Response(status=404)
        return web.Response(
            body=pic.bytes_,
            headers={
                **media_response_headers(pic.mime, "picture"),
                "Cache-Control": "public, max-age=86400, immutable",
                "ETag": f'"{pic.digest}"',
            },
        )


_DIRECTORY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Public Momentum directory</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<link rel="stylesheet" href="/static/users_directory.css">
</head>
<body>
<main id="directory" data-mode="list">
<header><h1>Public Momentum</h1>
<p>People publishing public moments through this Global Federation Server.</p>
<input id="dir-search" type="search" placeholder="Search by name…" autocomplete="off">
</header>
<ul id="dir-list" class="cards"></ul>
<p id="dir-empty" hidden>No registered users yet.</p>
</main>
<script src="/static/users_directory.js"></script>
</body>
</html>"""


_USER_DETAIL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<base href="/">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{display_name} — Public Momentum</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<link rel="stylesheet" href="/static/users_directory.css">
</head>
<body>
<main id="user-detail" data-user="{user_id}" data-mode="detail">
<a class="back" href="/moments">&larr; Directory</a>
<article class="card detail">
  <img class="avatar" alt="" src="{picture_src}">
  <h1>{display_name}</h1>
  <p class="handle">@{username} · {instance_id}</p>
  <p class="bio">{bio}</p>
  <p class="followers"><span id="follower-count">{follower_count}</span> followers</p>
  <a id="follow-cta" class="btn-primary"
     href="https://social-home.io/momentum/follow?gfs={gfs_id}&user={user_id}">
    Follow on your Social Home
  </a>
</article>
</main>
</body>
</html>"""


class GfsUserDirectoryHtmlView(GfsBaseView):
    """``GET /moments`` — anon-browseable directory SPA shell."""

    async def get(self) -> web.Response:
        return html_response(_DIRECTORY_HTML)


class GfsUserDetailHtmlView(GfsBaseView):
    """``GET /moments/{user_id}`` — per-user landing with Follow CTA.

    The hand-off page to the pinned viewer when one is configured
    (:mod:`..link_preview`; the viewer reads
    ``GET /gfs/pages/people/{user_id}``), else rendered here."""

    async def get(self) -> web.Response:
        user_id = self.match("user_id")
        viewer = self.svc(K.gfs_public_viewer_key)
        target = viewer.person(user_id) if viewer is not None else None
        if viewer is not None and target is not None:
            return await person_handoff(self.request.app, viewer, user_id, target)
        page = await load_person_page(self.request.app, user_id)
        if page is None:
            return web.Response(status=404, text="User not found")
        reg, followers = page.registration, page.follower_count
        cfg = self.request.app[K.gfs_config_key]
        gfs_id = cfg.instance_id
        picture_src = (
            f"/gfs/moments/users/{user_id}/picture?v={reg.picture_digest}"
            if reg.picture_digest
            else "/static/avatar_placeholder.svg"
        )
        bio = reg.bio or ""
        body = _USER_DETAIL_HTML.format(
            user_id=_html_escape(user_id),
            username=_html_escape(reg.username),
            display_name=_html_escape(reg.display_name),
            instance_id=_html_escape(reg.instance_id),
            picture_src=_html_escape(picture_src),
            bio=_html_escape(bio),
            follower_count=int(followers),
            gfs_id=_html_escape(str(gfs_id)),
        )
        return html_response(body)


def _user_dict(r) -> dict:
    return {
        "user_id": r.user_id,
        "instance_id": r.instance_id,
        "username": r.username,
        "display_name": r.display_name,
        "picture_url": r.picture_url,
        "picture_digest": r.picture_digest,
        "bio": r.bio,
        "home_instance_pk": r.home_instance_pk,
        "registered_at": r.registered_at,
    }


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─── Encrypted moment media (v_63) ─────────────────────────────────────────


#: Headers a browser on another origin (the pinned share-link viewer) may
#: read a blob with: anonymous, no credentials, cacheable forever (the
#: address is the content's hash).
_MEDIA_HEADERS = {
    "Content-Type": "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=604800, immutable",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


async def _media_uploader(
    view: GfsBaseView, blob_id: str
) -> tuple[str, int] | web.Response:
    """Header-signed upload: ``X-SH-Instance`` / ``-Author`` / ``-Expires`` /
    ``-Timestamp`` / ``-Signature`` over the canonical
    ``{instance_id, blob_id, author_user_id, expires_at, ts}`` — the body is
    the raw blob, so the signature can't ride inside it (same scheme as the
    relay stream). The signer must be the household the author registered
    from. Returns ``(author_user_id, expires_at)``."""
    h = view.request.headers
    instance_id = h.get("X-SH-Instance", "")
    author = h.get("X-SH-Author", "")
    signature = h.get("X-SH-Signature", "")
    try:
        ts = int(h.get("X-SH-Timestamp", ""))
        expires_at = int(h.get("X-SH-Expires", ""))
    except ValueError:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    if not instance_id or not author or not signature:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    if abs(int(time.time()) - ts) > RELAY_AUTH_MAX_SKEW_SECONDS:
        return web.json_response({"error": "stale_timestamp"}, status=401)
    sender = await view.svc(K.gfs_fed_repo_key).get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    body = {
        "instance_id": instance_id,
        "blob_id": blob_id,
        "author_user_id": author,
        "expires_at": expires_at,
        "ts": ts,
    }
    if not verify_report_signature(body, signature, sender.public_key):
        return web.json_response({"error": "invalid_signature"}, status=401)
    registry = view.svc(K.gfs_moment_public_registry_key)
    if not await registry.is_author_household(author, instance_id):
        return web.json_response({"error": "instance_mismatch"}, status=403)
    return author, expires_at


class GfsMomentMediaView(GfsBaseView):
    """``/gfs/moments/media/{blob_id}`` — encrypted moment media (v_63).

    * ``PUT`` — the author's household uploads one blob (header-signed).
    * ``GET`` — anyone fetches it by address: ciphertext only, readable
      cross-origin without credentials (the share-link viewer)."""

    async def put(self) -> web.Response:
        blob_id = self.match("blob_id")
        who = await _media_uploader(self, blob_id)
        if isinstance(who, web.Response):
            return who
        author, expires_at = who
        data = await self.bounded_raw(MAX_BLOB_BYTES)
        try:
            stored = await self.svc(K.gfs_moment_media_store_key).put(
                blob_id=blob_id,
                author_user_id=author,
                data=data,
                expires_at=expires_at,
            )
        except MediaRefused as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        return web.json_response({"stored": stored}, status=201 if stored else 200)

    async def get(self) -> web.Response:
        data = await self.svc(K.gfs_moment_media_store_key).read(self.match("blob_id"))
        if data is None:
            return web.Response(
                status=404, headers={"Access-Control-Allow-Origin": "*"}
            )
        return web.Response(body=data, headers=_MEDIA_HEADERS)


class GfsMomentMediaDeleteView(GfsBaseView):
    """``POST /gfs/moments/media/{blob_id}/delete`` — the author's household
    withdraws a blob (the moment was deleted). Signed JSON body
    ``{instance_id, author_user_id, blob_id}``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self, max_bytes=4096)
        if isinstance(result, web.Response):
            return result
        body, sender = result
        blob_id = self.match("blob_id")
        author = str(body.get("author_user_id") or "")
        if body.get("blob_id") != blob_id:
            return web.json_response({"error": "blob_id_mismatch"}, status=422)
        registry = self.svc(K.gfs_moment_public_registry_key)
        if not await registry.is_author_household(author, sender):
            return web.json_response({"error": "instance_mismatch"}, status=403)
        deleted = await self.svc(K.gfs_moment_media_store_key).delete(
            blob_id, author_user_id=author
        )
        return web.json_response({"deleted": deleted})

"""GFS public-Momentum routes (§Momentum-public).

Three surfaces:

* **Signed wire endpoints** for instance-side mutations:
  - ``POST /gfs/moments/users/register`` — opt a user into the directory.
  - ``POST /gfs/moments/users/{user_id}/deregister`` — remove a registration.
  - ``POST /gfs/moments/users/{user_id}/picture`` — push avatar bytes.
  - ``POST /gfs/moments/users/{user_id}/follow`` — record a follower.
  - ``POST /gfs/moments/users/{user_id}/unfollow`` — drop a follower.
  - ``POST /gfs/moments/publish`` — fan out a signed moment envelope.
  - ``POST /gfs/moments/delete`` — fan out a tombstone.
* **Public discovery** ``GET /gfs/moments/users`` (JSON, w/ ``?q=``),
  ``GET /gfs/moments/users/{user_id}`` (per-user JSON),
  ``GET /gfs/moments/users/{user_id}/picture`` (avatar bytes).
* **Public landing** ``GET /moments`` and ``GET /moments/{user_id}``
  (HTML SPA shells) — anon-browseable directory + per-user pages.

All signed endpoints reuse :func:`_rtc_authenticate` so the same
middleware that gates the Highlights publish flow gates these.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from aiohttp import web

from ...csp import media_response_headers
from .. import app_keys as K
from ..domain import GfsUserPicture
from ..html_page import html_response
from ..safe_embed import script_json
from .base import GfsBaseView
from .rtc import _rtc_authenticate

log = logging.getLogger(__name__)

#: Cap for inbound picture bytes (post-resize). 256 KiB matches the
#: SH-side avatar pipeline; reject larger uploads outright.
_MAX_PICTURE_BYTES = 256 * 1024

#: MIME types accepted for avatar upload.
_PICTURE_MIME_ALLOWED = frozenset({"image/jpeg", "image/png", "image/webp"})

#: Bio length cap. Twitter-shaped — short enough to render in a card.
_MAX_BIO_LEN = 280


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
        try:
            follow = await registry.add_follow(
                follower_user_id=follower_user_id,
                follower_instance_id=follower_instance_id,
                followed_user_id=followed_user_id,
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


class GfsMomentPublicPublishView(GfsBaseView):
    """``POST /gfs/moments/publish`` — fan a signed moment to followers."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _sender = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_moment(envelope=body)
        return web.json_response({"delivered": delivered})


class GfsMomentPublicDeleteView(GfsBaseView):
    """``POST /gfs/moments/delete`` — fan a tombstone."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _sender = result
        registry = self.svc(K.gfs_moment_public_registry_key)
        delivered = await registry.fan_out_delete(envelope=body)
        return web.json_response({"delivered": delivered})


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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
  href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Fraunces:opsz,wght,SOFT,WONK@9..144,400..900,0..100,0..1&display=swap">
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
  href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Fraunces:opsz,wght,SOFT,WONK@9..144,400..900,0..100,0..1&display=swap">
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
<section class="moments-index" aria-label="Current moments">
  <h2>Current moments</h2>
  <div id="moments-root"></div>
</section>
</main>
<script id="moments-boot" type="application/json">{boot_json}</script>
<script type="module" src="/static/moment_public_viewer.js"></script>
</body>
</html>"""


class GfsUserDirectoryHtmlView(GfsBaseView):
    """``GET /moments`` — anon-browseable directory SPA shell."""

    async def get(self) -> web.Response:
        return html_response(_DIRECTORY_HTML)


class GfsUserDetailHtmlView(GfsBaseView):
    """``GET /moments/{user_id}`` — per-user landing with Follow CTA."""

    async def get(self) -> web.Response:
        registry = self.svc(K.gfs_moment_public_registry_key)
        user_id = self.match("user_id")
        reg = await registry.get_registration(user_id)
        if reg is None or reg.status != "active":
            return web.Response(status=404, text="User not found")
        followers = await registry.follower_count(user_id)
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
            # ``script_json`` so a registration's user_id / instance_id
            # containing ``</script>`` can't break out of the inline
            # <script> block — stored XSS on this anonymous page.
            boot_json=script_json({"userId": user_id, "instanceId": reg.instance_id}),
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

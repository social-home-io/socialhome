"""What the GFS's public pages show — one loader per page, two renderers.

The space page, the invite landing and a person's Momentum profile are
rendered either by the pinned viewer from this server's JSON
(:mod:`.routes.viewer_pages`) or, when no viewer is configured, by this
server itself (:mod:`.public`, :mod:`.routes.moments_public`). Both read
through these loaders, so "what is public about a space / an invite / a
person" — the listed-and-not-withdrawn filter, the live-invite check, the
active registration — has exactly one place it could go wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiohttp import web

from . import app_keys as K
from .domain import GfsUserRegistration
from .invites import build_invite_code

#: A stored image a public page may show: a base64 ``data:`` URI of a raster
#: type. An ``https://`` URL (or an SVG, which can carry script) is never
#: passed on — not to the viewer, not into a link preview.
_IMAGE_DATA_URI_RE = re.compile(
    r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}"
)


def image_data_uri(value: str | None) -> str | None:
    """``value`` if it is a raster ``data:`` image a public page may show."""
    if value and _IMAGE_DATA_URI_RE.fullmatch(value):
        return value
    return None


async def server_name(app: web.Application) -> str:
    """The operator's server name (admin portal override, else config)."""
    cfg = app[K.gfs_config_key]
    return (await app[K.gfs_admin_repo_key].get_config("server_name")) or (
        cfg.server_name
    )


async def load_space_page(app: web.Application, space_id: str) -> dict | None:
    """The directory metadata of a LISTED space, or ``None``.

    Owner-withdrawn rows are hidden like a non-active status — the same
    discovery surface the listing and detail routes filter (``hide_space``).
    """
    space = await app[K.gfs_fed_repo_key].get_space(space_id)
    if space is None or space.status != "active" or space.withdrawn:
        return None
    return {
        "space_id": space.space_id,
        "name": space.name,
        "description": space.description,
        "about_markdown": space.about_markdown,
        "cover_url": space.cover_url,
        "icon_url": space.icon_url,
        "accent_color": space.accent_color,
        "primary_color": space.primary_color,
        "subscriber_count": space.subscriber_count,
        "category": space.category,
    }


async def load_invite_page(app: web.Application, token: str) -> dict | None:
    """A live invite of a listed space, or ``None``.

    A pure READ through :class:`~.invites.GfsInviteService` — nothing is
    written, counted or logged (see ``handle_invite_page``). ``None`` for an
    unknown, expired or revoked token and for a space that is no longer
    listed: one answer for all, so a prober can't tell which tokens once
    existed.
    """
    invite = await app[K.gfs_invite_service_key].get_live(token)
    if invite is None:
        return None
    space = await app[K.gfs_fed_repo_key].get_space(invite.space_id)
    if space is None or space.status != "active" or space.withdrawn:
        return None
    # Directory metadata only — all of it already public on the space page.
    return {
        "space_id": space.space_id,
        "name": space.name,
        "accent_color": space.accent_color,
        "icon_url": space.icon_url,
        "invite_code": build_invite_code(invite.blob),
    }


@dataclass(slots=True, frozen=True)
class PersonPage:
    """An active directory registration and its follower count."""

    registration: GfsUserRegistration
    follower_count: int


async def load_person_page(app: web.Application, user_id: str) -> PersonPage | None:
    """The person's ACTIVE registration in the Momentum directory, or ``None``."""
    registry = app[K.gfs_moment_public_registry_key]
    reg = await registry.get_registration(user_id)
    if reg is None or reg.status != "active":
        return None
    return PersonPage(
        registration=reg, follower_count=int(await registry.follower_count(user_id))
    )

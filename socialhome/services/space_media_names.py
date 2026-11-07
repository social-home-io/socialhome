"""What a space file another household sent is called here.

A space file is addressed by name, and a received file is write-once. If a
received row kept the name its sender used, the first household to push
bytes under that name — any member that learned it from the row — would
decide what every row naming it shows (and what this household re-seals
for its link-joined readers through the GFS media cache). So a received
name is never used as is: a file *F* that household *H* delivers — in a row
it sends, or as ``SPACE_MEDIA_BLOB`` bytes — is stored and referenced as
:func:`~socialhome.services.inbound_media_store.space_direct_media_name`
``(space, H, F)``. *H* is the household this one authenticated (the
envelope's sender, a ``SPACE_ROUTED`` inner's origin, the §25.6 provider).
Another household naming *F* reaches only its own copy.

A row this household already holds keeps the files it has when the
incoming name is the same file: the same name (a household relaying a file
it got from the same origin calls it the same), the sender's name mapped
(the same household again), or this household's own file as another
household calls it (``space_direct_media_name(space, us, F)`` — a
collaborator's edit of our event that kept its cover).

GFS descriptors (v_80, :class:`SpaceMediaGfsFetcher`) name the files a
link-joined reader fetches from a GFS instead; they take precedence
(:meth:`SpaceMediaNamer.combine`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..domain.space_media_ref import SpaceMediaLocalization
from .inbound_media_store import (
    is_reserved_media_name,
    media_basename,
    space_direct_media_name,
)


class SpaceMediaNamer:
    """Maps a received space item's file names to this household's names."""

    __slots__ = ("_own",)

    def __init__(self, own_instance_id: Callable[[], str] | str = "") -> None:
        self._own = own_instance_id

    def own_instance_id(self) -> str:
        own = self._own
        return str((own() if callable(own) else own) or "")

    def localize(
        self,
        space_id: str,
        origin: str,
        urls: Iterable[str | None],
        *,
        held: Iterable[str | None] = (),
    ) -> SpaceMediaLocalization:
        """This household's names for the files *urls* of an item household
        *origin* delivered, given the files the stored row already shows
        (*held*; empty for a new row). A reserved name (DM / moment / GFS
        media) is left alone — the sanitizers drop it."""
        if not space_id or not origin:
            return SpaceMediaLocalization()
        held_names = {n for n in (media_basename(u) for u in held) if n}
        own = self.own_instance_id()
        own_names = (
            {space_direct_media_name(space_id, own, h): h for h in held_names}
            if own
            else {}
        )
        names: dict[str, str] = {}
        for url in urls:
            name = media_basename(url)
            if not name or name in names or is_reserved_media_name(name):
                continue
            if name in held_names:
                continue  # the same file, by the name it has here
            names[name] = own_names.get(name) or space_direct_media_name(
                space_id, origin, name
            )
        return SpaceMediaLocalization(names=names)

    @staticmethod
    def combine(
        direct: SpaceMediaLocalization, gfs: SpaceMediaLocalization
    ) -> SpaceMediaLocalization:
        """*direct* names under the GFS ones (a described file is fetched
        from a GFS under its reader name), with the GFS rows to keep."""
        return SpaceMediaLocalization(
            names={**direct.names, **gfs.names}, refs=gfs.refs
        )


def same_file(held: object, sent: object, *, space_id: str, origin: str) -> bool:
    """Whether the reference this household holds (*held*) is the file
    household *origin* names *sent*: the same reference, or *sent* under
    the name it takes here as *origin* delivers it. Both empty is the same
    (no file)."""
    if held == sent or (not held and not sent):
        return True
    if not isinstance(held, str) or not isinstance(sent, str) or not held or not sent:
        return False
    name = media_basename(sent)
    return bool(name) and media_basename(held) == space_direct_media_name(
        space_id, origin, name
    )


def feature_media_urls(feature: str, payload: dict) -> list[str]:
    """Every local media file a moderation queue payload of *feature* (or
    the matching wire event / held row) references."""
    match feature:
        case "posts":
            return _post_media_urls(payload)
        case "calendar":
            return _calendar_cover_urls(payload)
        case _:
            return []


def renamed_media(
    feature: str, payload: dict, rename: Callable[[str | None], str | None]
) -> dict:
    """*payload* with every file :func:`feature_media_urls` lists passed through
    *rename* (same shape, a new dict)."""

    def one(value: object) -> object:
        return rename(value) if isinstance(value, str) and value else value

    def many(values: object) -> object:
        return [one(v) for v in values] if isinstance(values, list | tuple) else values

    out = dict(payload)
    match feature:
        case "posts":
            if "media_url" in out:
                out["media_url"] = one(out["media_url"])
            if "image_urls" in out:
                out["image_urls"] = many(out["image_urls"])
            for key, field in (("file_meta", "url"), ("link_preview", "thumbnail_url")):
                inner = out.get(key)
                if isinstance(inner, dict) and field in inner:
                    out[key] = {**inner, field: one(inner[field])}
            attachments = out.get("attachments")
            if isinstance(attachments, dict) and isinstance(
                attachments.get("bazaar"), dict
            ):
                bazaar = attachments["bazaar"]
                out["attachments"] = {
                    **attachments,
                    "bazaar": {**bazaar, "image_urls": many(bazaar.get("image_urls"))},
                }
        case "calendar":
            patch = out.get("patch")
            if isinstance(patch, dict):
                if "cover_url" in patch:
                    out["patch"] = {**patch, "cover_url": one(patch["cover_url"])}
            elif "cover_url" in out:
                out["cover_url"] = one(out["cover_url"])
    return out


def _calendar_cover_urls(payload: dict) -> list[str]:
    """The cover a queued calendar create (``cover_url``) or edit
    (``patch.cover_url``) sets — a cleared or unchanged cover sends none."""
    patch = payload.get("patch")
    url = (
        patch.get("cover_url") if isinstance(patch, dict) else payload.get("cover_url")
    )
    return [url] if isinstance(url, str) and url else []


def _post_media_urls(payload: dict) -> list[str]:
    """Every local media file a queued post (and its listing) references."""
    urls: list[str] = []
    for url in (payload.get("media_url"), *(payload.get("image_urls") or ())):
        if isinstance(url, str) and url:
            urls.append(url)
    fm = payload.get("file_meta")
    if isinstance(fm, dict) and isinstance(fm.get("url"), str) and fm["url"]:
        urls.append(fm["url"])
    lp = payload.get("link_preview")
    if (
        isinstance(lp, dict)
        and isinstance(lp.get("thumbnail_url"), str)
        and lp["thumbnail_url"]
    ):
        urls.append(lp["thumbnail_url"])
    bazaar = (payload.get("attachments") or {}).get("bazaar")
    if isinstance(bazaar, dict):
        for url in bazaar.get("image_urls") or ():
            if isinstance(url, str) and url:
                urls.append(url)
    return urls

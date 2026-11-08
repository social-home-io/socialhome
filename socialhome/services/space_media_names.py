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

**Hashed files (v_85).** An item that carries a valid ``files`` map
(:mod:`socialhome.domain.file_hashes`) names each hashed file by its
content instead: ``space_content_media_name(space, h)`` — whatever is
stored under it hashes to ``h``, so it needs no sender binding, and an
item naming another item's file name with other bytes only ever reaches
its own. A raw ``sc-`` name is never kept as is: it is re-derived from the
hash the item claims, or (unhashed) rebound to its sender like any name.

GFS descriptors (v_80, :class:`SpaceMediaGfsFetcher`) name the files a
link-joined reader fetches from a GFS instead; they take precedence
(:meth:`SpaceMediaNamer.combine`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from ..domain.file_hashes import (
    FileHash,
    is_space_content_media_name,
    space_content_media_name,
)
from ..domain.space_media_ref import (
    SpaceMediaLocalization,
    page_media_urls,
    rewrite_page_body_media,
)
from .inbound_media_store import (
    MEDIA_REF_PREFIX,
    is_reserved_media_name,
    local_media_ref,
    media_basename,
    space_direct_media_name,
)


class _ConfinedFiles(Protocol):
    async def confined_to_space(self, basename: str, space_id: str) -> bool: ...


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
        hashes: Mapping[str, FileHash] | None = None,
    ) -> SpaceMediaLocalization:
        """This household's names for the files *urls* of an item household
        *origin* delivered, given the files the stored row already shows
        (*held*; empty for a new row) and the item's valid hash map
        (*hashes*, by the sender's file names — v_85). A reserved name (DM /
        moment / GFS media) is left alone — the sanitizers drop it.

        Per file, first match: a file the row holds keeps its name; our own
        file as the sender calls it keeps ours; a file the row holds under
        the sender's name keeps it; a hashed file takes its content name;
        anything else its sender-bound name."""
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
            direct = space_direct_media_name(space_id, origin, name)
            fh = hashes.get(name) if hashes else None
            names[name] = (
                own_names.get(name)
                # A legacy row keeps the file it holds from this sender —
                # an edit that now carries hashes never re-points it.
                or (direct if direct in held_names else None)
                or (space_content_media_name(space_id, fh.h, name) if fh else None)
                or direct
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


def same_file(
    held: object,
    sent: object,
    *,
    space_id: str,
    origin: str,
    hashes: Mapping[str, FileHash] | None = None,
) -> bool:
    """Whether the reference this household holds (*held*) is the file
    household *origin* names *sent*: the same reference, or *sent* under
    the name it takes here as *origin* delivers it (its content name when
    *hashes* — the item's valid hash map — covers it). Both empty is the
    same (no file)."""
    if held == sent or (not held and not sent):
        return True
    if not isinstance(held, str) or not isinstance(sent, str) or not held or not sent:
        return False
    name = media_basename(sent)
    if not name:
        return False
    fh = hashes.get(name) if hashes else None
    if fh is not None:
        return media_basename(held) == space_content_media_name(space_id, fh.h, name)
    return media_basename(held) == space_direct_media_name(space_id, origin, name)


def comment_media_ref(
    space_id: str, value: object, hashes: Mapping[str, FileHash] | None
) -> str | None:
    """A received space comment's picture as stored here: its content
    name when the comment's valid hash map (v_85) covers it, else the
    reference as sent (comments keep their sender's name, as before v_85) —
    never a raw ``sc-`` name, which only a hash may produce."""
    ref = local_media_ref(value)
    if ref is None:
        return None
    name = ref[len(MEDIA_REF_PREFIX) :]
    fh = hashes.get(name) if hashes else None
    if fh is not None:
        return MEDIA_REF_PREFIX + space_content_media_name(space_id, fh.h, name)
    return None if is_space_content_media_name(name) else ref


async def space_own_files(
    media_refs: _ConfinedFiles | None, space_id: str, urls: Iterable[str]
) -> list[str]:
    """The files of *urls* that are *space_id*'s own content here — no row
    outside the space names them (``confined_to_space``). A space page's
    names are its host's (v_82): what the host ships must never be a
    household file, a DM's or another space's. No repo: none (fail
    closed)."""
    if media_refs is None:
        return []
    return [
        u
        for u in urls
        if await media_refs.confined_to_space(media_basename(u), space_id)
    ]


def feature_media_urls(feature: str, payload: dict) -> list[str]:
    """Every local media file a moderation queue payload of *feature* (or
    the matching wire event / held row) references."""
    match feature:
        case "posts":
            return _post_media_urls(payload)
        case "calendar":
            return _calendar_cover_urls(payload)
        case "pages":
            return _page_media_urls(payload)
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
        case "pages":
            patch = out.get("patch")
            if isinstance(patch, dict):
                out["patch"] = _renamed_page_fields(patch, rename)
            else:
                out = _renamed_page_fields(out, rename)
    return out


def _renamed_page_fields(
    fields: dict, rename: Callable[[str | None], str | None]
) -> dict:
    """*fields* (a page create, an edit's patch, a resolution, the wire
    event) with its cover and every body picture renamed — a body
    reference renamed to nothing is emptied (a new dict)."""
    out = dict(fields)
    cover = out.get("cover_image_url")
    if isinstance(cover, str) and cover:
        out["cover_image_url"] = rename(cover)
    for key in _PAGE_BODY_KEYS:
        text = out.get(key)
        if isinstance(text, str) and text:
            out[key] = rewrite_page_body_media(text, lambda u: rename(u) or "")
    return out


#: The markdown bodies a page payload carries: a create's / edit's
#: ``content``, a merged conflict resolution's ``merged_content``.
_PAGE_BODY_KEYS: tuple[str, ...] = ("content", "merged_content")


def _page_media_urls(payload: dict) -> list[str]:
    """The files a queued page create (or its wire event / held row), an
    edit's ``patch`` or a merged resolution shows: the cover, then each
    body picture."""
    patch = payload.get("patch")
    fields = patch if isinstance(patch, dict) else payload
    urls = page_media_urls(fields.get("cover_image_url"), fields.get("content"))
    for url in page_media_urls(None, fields.get("merged_content")):
        if url not in urls:
            urls.append(url)
    return urls


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

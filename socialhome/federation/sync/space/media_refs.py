"""§25.6 records that show files — where a v_80 stream to a member seated
from an invite link adds the ``media_refs`` descriptor (the record's files
in a GFS media cache), and where the receiver reads it back.

Pure — no I/O.
"""

from __future__ import annotations

from typing import Any

#: The resources whose records carry ``media_refs`` toward a link-joined
#: v_80 member: posts (pictures, file, link card), calendar events (the
#: cover) and gallery items (thumbnail + full file).
MEDIA_REF_RESOURCES: frozenset[str] = frozenset({"posts", "calendar", "gallery"})


def _str(value: object) -> list[str]:
    return [value] if isinstance(value, str) and value else []


def record_media_urls(resource: str, record: dict[str, Any]) -> list[str]:
    """The local file references a record of *resource* shows, in order."""
    if resource == "posts":
        urls = _str(record.get("media_url"))
        images = record.get("image_urls")
        if isinstance(images, list):
            for url in images:
                urls.extend(_str(url))
        file_meta = record.get("file_meta")
        if isinstance(file_meta, dict):
            urls.extend(_str(file_meta.get("url")))
        preview = record.get("link_preview")
        if isinstance(preview, dict):
            urls.extend(_str(preview.get("thumbnail_url")))
        return urls
    if resource == "calendar":
        return _str(record.get("cover_url"))
    if resource == "gallery" and record.get("kind") == "item":
        return _str(record.get("thumbnail_url")) + _str(record.get("url"))
    return []

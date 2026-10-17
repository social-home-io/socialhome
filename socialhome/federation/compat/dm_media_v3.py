"""Sub-v_3 fallback for DM media (image / video / file).

§319 paragraph 5 policy: **fallback**. A v_2 receiver doesn't know
how to render an ``image`` / ``video`` / ``file`` message and doesn't
expect the ``DM_MEDIA_BLOB`` follow-up event to land. Rather than
silently dropping the send (``skip``) or refusing the operation
(``force-upgrade``), we degrade the wire shape to a normal
``type='text'`` message whose content names the file the sender
intended to share — the receiver sees a useful "📎 cat.jpg — sender
needs to upgrade…" line in the thread and can ask the sender to
re-send once they're current.

User-facing consequence:

* **Sender on v_3** → recipient on v_2: the recipient's thread shows
  a single text bubble of the form "📎 ``<file_name>`` — your peer's
  household needs to update before they can share media." The
  sender's own bubble renders normally (image / video / file). The
  send isn't blocked; the operator's UI doesn't surface anything
  beyond the natural "peer behind" notice from the admin compat
  panel (issue #319 paragraph 1).
* **Sender on v_2** → recipient on v_3: the recipient knows the
  sender can't ship media yet — no rewrite happens here because
  v_2 senders never produce media events in the first place.

This file is the canonical reference shape for a fallback shim — see
:mod:`socialhome.federation.compat` for the discipline + how to add
a new one when v_4 ships.
"""

from __future__ import annotations

import logging

from ...domain.federation import FederationEventType
from ...domain.federation_capabilities import FederationCapability
from . import register

log = logging.getLogger(__name__)


#: Set of message ``type`` values that this transform considers
#: "media". A v_3 sender producing one of these types must either
#: degrade to text (this shim) or skip the send (caller's choice when
#: ``transform_for_peer`` returns the text shape).
_MEDIA_TYPES: frozenset[str] = frozenset({"image", "video", "file"})


def _text_fallback(payload: dict) -> dict:
    """Produce the v_(N-1)-shaped equivalent of a media ``DM_MESSAGE``.

    The returned dict is what the sub-v_3 peer's ``_on_dm_message``
    handler will store: a regular text message with no media fields,
    whose body explains why the media isn't visible. Stripping the
    media fields (rather than nulling them) keeps the receiver-side
    payload exactly v_2-shaped — no unknown keys to log-warn about.
    """
    file_name = payload.get("file_name") or "attachment"
    # User-facing copy. Kept neutral / non-blaming — most "outdated
    # peer" cases are operator-side update lag, not user neglect.
    body = f"📎 {file_name} — your peer's household needs to update to share media."
    fallback = dict(payload)
    fallback["type"] = "text"
    fallback["content"] = body
    # Strip every v_3 media field so the receiver's strict-shape
    # check (if any) sees a clean v_2 envelope.
    for key in (
        "media_url",
        "file_name",
        "mime_type",
        "file_size_bytes",
        "media_blob_id",
        "preview_bytes_b64",
    ):
        fallback.pop(key, None)
    return fallback


def transform(*, payload: dict, peer_version: int) -> dict | None:
    """v_3 → v_2 fallback for ``DM_MESSAGE`` payloads carrying media.

    Non-media ``DM_MESSAGE`` payloads (``type='text'`` /
    ``'transcript'`` / ``'location'``) pass through untouched at any
    peer version — they're already shape-compatible with v_2. Media
    payloads going to a sub-v_3 peer get the synthesised text body.
    """
    msg_type = str(payload.get("type") or "text")
    if msg_type not in _MEDIA_TYPES:
        return payload
    if peer_version >= FederationCapability.MIN_FOR_DM_MEDIA_SYNC:
        return payload
    log.info(
        "dm_media_v3: degrading type=%s to text for peer at proto_version=%d",
        msg_type,
        peer_version,
    )
    return _text_fallback(payload)


# Module-load-time registration. Imported by
# :func:`socialhome.federation.compat._auto_register`.
register(FederationEventType.DM_MESSAGE, transform)

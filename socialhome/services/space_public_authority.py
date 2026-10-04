"""Authority kinds on the host relay (``space_post_public``).

The host relay carries an author-signed post inner
(:mod:`socialhome.services.space_public_author`): the space-authority
signature on the envelope proves a seed holder relayed it, the author
signature inside proves who wrote it. Two moderation outcomes ride it too:

* a **removal** (:class:`~socialhome.domain.space_item.AuthorityRemoval`) —
  a post or comment removed on the host path, by a moderator, an admin, the
  owner or the author. It names the item (and its author, for the tombstone
  rule) — never who removed it or why. It has no author signature; the
  authority signature is its only authorizer. :func:`is_authority_inner`
  tells it apart: an inner WITHOUT ``author_sig`` can only be a removal.
* an **approved post** — a post released from the moderation queue of a
  ``MODERATED`` space. Its author's household signs the post inner when it
  SUBMITS the item (``created_at`` = the submission time; the queue keeps
  that signed copy as ``public_relay``), so the author signature still
  proves authorship to followers — a seed holder cannot forge it. The seed
  holder that applies the approved item checks that the signed copy is
  exactly the post it publishes (:func:`approved_relay_for`), and relays it
  with the ``approved_post`` mark: outside the author signature, under the
  authority signature. The mark is what lets a follower accept the
  ``comment``-scope writer cert a plain member of a ``MODERATED`` space
  holds. A seed holder never relays a member's own inner that already names
  an authority kind, so only the authority sets the mark.

Pure module — no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..crypto import derive_instance_id
from ..domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    PAD_FIELD,
)
from .space_public_author import verify_signed_author_inner
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.post import Post

#: Fields an approved relay drops from the author's signed copy: the link
#: card (its own author signature covers a card the reviewers may not have
#: seen in that form — the post itself is enough), any padding and any cert
#: (the seed holder re-stamps the cert for the relay epoch).
_DROPPED_FIELDS: frozenset[str] = frozenset(
    {
        "link_preview",
        "link_preview_sig",
        "link_preview_sig_suite",
        PAD_FIELD,
        WRITER_CERT_FIELD,
    }
)


def is_authority_inner(inner: dict) -> bool:
    """True when ``inner`` carries no author signature — only a removal may
    look like that. An inner WITH one is a post, whatever else it claims."""
    return inner.get("author_sig") is None


def is_approved(inner: dict) -> bool:
    """True when a seed holder marked this author-signed inner as released
    from the moderation queue."""
    return inner.get(AUTHORITY_KIND_FIELD) == AUTHORITY_KIND_APPROVED_POST


def _post_fields(post: "Post") -> dict:
    """The content fields of ``post`` as the author's signed inner carries
    them (``space_public_author.build_signed_author_inner``)."""
    return {
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "image_urls": list(post.image_urls),
        "hidden_from_feed": post.hidden_from_feed,
        "location": (
            {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
            if post.location is not None
            else None
        ),
    }


def approved_relay_for(relay: object, *, post: "Post", space_id: str) -> dict | None:
    """The inner to relay for ``post`` — just released from the moderation
    queue of ``space_id`` — from its author's signed copy ``relay``, marked
    ``approved_post``; ``None`` unless ``relay`` is a valid author-signed
    inner of exactly this post: its author signature and self-cert verify,
    it names no authority kind already, it is for this space, this post id
    and this author, its origin is the author key's household, and every
    content field equals the post being published (so reviewers approved
    precisely what followers will see)."""
    if not isinstance(relay, dict) or AUTHORITY_KIND_FIELD in relay:
        return None
    if not verify_signed_author_inner(relay):
        return None
    if (
        relay.get("space_id") != space_id
        or relay.get("post_id") != post.id
        or relay.get("author_user_id") != post.author
    ):
        return None
    try:
        origin = derive_instance_id(bytes.fromhex(str(relay.get("author_pk") or "")))
    except ValueError:
        return None
    if origin != relay.get("origin_instance_id"):
        return None
    if any(relay.get(k) != v for k, v in _post_fields(post).items()):
        return None
    out = {k: v for k, v in relay.items() if k not in _DROPPED_FIELDS}
    out[AUTHORITY_KIND_FIELD] = AUTHORITY_KIND_APPROVED_POST
    return out

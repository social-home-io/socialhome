"""The descriptor a page proposal carries for its new files (v_82).

A member household seated from an invite link reaches the space's host only
through the GFS relay, which refuses ``SPACE_MEDIA_BLOB`` bytes. Its page
proposal therefore carries its new files as a ``media_refs`` descriptor
(:mod:`socialhome.domain.space_media_ref`): the proposer seals each file
once and uploads it to its GFS media cache
(:class:`~.space_media_gfs_uploader.SpaceMediaGfsUploader`, authorized as a
space writer), and the host fetches it
(:class:`~.space_media_gfs_fetcher.SpaceMediaGfsFetcher`), checks the
scope-bound address, decrypts — the blob's AAD names the page and the
proposer's household — and stores it under the proposer's sender-bound
``sd-`` name.

The descriptor is signed by the proposer's household identity key, like
the author-signed descriptor a follower takes (``media_refs_sig``), bound
to the space, the page, the proposing household and the proposal as sent
(its version hash): a descriptor lifted from another proposal, another
page or another household never verifies, and an unsigned one is dropped.
The host verifies it against the key it pinned for that household.
Suite-tagged (``media_refs_sig_suite``); an unknown suite is refused, never
defaulted.
"""

from __future__ import annotations

from ..canonical_json import canonical_bytes
from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from ..domain.space_media_ref import MEDIA_REFS_FIELD

#: The signature suite of a page proposal's descriptor.
PAGE_MEDIA_REFS_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_PAGE_MEDIA_REFS_SIG_SUITES: frozenset[str] = frozenset(
    {PAGE_MEDIA_REFS_SIG_SUITE_ED25519}
)

#: The payload fields next to the descriptor.
SIG_FIELD: str = "media_refs_sig"
SIG_SUITE_FIELD: str = "media_refs_sig_suite"

#: Domain separation from every other signature of the household key.
_DOMAIN: bytes = b"space-page-media-refs:v1:"


class UnsupportedPageMediaRefsSigSuite(ValueError):
    """A page proposal's descriptor names a signature suite we don't support."""


def page_media_refs_signing_bytes(
    *,
    space_id: str,
    page_id: str,
    origin: str,
    proposal_hash: str,
    media_refs: object,
    suite: str,
) -> bytes:
    """Canonical bytes of the signature: the descriptor, pinned to one
    space, page, proposing household and proposal as sent."""
    return _DOMAIN + canonical_bytes(
        {
            "space_id": space_id,
            "page_id": page_id,
            "origin": origin,
            "proposal_hash": proposal_hash,
            "media_refs": media_refs,
            "suite": suite,
        }
    )


def signed_page_media_refs(
    *,
    space_id: str,
    page_id: str,
    origin: str,
    proposal_hash: str,
    media_refs: dict,
    identity_seed: bytes,
) -> dict:
    """The payload fields carrying *media_refs* signed by our household."""
    suite = PAGE_MEDIA_REFS_SIG_SUITE_ED25519
    sig = sign_ed25519(
        identity_seed,
        page_media_refs_signing_bytes(
            space_id=space_id,
            page_id=page_id,
            origin=origin,
            proposal_hash=proposal_hash,
            media_refs=media_refs,
            suite=suite,
        ),
    )
    return {
        MEDIA_REFS_FIELD: media_refs,
        SIG_SUITE_FIELD: suite,
        SIG_FIELD: b64url_encode(sig),
    }


def verified_page_media_refs(
    payload: dict,
    *,
    space_id: str,
    page_id: str,
    origin: str,
    proposal_hash: str,
    origin_pk: bytes | None,
) -> object | None:
    """The proposal's descriptor iff household *origin* (whose key is
    *origin_pk*) signed it for exactly this proposal, else ``None``.

    :raises UnsupportedPageMediaRefsSigSuite: an unknown suite (never a
        fallback)."""
    refs = payload.get(MEDIA_REFS_FIELD)
    if refs is None or not origin_pk:
        return None
    suite = payload.get(SIG_SUITE_FIELD)
    if suite is None:
        return None  # unsigned: never taken
    if suite not in SUPPORTED_PAGE_MEDIA_REFS_SIG_SUITES:
        raise UnsupportedPageMediaRefsSigSuite(str(suite))
    sig_text = payload.get(SIG_FIELD)
    if not isinstance(sig_text, str) or not sig_text:
        return None
    try:
        sig = b64url_decode(sig_text)
        signing = page_media_refs_signing_bytes(
            space_id=space_id,
            page_id=page_id,
            origin=origin,
            proposal_hash=proposal_hash,
            media_refs=refs,
            suite=str(suite),
        )
    except TypeError, ValueError:
        return None
    if not verify_ed25519(origin_pk, signing, sig):
        return None
    return refs

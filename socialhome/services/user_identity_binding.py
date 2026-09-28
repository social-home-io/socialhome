"""Per-user identity-binding wire fields for outbound user publication.

Phase 1 of the independent user identity (§ user-identity, proto v_25): when
a household publishes one of its users to a peer — via the ``USERS_SYNC``
roster push on pair-confirm or the ``USER_UPDATED`` profile fan-out — the
per-user payload may carry a *binding* that proves the user holds their own
Ed25519 identity key, independent of the hosting instance.

The binding is a *self-verifying portable credential* — five fields that
let a receiver reconstruct and re-verify the whole
:class:`~socialhome.domain.user.UserIdentityAssertion` **outside** the
original signed federation envelope (Phase 3 resolves it on demand, Phase 4
carries it on move-out, so it is cached and relayed standalone):

* ``user_identity_public_key`` — hex Ed25519 user public key,
* ``user_sig_suite`` — the signature suite (``"ed25519"`` in Phase 1),
* ``user_signature`` — the base64url USER self-signature,
* ``user_assertion_signature`` — the base64url INSTANCE signature (named
  distinctly so it can't collide with any per-user ``signature`` key),
* ``user_assertion_issued_at`` — the ISO-8601 ``issued_at`` the instance
  signature commits to.

Carrying the instance signature + issued_at (not just the user self-sig) is
load-bearing: the instance signature is what defends against the
instance→user-key transplant (commit 0d64006f). A receiver that only had the
user self-sig would lose that defense the moment the assertion travels
detached from its envelope.

They ride alongside the existing legacy per-user fields, never replacing
them. They are emitted **only** for a peer that advertises v_25
(:data:`FederationCapability.MIN_FOR_USER_IDENTITY_KEY`) — an older peer
can't validate them and would mis-handle the wire shape, so it gets exactly
the legacy payload. The binding is also skipped when the user has no minted
identity key (an early-boot row the startup backfill hasn't reached yet) or
when no ``user_repo`` is wired to fetch the keypair.

A v_26 peer (:data:`FederationCapability.MIN_FOR_IDENTITY_ANCHOR`)
additionally gets the immutable ``identity_anchor`` (the uuid the receiver
derives ``user_id`` from). The anchor is baked into both signatures of the
assertion, so it is shipped **only** to v_26+ peers — a v_25 peer would
verify the assertion against the username-derived ``user_id`` and so must get
the anchor-free binding whose signatures commit to no anchor. Including the
anchor is therefore gated on its own ``peer_supports`` check, nested inside
the v_25 binding gate.

Both outbound services call :func:`user_identity_binding_fields` so the gate,
the key-handling and the suite stay in one place.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..crypto import USER_SIG_SUITE_ED25519, build_user_identity_assertion
from ..domain.federation_capabilities import FederationCapability

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


async def user_identity_binding_fields(
    *,
    federation_service: "FederationService",
    user_repo: "AbstractUserRepo | None",
    peer_instance_id: str,
    user_id: str,
    username: str,
    display_name: str,
    picture_hash: str | None = None,
) -> dict[str, str]:
    """Return the per-user identity-binding wire fields for one user/peer.

    No binding (legacy shape) when:

    * ``user_repo`` is ``None`` (the service wasn't wired with one) — empty, or
    * the peer doesn't support v_25, or the user has no minted identity
      keypair — then only the bare ``identity_anchor`` (when the user has
      one), so a first-seen ``user_id`` can still be re-derived.

    Otherwise the returned dict carries the full self-verifying credential —
    ``user_identity_public_key`` / ``user_sig_suite`` / ``user_signature``
    (the issued-at-independent binding fields) **plus**
    ``user_assertion_signature`` (the INSTANCE signature) and
    ``user_assertion_issued_at`` (the assertion's ``issued_at``) — so the
    receiver can reconstruct + verify the whole assertion standalone.
    Fail-soft: any error fetching the keypair or building the binding degrades
    to the legacy shape rather than dropping the user from the snapshot.
    """
    if user_repo is None:
        return {}
    if not await federation_service.peer_supports(
        peer_instance_id,
        min_version=FederationCapability.MIN_FOR_USER_IDENTITY_KEY,
    ):
        return await _plain_anchor(user_repo, username)

    try:
        keypair = await user_repo.get_user_identity_keypair(username)
    except Exception as exc:  # pragma: no cover — defensive, fail-soft
        log.warning(
            "user-identity-binding: keypair lookup for %s failed: %s",
            username,
            exc,
        )
        return {}
    if keypair is None:
        return await _plain_anchor(user_repo, username)
    user_public_key, user_seed = keypair

    # The immutable anchor (uuid) is the v_26 addition. We only put it on the
    # wire for a peer that advertises v_26 (MIN_FOR_IDENTITY_ANCHOR) — a v_25
    # peer can't derive user_id from the anchor, so it gets the Phase-1 binding
    # WITHOUT the anchor (and the assertion it reconstructs falls back to the
    # username for user_id derivation, exactly as in Phase 1). The anchor must
    # nonetheless be baked into BOTH signatures here so a v_26 receiver's
    # verify — which commits the anchor into the signed bytes — passes.
    identity_anchor: str | None = None
    if await federation_service.peer_supports(
        peer_instance_id,
        min_version=FederationCapability.MIN_FOR_IDENTITY_ANCHOR,
    ):
        try:
            identity_anchor = await user_repo.get_user_identity_anchor(username)
        except Exception as exc:  # pragma: no cover — defensive, fail-soft
            log.warning(
                "user-identity-binding: anchor lookup for %s failed: %s",
                username,
                exc,
            )
            identity_anchor = None

    issued_at = datetime.now(timezone.utc).isoformat()
    assertion = build_user_identity_assertion(
        instance_seed=federation_service.own_identity_seed,
        user_id=user_id,
        instance_id=federation_service.own_instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued_at,
        picture_hash=picture_hash,
        user_seed=user_seed,
        user_public_key=user_public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
        identity_anchor=identity_anchor,
    )
    if (
        assertion.user_identity_public_key is None
        or assertion.user_sig_suite is None
        or assertion.user_signature is None
    ):  # pragma: no cover — both halves were supplied, so always present
        return {}
    fields = {
        "user_identity_public_key": assertion.user_identity_public_key,
        "user_sig_suite": assertion.user_sig_suite,
        "user_signature": assertion.user_signature,
        # Full self-verifying credential: the INSTANCE signature + issued_at
        # let the receiver reconstruct + re-verify the assertion outside the
        # original envelope (defends the key-transplant attack on a relayed
        # / cached copy). Named ``user_assertion_*`` to avoid colliding with
        # any per-user ``signature`` / ``issued_at`` entry key.
        "user_assertion_signature": assertion.signature,
        "user_assertion_issued_at": assertion.issued_at,
    }
    # v_26: ship the anchor only to peers that support it. A v_25 peer gets the
    # Phase-1 binding above with no ``identity_anchor`` key on the wire.
    if identity_anchor is not None:
        fields["identity_anchor"] = identity_anchor
    return fields


async def _plain_anchor(user_repo: "AbstractUserRepo", username: str) -> dict[str, str]:
    """The bare ``identity_anchor`` when no binding carries it.

    A receiver only takes a user it has never seen when the ``user_id``
    re-derives from the sender's pinned key and this anchor (or the
    username for a legacy id). Nothing signs it here and nothing needs to:
    the derivation itself is the proof. Omitted when the user has none.
    """
    try:
        anchor = await user_repo.get_user_identity_anchor(username)
    except Exception as exc:  # pragma: no cover — defensive, fail-soft
        log.warning("identity-anchor lookup for %s failed: %s", username, exc)
        return {}
    return {"identity_anchor": anchor} if anchor else {}

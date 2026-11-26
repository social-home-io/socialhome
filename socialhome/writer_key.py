"""Writer key signatures — the channel writer key's sign / verify (v_51).

A space's opaque channel has a per-epoch **channel writer key** (one Ed25519
key shared by every household allowed to publish there, delivered inside the
channel grant, :mod:`socialhome.gfs_channel`). It signs the anonymous channel
publish and the channel-scope media upload; the GFS verifies against the key
the channel key pinned for that epoch. The suite tag lives in
:mod:`socialhome.domain.writer_key`.

(The space-keyed writer GROUP key of v_50 — derived from the space authority
seed, pinned on a GFS with an authority-signed cert — went with the GFS's
space-keyed routes in v_96.)

Depends only on :mod:`socialhome.crypto`, so the content-blind GFS process
can import it.
"""

from __future__ import annotations

from .crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from .domain.writer_key import SUPPORTED_WRITER_KEY_SUITES, UnsupportedWriterKeySuite


def sign_with_writer_key(writer_seed: bytes, message: bytes) -> str:
    """b64url Ed25519 signature of ``message`` under the writer key."""
    return b64url_encode(sign_ed25519(writer_seed, message))


def verify_writer_sig(
    *, writer_pk: bytes, message: bytes, writer_sig: str, suite: str
) -> bool:
    """Whether ``writer_sig`` (suite ``suite``) signs ``message`` under
    ``writer_pk``. Raises :class:`UnsupportedWriterKeySuite` for an unknown
    suite; ``False`` for a malformed or failing signature."""
    if suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(suite)
    try:
        sig = b64url_decode(writer_sig)
    except ValueError, TypeError:
        return False
    return verify_ed25519(writer_pk, message, sig)

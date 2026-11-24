"""Writer key suite — the tag of a channel writer key (v_51).

A space's opaque channel has a per-epoch writer key (one Ed25519 key shared
by every household allowed to publish there, both ``write`` and ``comment``
scopes, so the server cannot tell them apart). Its public half is pinned on
the GFS by the channel key (:class:`~socialhome.domain.gfs_channel
.ChannelWriterKeyCert`); the private half rides the channel grant
(:class:`~socialhome.domain.gfs_channel.ChannelWriterKeyGrant`). Every such
shape carries ``writer_key_suite`` (crypto-suite rule — receivers reject an
unknown suite, never default).

(The space-keyed writer GROUP key of v_50 and its ``WriterKeyCert`` /
``WriterKeyGrant`` shapes went with the GFS's space-keyed routes in v_96.)

Pure: constants only.
"""

from __future__ import annotations

#: The writer key's algorithm — of ``writer_pk`` and of every ``writer_sig``
#: it makes. Phase-2 of ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a
#: sibling.
WRITER_KEY_SUITE_ED25519: str = "ed25519"
SUPPORTED_WRITER_KEY_SUITES: frozenset[str] = frozenset({WRITER_KEY_SUITE_ED25519})


class UnsupportedWriterKeySuite(ValueError):
    """A writer key cert, grant or signature names a suite this build does
    not know. Rejected — never defaulted to Ed25519."""

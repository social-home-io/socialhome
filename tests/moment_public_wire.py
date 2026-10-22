"""Test helpers for encrypted public moments (v_62).

Builds the envelopes an author household sends — the moment sealed under
its item key and the envelope signed — and a stand-in for the follower's
:class:`~socialhome.services.moment_public_key_inbound
.MomentPublicKeyInbound` that holds that one step key.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

from socialhome.moment_public_seal import seal_body, sign_wire

#: The step key and key id the helpers seal under. The generation is minted
#: now, so step 0 is the current step — a GFS refuses an envelope sealed
#: under an earlier day's step as a re-post.
TEST_KEY: bytes = os.urandom(32)
TEST_GEN: int = int(time.time())
TEST_STEP: int = 0


def now_iso(*, hours: float = 0) -> str:
    """A tz-aware ISO timestamp *hours* from now (negative: in the past)."""
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def encrypted_envelope(
    payload: dict,
    seed: bytes,
    *,
    key: bytes = TEST_KEY,
    gen: int = TEST_GEN,
    step: int = TEST_STEP,
    op: str | None = None,
) -> dict:
    """*payload* (a create's or delete's fields) as the author household
    sends it: sealed, then signed with *seed*.

    The op defaults to ``create`` when the payload has a ``created_at``,
    else ``delete``. ``moment_id`` and ``instance_id`` (the author
    household) go on the envelope, not in the sealed body."""
    body = {k: v for k, v in payload.items() if k not in ("instance_id", "moment_id")}
    body["op"] = op or ("create" if "created_at" in payload else "delete")
    author = str(payload.get("author_user_id") or "")
    return sign_wire(
        seed,
        {
            "author_user_id": author,
            "instance_id": str(payload.get("instance_id") or "inst-remote"),
            **seal_body(
                step_key=key,
                author_user_id=author,
                key_gen=gen,
                key_step=step,
                moment_id=str(payload["moment_id"]),
                body=body,
            ),
        },
    )


class StubMomentKeys:
    """Holds :data:`TEST_KEY` for every author at (:data:`TEST_GEN`,
    :data:`TEST_STEP`) (``missing`` authors hold none); records requests."""

    def __init__(self, *, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.requests: list[tuple[str, str, int]] = []
        self.deliveries: list[dict] = []
        self.import_gen: int | None = None

    async def key_for(self, author_user_id: str, key_gen: int, key_step: int):
        if author_user_id in self.missing:
            return None
        if (key_gen, key_step) != (TEST_GEN, TEST_STEP):
            return None
        return TEST_KEY

    async def request_key(self, author_user_id: str, *, gfs_id: str, want_gen: int):
        self.requests.append((author_user_id, gfs_id, want_gen))
        return True

    async def import_delivery(self, delivery: dict, *, gfs_id: str) -> int | None:
        self.deliveries.append(delivery)
        if self.import_gen is not None:
            self.missing.discard(str(delivery.get("author_user_id") or ""))
        return self.import_gen

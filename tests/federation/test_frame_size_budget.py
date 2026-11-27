"""Frame-size budget: what one space item costs per channel seat.

The GFS sends one frame per seat per item, so a byte here is multiplied by
every member and follower (10 000 followers × 400 posts a day is the
planning case in ``docs/protocol/discovery.md``). This test builds the real
frames — the production signers, writer certs, size padding and the GFS
fan-out shape — for realistic items and pins their sizes. Since v_96 a
space's content reaches a GFS only through its opaque channel: the author's
household publishes the item into the channel and every seat gets
``{"type": "relay", "channel_id", "event_type": "space_item", "epoch",
"payload"}`` — no frame cert, the writer cert rides inside the ciphertext.
(The space-keyed host relay and its ``payload_suite`` compression went with
the GFS's space-keyed routes in P7.)

Run with ``-s`` to print the table. The numbers are the regression guard
for the payload-size work (canonical suite, binary frames).
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime

import orjson
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    ed25519_public_key,
)
from socialhome.domain.gfs_channel import SPACE_ITEM_EVENT_TYPE
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import Post, PostType
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS
from socialhome.domain.ws_binary_frame import encode_binary_frame
from socialhome.services.gfs_member_publish_service import build_item_plaintext
from socialhome.services.space_item_author import build_signed_item_inner
from socialhome.services.space_public_author import build_signed_author_inner
from socialhome.services.space_writer_cert_service import WRITER_CERT_FIELD
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

#: 300 characters of ordinary text per language — the "typical post".
TEXT_EN = (
    "Lovely morning at the allotment today! The tomatoes finally turned red, "
    "the beans are climbing the new trellis and somebody left a basket of "
    "plums by the gate with a note saying help yourself. Bring a bag on "
    "Saturday if you want some, we will be there from ten until about two. "
    "Coffee is on us, cake is on you."
)[:300]
TEXT_DE = (
    "Schöner Morgen im Kleingarten! Die Tomaten sind endlich rot, die Bohnen "
    "klettern am neuen Spalier hoch und jemand hat einen Korb Pflaumen ans "
    "Tor gestellt, mit einem Zettel: Bedient euch. Bringt am Samstag eine "
    "Tüte mit, wir sind von zehn bis etwa zwei Uhr da. Kaffee geht auf uns, "
    "Kuchen auf euch. Grüße an alle Nachbarn!"
)[:300]
#: Chinese text without repetition: a natural opening, then characters drawn
#: (seeded, so every run measures the same bytes) from the CJK block. Real
#: Chinese compresses a little better than this, so its numbers are a
#: ceiling. A repeated passage would compress far better than any real post.
_CJK_OPENING = (
    "今天早上在菜园里真是太美好了！西红柿终于红了，豆子正沿着新的架子往上爬，"
    "有人在门口放了一篮李子，还留了一张纸条写着请随便拿。"
)
_CJK_RNG = random.Random(20261023)
TEXT_CJK_LONG = (
    _CJK_OPENING
    + "".join(
        chr(_CJK_RNG.randrange(0x4E00, 0x6000)) for _ in range(600 - len(_CJK_OPENING))
    )
)[:600]
TEXT_CJK = TEXT_CJK_LONG[:300]

SPACE_ID = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
CHANNEL_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
EPOCH = 7


@dataclass(frozen=True)
class _World:
    """One space, one author household, real keys."""

    space_seed: bytes
    author_seed: bytes
    author_pk: bytes
    author_user_id: str
    origin_instance_id: str
    content_key: bytes


def _fixed(label: str, n: int = 32) -> bytes:
    """Deterministic bytes: keys and ids are fixed, so every run (and the
    text vs binary comparison) measures exactly the same plaintext — a case
    near a bucket boundary can't flip between runs."""
    return hashlib.sha256(label.encode()).digest()[:n]


def _world() -> _World:
    space_seed, author_seed = _fixed("space seed"), _fixed("author seed")
    author_pk = ed25519_public_key(author_seed)
    return _World(
        space_seed=space_seed,
        author_seed=author_seed,
        author_pk=author_pk,
        author_user_id=derive_user_id(author_pk, "erin"),
        origin_instance_id=derive_instance_id(author_pk),
        content_key=_fixed("content key"),
    )


def _encrypt(world: _World, plaintext: bytes) -> str:
    """The wire form ``SpaceContentEncryption.encrypt`` produces:
    ``b64url(nonce) + ":" + b64url(AES-256-GCM(plaintext, aad=space_id))``."""
    nonce = os.urandom(12)
    ct = AESGCM(world.content_key).encrypt(nonce, plaintext, SPACE_ID.encode())
    return b64url_encode(nonce) + ":" + b64url_encode(ct)


def _cert(world: _World, scope: str = "write"):
    cert = sign_writer_cert(
        space_seed=world.space_seed,
        space_id=SPACE_ID,
        epoch=EPOCH,
        instance_pk=world.author_pk,
        scope=scope,
        issued_at=1_790_000_000,
    )
    return bind_writer_users(
        cert, space_seed=world.space_seed, user_ids=[world.author_user_id]
    )


def _post(world: _World, text: str, *, preview: bool = False) -> Post:
    return Post(
        id=_fixed("post " + text, 16).hex(),
        author=world.author_user_id,
        type=PostType.TEXT,
        created_at=datetime(2026, 10, 23, 9, 30, tzinfo=UTC),
        content=text,
        link_preview=(
            LinkPreview(
                url="https://allotment.example.org/news/plum-harvest-2026",
                title="Plum harvest weekend at the allotments",
                description=(
                    "Bring a bag, take some plums. Saturday from ten, coffee "
                    "provided, cake welcome."
                ),
                site_name="Allotment Society",
                thumbnail_url="api/media/9f2c4e1a7b3d5f60.webp",
            )
            if preview
            else None
        ),
    )


def _post_inner(world: _World, text: str, *, member: bool, preview: bool = False):
    post = _post(world, text, preview=preview)
    return build_signed_author_inner(
        post=post,
        space_id=SPACE_ID,
        author_username="erin",
        author_pk=world.author_pk,
        author_identity_seed=world.author_seed,
        origin_instance_id=world.origin_instance_id,
        item_type="post" if member else None,
        item_target=post.id if member else None,
    )


def _comment_inner(world: _World, text: str) -> dict:
    return build_signed_item_inner(
        item_type="comment",
        item_target=_fixed("comment " + text, 16).hex(),
        space_id=SPACE_ID,
        post_id=_fixed("post of " + text, 16).hex(),
        author_user_id=world.author_user_id,
        author_username="erin",
        author_pk=world.author_pk,
        author_identity_seed=world.author_seed,
        origin_instance_id=world.origin_instance_id,
        ts="2026-10-23T09:31:00+00:00",
        comment_type="text",
        content=text,
        created_at="2026-10-23T09:31:00+00:00",
    )


def channel_frame(world: _World, item_type: str, inner: dict) -> tuple[bytes, int]:
    """``(seat frame bytes, plaintext bucket)`` of a channel item."""
    cert = _cert(world, "write" if item_type == "post" else "comment")
    plaintext = build_item_plaintext(
        item_type, {**inner, WRITER_CERT_FIELD: cert.to_wire()}
    )
    frame: dict = {
        "type": "relay",
        "channel_id": CHANNEL_ID,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": EPOCH,
        "payload": _encrypt(world, plaintext),
    }
    return orjson.dumps(frame), len(plaintext)


def size_table(*, binary: bool = False) -> list[tuple[str, int, int]]:
    """``(case, bucket, frame bytes)`` for every fixture — as the JSON text
    frame, or with ``binary`` the ``ws_binary1`` frame a socket that asked
    for one receives."""
    rows = _size_table()
    if not binary:
        return [(c, b, len(f)) for c, b, f in rows]
    out = []
    for case, bucket, frame in rows:
        raw = encode_binary_frame(orjson.loads(frame))
        assert raw is not None, case
        out.append((case, bucket, len(raw)))
    return out


def _size_table() -> list[tuple[str, int, bytes]]:
    w = _world()
    rows: list[tuple[str, int, bytes]] = []
    for label, text in (
        ("post en", TEXT_EN),
        ("post de", TEXT_DE),
        ("post cjk", TEXT_CJK),
        ("post cjk 600", TEXT_CJK_LONG),
    ):
        frame, bucket = channel_frame(w, "post", _post_inner(w, text, member=True))
        rows.append((label, bucket, frame))
    frame, bucket = channel_frame(
        w, "post", _post_inner(w, TEXT_EN, member=True, preview=True)
    )
    rows.append(("post en + link preview", bucket, frame))
    frame, bucket = channel_frame(
        w, "comment", _comment_inner(w, "Count me in, I'll bring a plum cake!")
    )
    rows.append(("comment", bucket, frame))
    return rows


def _print_table(rows: list[tuple[str, int, int]]) -> None:
    print()
    print(f"{'case':<26}{'bucket':>8}{'frame B':>9}")
    for case, bucket, size in rows:
        print(f"{case:<26}{bucket:>8}{size:>9}")


#: ``case → (padding bucket, max frame bytes)``. The frame size of a fixed
#: bucket only moves with the fixed fields (ids, the epoch), so the ceiling
#: is tight; a change that grows a frame past it — or that pushes a case
#: into a bigger bucket — fails here first.
BUDGET: dict[str, tuple[int, int]] = {
    "post en": (4096, 5640),
    "post de": (4096, 5640),
    "post cjk": (4096, 5640),
    "post cjk 600": (4096, 5640),
    "post en + link preview": (4096, 5640),
    "comment": (4096, 5640),
}


def test_frame_sizes_stay_within_budget() -> None:
    rows = size_table()
    _print_table(rows)
    assert {case for case, _b, _s in rows} == set(BUDGET)
    for case, bucket, size in rows:
        want_bucket, max_bytes = BUDGET[case]
        assert bucket in ITEM_SIZE_BUCKETS, (case, bucket)
        assert bucket == want_bucket, (case, bucket)
        assert size <= max_bytes, (case, size)


#: ``ws_binary1`` frames (what a seat whose socket asked for binary frames
#: receives): the ciphertext travels raw instead of base64 — ~22 % off a
#: 4 KiB-bucket frame; the rest is the JSON header.
BUDGET_BINARY: dict[str, int] = dict.fromkeys(BUDGET, 4310)


def test_binary_frame_sizes_stay_within_budget() -> None:
    rows = size_table(binary=True)
    _print_table(rows)
    text = {c: s for c, _b, s in size_table()}
    assert {case for case, _b, _s in rows} == set(BUDGET_BINARY)
    for case, _bucket, size in rows:
        assert size <= BUDGET_BINARY[case], (case, size)
        assert size < text[case] * 0.85, case


def test_channel_frames_carry_no_frame_cert_and_no_space() -> None:
    """The channel frame names no space and carries no plaintext writer
    cert (it rides inside the ciphertext)."""
    for _case, _bucket, frame in _size_table():
        parsed = orjson.loads(frame)
        assert "writer_cert" not in parsed
        assert "space_id" not in parsed
        assert "payload_suite" not in parsed
        assert SPACE_ID.encode() not in frame


@pytest.mark.parametrize("bucket", ITEM_SIZE_BUCKETS[:2])
def test_a_frame_costs_its_bucket_plus_a_fixed_overhead(bucket: int) -> None:
    """Base64 (×4/3) of the padded ciphertext plus the routing fields: the
    frame grows with the bucket, never with the text inside it."""
    w = _world()
    sizes = set()
    for text in ("short", TEXT_EN[:120]):
        inner = _post_inner(w, text, member=True)
        frame, got = channel_frame(w, "post", inner)
        if got == bucket:
            sizes.add(len(frame))
    assert len(sizes) <= 1

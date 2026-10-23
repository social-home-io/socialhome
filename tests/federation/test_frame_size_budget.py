"""Frame-size budget: what one public post costs per follower.

The GFS sends one frame per follower per item, so a byte here is multiplied
by every follower (10 000 followers × 400 posts a day is the planning case
in ``docs/protocol/discovery.md``). This test builds the real frames — the
production signers, writer certs, size padding and the GFS fan-out shape —
for realistic items on both delivery paths and pins their sizes:

* **relay** — the space's seed holder relays a member's post: the
  author-signed inner (with its re-stamped writer cert) padded, encrypted
  under the space content key, authority-signed, fanned out by the GFS as
  ``{"type": "relay", "space_id", "event_type": "space_post_public",
  "payload": {...}}``.
* **member** — the author's household publishes the item itself (v_49
  trusted mode): ``{"type": "relay", "space_id", "event_type":
  "space_item", "epoch", "writer_cert", "payload"}``.

Run with ``-s`` to print the table. The numbers are the regression guard
for the payload-size work (canonical suite, compression, binary frames).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import orjson
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.authority_sig import sign_authority_event, strip_authority_sig_fields
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.gfs_member_publish import SPACE_ITEM_EVENT_TYPE
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import Post, PostType
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS, pad_json_object
from socialhome.services.gfs_member_publish_service import build_item_plaintext
from socialhome.services.space_item_author import build_signed_item_inner
from socialhome.services.space_public_author import build_signed_author_inner
from socialhome.services.space_public_outbound import AUTHORITY_EVENT_SPACE_POST_PUBLIC
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
TEXT_CJK = (
    "今天早上在菜园里真是太美好了！西红柿终于红了，豆子正沿着新的架子往上爬，"
    "有人在门口放了一篮李子，还留了一张纸条写着请随便拿。星期六如果你想要的话就带个袋子来，"
    "我们大概从十点待到两点左右。咖啡我们请客，蛋糕就交给你们了。"
) * 8
TEXT_CJK_LONG = TEXT_CJK[:600]
TEXT_CJK = TEXT_CJK[:300]

SPACE_ID = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
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


def _world() -> _World:
    space = generate_identity_keypair()
    author = generate_identity_keypair()
    return _World(
        space_seed=space.private_key,
        author_seed=author.private_key,
        author_pk=author.public_key,
        author_user_id=derive_user_id(author.public_key, "erin"),
        origin_instance_id=derive_instance_id(author.public_key),
        content_key=os.urandom(32),
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
        id=secrets.token_hex(16),
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
        item_target=secrets.token_hex(16),
        space_id=SPACE_ID,
        post_id=secrets.token_hex(16),
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


def relay_frame(world: _World, inner: dict) -> tuple[bytes, int]:
    """``(follower frame bytes, plaintext bucket)`` on the seed-holder relay."""
    body = dict(inner)
    body[WRITER_CERT_FIELD] = _cert(world).to_wire()
    plaintext = pad_json_object(body)
    envelope: dict = {
        "space_id": SPACE_ID,
        "epoch": EPOCH,
        "encrypted_payload": _encrypt(world, plaintext),
    }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=world.space_seed,
        )
    )
    frame = {
        "type": "relay",
        "space_id": SPACE_ID,
        "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        "payload": envelope,
    }
    return orjson.dumps(frame), len(plaintext)


def member_frame(world: _World, item_type: str, inner: dict) -> tuple[bytes, int]:
    """``(follower frame bytes, plaintext bucket)`` on the member publish."""
    cert = _cert(world, "write" if item_type == "post" else "comment")
    plaintext = build_item_plaintext(
        item_type, {**inner, WRITER_CERT_FIELD: cert.to_wire()}
    )
    frame = {
        "type": "relay",
        "space_id": SPACE_ID,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": EPOCH,
        "writer_cert": cert.v1().to_wire(),
        "payload": _encrypt(world, plaintext),
    }
    return orjson.dumps(frame), len(plaintext)


def size_table() -> list[tuple[str, str, int, int]]:
    """``(case, path, bucket, frame bytes)`` for every fixture."""
    w = _world()
    rows: list[tuple[str, str, int, int]] = []
    for label, text in (
        ("post en", TEXT_EN),
        ("post de", TEXT_DE),
        ("post cjk", TEXT_CJK),
        ("post cjk 600", TEXT_CJK_LONG),
    ):
        frame, bucket = relay_frame(w, _post_inner(w, text, member=False))
        rows.append((label, "relay", bucket, len(frame)))
        frame, bucket = member_frame(w, "post", _post_inner(w, text, member=True))
        rows.append((label, "member", bucket, len(frame)))
    frame, bucket = relay_frame(w, _post_inner(w, TEXT_EN, member=False, preview=True))
    rows.append(("post en + link preview", "relay", bucket, len(frame)))
    frame, bucket = member_frame(
        w, "post", _post_inner(w, TEXT_EN, member=True, preview=True)
    )
    rows.append(("post en + link preview", "member", bucket, len(frame)))
    frame, bucket = member_frame(
        w, "comment", _comment_inner(w, "Count me in, I'll bring a plum cake!")
    )
    rows.append(("comment", "member", bucket, len(frame)))
    return rows


def _print_table(rows: list[tuple[str, str, int, int]]) -> None:
    print()
    print(f"{'case':<26}{'path':<8}{'bucket':>8}{'frame B':>9}")
    for case, path, bucket, size in rows:
        print(f"{case:<26}{path:<8}{bucket:>8}{size:>9}")


#: ``(case, path) → (padding bucket, max frame bytes)``. The frame size of a
#: fixed bucket only moves with the fixed fields (ids, signatures, the
#: writer cert), so the ceiling is tight; a change that grows a frame past
#: it — or that pushes a case into a bigger bucket — fails here first.
BUDGET: dict[tuple[str, str], tuple[int, int]] = {
    ("post en", "relay"): (4096, 5840),
    ("post en", "member"): (4096, 5920),
    ("post de", "relay"): (4096, 5840),
    ("post de", "member"): (4096, 5920),
    ("post cjk", "relay"): (4096, 5840),
    ("post cjk", "member"): (4096, 5920),
    ("post cjk 600", "relay"): (4096, 5840),
    ("post cjk 600", "member"): (4096, 5920),
    ("post en + link preview", "relay"): (4096, 5840),
    ("post en + link preview", "member"): (4096, 5920),
    ("comment", "member"): (4096, 5920),
}


def test_frame_sizes_stay_within_budget() -> None:
    rows = size_table()
    _print_table(rows)
    assert {(case, path) for case, path, _b, _s in rows} == set(BUDGET)
    for case, path, bucket, size in rows:
        want_bucket, max_bytes = BUDGET[(case, path)]
        assert bucket in ITEM_SIZE_BUCKETS, (case, path, bucket)
        assert bucket == want_bucket, (case, path, bucket)
        assert size <= max_bytes, (case, path, size)


@pytest.mark.parametrize("bucket", ITEM_SIZE_BUCKETS[:2])
def test_a_frame_costs_its_bucket_plus_a_fixed_overhead(bucket: int) -> None:
    """Base64 (×4/3) of the padded ciphertext plus the routing and
    signature fields: the frame grows with the bucket, never with the text
    inside it."""
    w = _world()
    sizes = set()
    for text in ("short", TEXT_EN[:120]):
        inner = _post_inner(w, text, member=True)
        frame, got = member_frame(w, "post", inner)
        if got == bucket:
            sizes.add(len(frame))
    assert len(sizes) <= 1

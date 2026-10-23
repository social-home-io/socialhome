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

import hashlib
import os
import random
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
    ed25519_public_key,
)
from socialhome.domain.gfs_member_publish import SPACE_ITEM_EVENT_TYPE
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import Post, PostType
from socialhome.domain.space_item import (
    ITEM_SIZE_BUCKETS,
    PAYLOAD_SUITE_JSON,
    PAYLOAD_SUITE_ZLIB,
    open_item_plaintext,
    seal_item_plaintext,
)
from socialhome.domain.ws_binary_frame import encode_binary_frame
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


def relay_frame(
    world: _World, inner: dict, suite: str = PAYLOAD_SUITE_JSON
) -> tuple[bytes, int]:
    """``(follower frame bytes, plaintext bucket)`` on the seed-holder relay."""
    body = dict(inner)
    body[WRITER_CERT_FIELD] = _cert(world).to_wire()
    plaintext = seal_item_plaintext(body, suite=suite)
    assert plaintext is not None
    envelope: dict = {
        "space_id": SPACE_ID,
        "epoch": EPOCH,
        "encrypted_payload": _encrypt(world, plaintext),
    }
    if suite != PAYLOAD_SUITE_JSON:
        envelope["payload_suite"] = suite
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


def member_frame(
    world: _World, item_type: str, inner: dict, suite: str = PAYLOAD_SUITE_JSON
) -> tuple[bytes, int]:
    """``(follower frame bytes, plaintext bucket)`` on the member publish."""
    cert = _cert(world, "write" if item_type == "post" else "comment")
    plaintext = build_item_plaintext(
        item_type, {**inner, WRITER_CERT_FIELD: cert.to_wire()}, suite=suite
    )
    assert plaintext is not None
    frame: dict = {
        "type": "relay",
        "space_id": SPACE_ID,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": EPOCH,
        "writer_cert": cert.v1().to_wire(),
        "payload": _encrypt(world, plaintext),
    }
    if suite != PAYLOAD_SUITE_JSON:
        frame["payload_suite"] = suite
    return orjson.dumps(frame), len(plaintext)


def size_table(
    suite: str = PAYLOAD_SUITE_JSON, *, binary: bool = False
) -> list[tuple[str, str, int, int]]:
    """``(case, path, bucket, frame bytes)`` for every fixture, under
    ``suite`` — as the JSON text frame, or with ``binary`` the ``ws_binary1``
    frame a socket that asked for one receives."""
    rows = _size_table(suite)
    if not binary:
        return [(c, p, b, len(f)) for c, p, b, f in rows]
    out = []
    for case, path, bucket, frame in rows:
        raw = encode_binary_frame(orjson.loads(frame))
        assert raw is not None, (case, path)
        out.append((case, path, bucket, len(raw)))
    return out


def _size_table(suite: str) -> list[tuple[str, str, int, bytes]]:
    w = _world()
    rows: list[tuple[str, str, int, bytes]] = []
    for label, text in (
        ("post en", TEXT_EN),
        ("post de", TEXT_DE),
        ("post cjk", TEXT_CJK),
        ("post cjk 600", TEXT_CJK_LONG),
    ):
        frame, bucket = relay_frame(w, _post_inner(w, text, member=False), suite)
        rows.append((label, "relay", bucket, frame))
        frame, bucket = member_frame(
            w, "post", _post_inner(w, text, member=True), suite
        )
        rows.append((label, "member", bucket, frame))
    frame, bucket = relay_frame(
        w, _post_inner(w, TEXT_EN, member=False, preview=True), suite
    )
    rows.append(("post en + link preview", "relay", bucket, frame))
    frame, bucket = member_frame(
        w, "post", _post_inner(w, TEXT_EN, member=True, preview=True), suite
    )
    rows.append(("post en + link preview", "member", bucket, frame))
    frame, bucket = member_frame(
        w,
        "comment",
        _comment_inner(w, "Count me in, I'll bring a plum cake!"),
        suite,
    )
    rows.append(("comment", "member", bucket, frame))
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


#: The same cases under ``payload_suite: "zlib1"`` — what a server that
#: proved ``payload_zlib1`` fans out once every follower there reads it. A
#: 300-character Latin-script post drops from the 4 KiB to the 1 KiB bucket
#: (~5.8 KB → ~1.75 KB per follower). Chinese text compresses far less
#: (each character is three UTF-8 bytes of near-random data to zlib): a
#: 300-character post stays in 4 KiB, as does a post with a link preview —
#: the publisher then sends no compressed copy at all.
BUDGET_ZLIB: dict[tuple[str, str], tuple[int, int]] = {
    ("post en", "relay"): (1024, 1760),
    ("post en", "member"): (1024, 1840),
    ("post de", "relay"): (1024, 1760),
    ("post de", "member"): (1024, 1840),
    ("post cjk", "relay"): (4096, 5860),
    ("post cjk", "member"): (4096, 5940),
    ("post cjk 600", "relay"): (4096, 5860),
    ("post cjk 600", "member"): (4096, 5940),
    ("post en + link preview", "relay"): (4096, 5860),
    ("post en + link preview", "member"): (4096, 5940),
    ("comment", "member"): (1024, 1840),
}


def test_compressed_frame_sizes_stay_within_budget() -> None:
    rows = size_table(PAYLOAD_SUITE_ZLIB)
    _print_table(rows)
    assert {(case, path) for case, path, _b, _s in rows} == set(BUDGET_ZLIB)
    for case, path, bucket, size in rows:
        want_bucket, max_bytes = BUDGET_ZLIB[(case, path)]
        assert bucket == want_bucket, (case, path, bucket)
        assert size <= max_bytes, (case, path, size)


#: ``ws_binary1`` frames of the compressed items (what a follower whose
#: socket asked for binary frames receives): the ciphertext travels raw
#: instead of base64: ~17 % off a 1 KiB-bucket frame (1 751 → 1 450 B) and
#: ~22 % off a 4 KiB one (5 823 → 4 522 B) — the rest is the JSON header.
BUDGET_BINARY: dict[tuple[str, str], int] = {
    ("post en", "relay"): 1460,
    ("post en", "member"): 1520,
    ("post de", "relay"): 1460,
    ("post de", "member"): 1520,
    ("post cjk", "relay"): 4530,
    ("post cjk", "member"): 4590,
    ("post cjk 600", "relay"): 4530,
    ("post cjk 600", "member"): 4590,
    ("post en + link preview", "relay"): 4530,
    ("post en + link preview", "member"): 4590,
    ("comment", "member"): 1520,
}


def test_binary_frame_sizes_stay_within_budget() -> None:
    rows = size_table(PAYLOAD_SUITE_ZLIB, binary=True)
    _print_table(rows)
    text = {(c, p): s for c, p, _b, s in size_table(PAYLOAD_SUITE_ZLIB)}
    assert {(case, path) for case, path, _b, _s in rows} == set(BUDGET_BINARY)
    for case, path, _bucket, size in rows:
        assert size <= BUDGET_BINARY[(case, path)], (case, path, size)
        assert size < text[(case, path)] * 0.85, (case, path)


#: Member items to a follower whose hello listed ``space_item_no_cert``: the
#: GFS leaves the plaintext v1 ``writer_cert`` (279 B of JSON, the server's
#: own authorization material) out of the frame: 294 B, ~19 % off a 1 KiB-bucket
#: binary frame (1 512 → 1 218 B). The relay path never carried it.
BUDGET_NO_FRAME_CERT: dict[str, int] = {
    "post en": 1220,
    "post de": 1220,
    "post cjk": 4300,
    "post cjk 600": 4300,
    "post en + link preview": 4300,
    "comment": 1220,
}


def test_member_frames_without_the_frame_cert_stay_within_budget() -> None:
    rows = _size_table(PAYLOAD_SUITE_ZLIB)
    with_cert = {
        c: s
        for c, p, _b, s in size_table(PAYLOAD_SUITE_ZLIB, binary=True)
        if p == "member"
    }
    lean = {}
    for case, path, _bucket, frame in rows:
        if path != "member":
            continue
        parsed = orjson.loads(frame)
        assert "writer_cert" in parsed
        del parsed["writer_cert"]
        raw = encode_binary_frame(parsed)
        assert raw is not None
        lean[case] = len(raw)
    print()
    for case, size in lean.items():
        print(f"{case:<26}member  {with_cert[case]:>6} → {size:>6}")
    assert set(lean) == set(BUDGET_NO_FRAME_CERT)
    for case, size in lean.items():
        assert size <= BUDGET_NO_FRAME_CERT[case], (case, size)
        assert with_cert[case] - size >= 290, case


def test_compression_never_needs_a_bigger_bucket() -> None:
    plain = {(c, p): b for c, p, b, _s in size_table()}
    for case, path, bucket, _size in size_table(PAYLOAD_SUITE_ZLIB):
        assert bucket <= plain[(case, path)], (case, path)


def test_a_compressed_frame_opens_to_the_same_item() -> None:
    w = _world()
    inner = _post_inner(w, TEXT_DE, member=True)
    cert = _cert(w)
    body = {"item_type": "post", "inner": {**inner, WRITER_CERT_FIELD: cert.to_wire()}}
    sealed = seal_item_plaintext(body, suite=PAYLOAD_SUITE_ZLIB)
    assert sealed is not None
    assert orjson.loads(open_item_plaintext(sealed, PAYLOAD_SUITE_ZLIB)) == body


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

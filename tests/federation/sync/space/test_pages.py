"""``SyncPageService`` — seal, carry, verify, store, root check, status
(``docs/protocol/sync-pages.md`` Phase 2)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from dataclasses import replace

import orjson

from socialhome.canonical_json import canonical_str
from socialhome.crypto import ed25519_public_key
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.space_sync_page import StoredSyncPage, state_with_page
from socialhome.domain.space_sync_status import SpaceSyncState
from socialhome.federation.sync.space.page_state import BucketRows
from socialhome.federation.sync.space.pages import SyncPageService
from socialhome.sync_page import (
    PAGE_MIN_INTERVAL_S,
    VECTOR_BYTES,
    ListingRow,
    apply_delta,
    bucket_hash,
    delta_from_wire,
    listing_to_wire,
    parse_page,
    parse_page_ref,
    root_of,
    row_key,
    sign_page,
    vector_from_wire,
)

SPACE = "sp-1"
HOST = "host-1"
ADMIN = "admin-1"
ME = "member-1"
SEED = bytes(range(32))
PK_HEX = ed25519_public_key(SEED).hex()
T0 = 1_800_000_000_000


def _vec(fill: int) -> bytes:
    return bytes([fill]) * VECTOR_BYTES


def _space(**over) -> Space:
    fields = {
        "id": SPACE,
        "name": "S",
        "owner_instance_id": HOST,
        "owner_username": "anna",
        "identity_public_key": PK_HEX,
        "config_sequence": 0,
        "features": SpaceFeatures(),
        "space_type": SpaceType.PRIVATE,
        "join_mode": JoinMode.INVITE_ONLY,
        "authority_key_epoch": 3,
    }
    fields.update(over)
    return Space(**fields)


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, StoredSyncPage] = {}

    async def get(self, space_id):
        return self.rows.get(space_id)

    async def put(self, page):
        self.rows[page.space_id] = page

    async def delete(self, space_id):
        self.rows.pop(space_id, None)


class _Spaces:
    def __init__(self, space: Space, seed: bytes | None = None) -> None:
        self.space = space
        self.seed = seed
        self.local: list[str] = []
        self.members: list[SpaceMember] = []

    async def get(self, space_id):
        return self.space if space_id == self.space.id else None

    async def get_space_seed(self, space_id):
        return self.seed

    async def list_local_member_user_ids(self, space_id):
        return self.local

    async def list_members(self, space_id):
        return self.members


class _Seqs:
    def __init__(self) -> None:
        self.seq = 100
        self.applied: dict[str, int] = {}

    async def current_seq(self):
        return self.seq

    async def applied_seq(self, space_id, instance_id):
        return self.applied.get(instance_id)


class _Crypto:
    """Seals bound to (space, sync_id) like ``encrypt_chunk``'s AAD."""

    epoch = 7

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        blob = f"{space_id}|{sync_id}|".encode() + plaintext
        return self.epoch, base64.b64encode(blob).decode()

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        blob = base64.b64decode(ciphertext)
        head = f"{space_id}|{sync_id}|".encode()
        if epoch != self.epoch or not blob.startswith(head):
            raise ValueError("InvalidTag")
        return blob[len(head) :]


class _State:
    def __init__(self) -> None:
        self.vec = _vec(1)
        self.rows = 10
        self.calls = 0
        self.cutoffs: list = []
        self.window_cutoff: str | None = None

    async def cutoff(self, space_id):
        return self.window_cutoff

    async def vector(self, space_id, *, cutoff=None):
        self.calls += 1
        self.cutoffs.append(cutoff)
        return self.vec, self.rows

    async def rows_in(self, space_id, buckets, *, cutoff=None):
        return BucketRows(rows={i: [] for i in buckets}, gone=set())


class _Exporter:
    resource = "posts"

    def __init__(self) -> None:
        self.changed_above: int | None = None

    async def list_records(self, space_id):
        return []

    async def iter_changed(self, space_id, since):
        if self.changed_above is not None and self.changed_above > since:
            yield [{"id": "p"}]


class _Env:
    def __init__(self, *, own: str = HOST, seed: bytes | None = SEED) -> None:
        self.now = T0
        self.mono = 0.0
        self.repo = _Repo()
        self.spaces = _Spaces(_space(), seed)
        self.seqs = _Seqs()
        self.crypto = _Crypto()
        self.state = _State()
        self.exporter = _Exporter()
        self.changes: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.slept: list[float] = []
        self.hosts: list[str] = [HOST]

        async def on_change(space_id):
            self.changes.append(space_id)

        #: When set, a spacing wait blocks until it is released.
        self.gate: asyncio.Event | None = None

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            if self.gate is not None:
                await self.gate.wait()
            self.mono += seconds

        async def hosts(space):
            return list(self.hosts)

        async def request_sync(space_id: str, host: str) -> None:
            self.requests.append((space_id, host))

        self.svc = SyncPageService(
            repo=self.repo,
            spaces=self.spaces,
            seqs=self.seqs,
            crypto=self.crypto,
            state=self.state,
            exporters={"posts": self.exporter},
            own_instance_id=own,
            on_change=on_change,
            clock_ms=lambda: self.now,
            monotonic=lambda: self.mono,
            sleep=sleep,
        )
        self.svc.attach_hosts(hosts)

    def held(self):
        row = self.repo.rows.get(SPACE)
        return None if row is None else parse_page(orjson.loads(row.page_json))


V = FederationCapability.MIN_FOR_SYNC_PAGES


async def _sentinel(env: _Env, *, ref=None, version=V, sync_id="s-1"):
    sealed = await env.svc.sentinel_page(
        space_id=SPACE,
        sync_id=sync_id,
        requester_version=version,
        requester_ref=parse_page_ref(ref) if ref is not None else None,
    )
    await env.svc.wait_idle()
    return sealed


def _open(env: _Env, sealed, sync_id="s-1") -> dict:
    blob = base64.b64decode(sealed["payload"])
    return orjson.loads(blob[len(f"{SPACE}|{sync_id}|") :])


def _signed(*, vector=None, seq=50, issued_at=T0, prev_root=None, signer=HOST, epoch=3):
    return sign_page(
        space_id=SPACE,
        epoch=epoch,
        signer=signer,
        seq=seq,
        issued_at=issued_at,
        cutoff=None,
        rows=4,
        vector=vector if vector is not None else _vec(2),
        prev_root=prev_root,
        space_seed=SEED,
    )


async def _sealed_for_member(page, *, vector=None, delta=None, sync_id="s-1"):
    body: dict = {"page": page.to_wire()}
    if delta is not None:
        body["delta"] = delta
    else:
        body["vector"] = (
            base64.urlsafe_b64encode(vector if vector is not None else _vec(2))
            .decode()
            .rstrip("=")
        )
    epoch, ct = await _Crypto().encrypt_chunk(
        space_id=SPACE, sync_id=sync_id, plaintext=orjson.dumps(body)
    )
    return {"epoch": epoch, "payload": ct}


# ── Seal (signer) ────────────────────────────────────────────────────────


async def test_the_owner_seals_its_first_page():
    env = _Env()
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    assert (page.signer, page.epoch, page.seq, page.rows) == (HOST, 3, 100, 10)
    assert page.root == root_of(env.state.vec) and page.prev_root is None
    row = env.repo.rows[SPACE]
    assert row.vector == env.state.vec and row.delta is None
    assert env.changes == [SPACE]


async def test_a_plain_member_never_seals():
    env = _Env(own=ME, seed=None)
    assert await env.svc.seal_if_due(SPACE) is None
    env = _Env(own=ME)  # holds a seed but delegation is off
    assert await env.svc.seal_if_due(SPACE) is None
    assert env.state.calls == 0


async def test_a_delegated_admin_with_the_seed_seals():
    env = _Env(own=ADMIN)
    env.spaces.space = _space(
        features=replace(SpaceFeatures(), delegated_admin_authority=True)
    )
    env.spaces.local = ["u-admin"]
    env.spaces.members = [
        SpaceMember(
            space_id=SPACE,
            user_id="u-admin",
            role=SpaceRole.ADMIN.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    ]
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.signer == ADMIN


async def test_a_seed_for_another_key_never_signs():
    env = _Env(seed=bytes(32))
    assert await env.svc.seal_if_due(SPACE) is None


async def test_no_new_seal_while_the_space_is_unchanged_or_the_page_is_young():
    env = _Env()
    first = await env.svc.seal_if_due(SPACE)
    assert first is not None
    env.now += (PAGE_MIN_INTERVAL_S + 1) * 1000
    assert await env.svc.seal_if_due(SPACE) is None  # unchanged
    env.exporter.changed_above = 150
    env.now = T0 + 60_000  # changed, but only a minute old
    assert await env.svc.seal_if_due(SPACE) is None
    assert env.state.calls == 1


async def test_a_changed_space_seals_after_15_minutes_with_a_delta():
    env = _Env()
    first = await env.svc.seal_if_due(SPACE)
    env.exporter.changed_above = 150
    env.seqs.seq = 160
    env.now += PAGE_MIN_INTERVAL_S * 1000
    new_vec = bytearray(env.state.vec)
    new_vec[32 * 9 : 32 * 10] = b"\x09" * 32
    env.state.vec = bytes(new_vec)
    second = await env.svc.seal_if_due(SPACE)
    assert second is not None and first is not None
    assert second.prev_root == first.root and second.seq == 160
    row = env.repo.rows[SPACE]
    assert row.delta == bytes([9]) + b"\x09" * 32


async def test_a_new_cutoff_day_reseals_a_young_page():
    env = _Env()
    env.state.window_cutoff = "2026-10-01 23:59:00"
    await env.svc.seal_if_due(SPACE)
    env.exporter.changed_above = 150
    env.now += 60_000
    env.state.window_cutoff = "2026-10-02 00:01:00"
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.cutoff == "2026-10-02 00:01:00"


async def test_a_fresh_page_of_another_signer_holds_our_seal_back():
    """A delegated admin that holds the owner's page (synced as a member)
    does not seal on top of every owner page: only once it is 15 min old."""
    env = _Env()
    other = _signed(signer=ADMIN, issued_at=T0, vector=_vec(4))
    env.repo.rows[SPACE] = StoredSyncPage(
        SPACE, canonical_str(other.to_wire()), _vec(4)
    )
    assert await env.svc.seal_if_due(SPACE) is None
    env.now += PAGE_MIN_INTERVAL_S * 1000
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.signer == HOST and page.prev_root is None


async def test_an_old_epoch_page_is_not_held_and_a_new_one_is_sealed():
    env = _Env()
    await env.svc.seal_if_due(SPACE)
    env.spaces.space = _space(authority_key_epoch=4)
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.epoch == 4 and page.prev_root is None


# ── Carry (sentinel) ─────────────────────────────────────────────────────


async def test_an_older_requester_gets_no_page_fields():
    env = _Env()
    await env.svc.seal_if_due(SPACE)
    assert await _sentinel(env, version=V - 1) is None


async def test_a_requester_without_a_page_gets_the_page_and_the_full_vector():
    env = _Env()
    page = await env.svc.seal_if_due(SPACE)
    sealed = await _sentinel(env)
    assert sealed is not None and sealed["epoch"] == _Crypto.epoch
    body = _open(env, sealed)
    assert parse_page(body["page"]) == page
    assert vector_from_wire(body["vector"]) == env.state.vec
    assert "delta" not in body


async def test_the_page_is_sealed_never_in_the_clear():
    env = _Env()
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    sealed = await _sentinel(env)
    assert set(sealed) == {"epoch", "payload"}
    assert canonical_str(page.to_wire())[:40] not in canonical_str(sealed)


async def test_only_when_newer_than_the_requesters_page():
    env = _Env()
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    assert await _sentinel(env, ref=page.ref()) is None
    newer = _signed(issued_at=T0 + 10, seq=999, vector=_vec(9))
    assert await _sentinel(env, ref=newer.ref()) is None
    older = _signed(issued_at=T0 - 10, seq=1, vector=_vec(8))
    assert await _sentinel(env, ref=older.ref()) is not None


async def test_the_previous_page_gets_only_the_delta():
    env = _Env()
    first = await env.svc.seal_if_due(SPACE)
    env.exporter.changed_above = 150
    env.now += PAGE_MIN_INTERVAL_S * 1000
    new_vec = bytearray(env.state.vec)
    new_vec[0:32] = b"\x05" * 32
    env.state.vec = bytes(new_vec)
    second = await env.svc.seal_if_due(SPACE)
    assert first is not None and second is not None
    body = _open(env, await _sentinel(env, ref=first.ref()))
    assert "vector" not in body
    delta = delta_from_wire(body["delta"])
    assert apply_delta(_vec(1), delta) == env.state.vec
    # Two pages behind (a ref that is not the previous page): full vector.
    stale = _signed(issued_at=T0 - 5, vector=_vec(3))
    body = _open(env, await _sentinel(env, ref=stale.ref()))
    assert "vector" in body


async def test_a_sentinel_starts_a_due_seal_in_the_background():
    env = _Env()
    assert await _sentinel(env) is None  # nothing held yet
    assert env.held() is not None  # sealed in the background meanwhile


# ── Verify and store (member) ────────────────────────────────────────────


async def test_a_member_stores_a_verified_page_and_its_vector():
    env = _Env(own=ME, seed=None)
    page = _signed()
    ok = await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(page),
    )
    await env.svc.wait_idle()
    assert ok and env.held() == page
    assert env.repo.rows[SPACE].vector == _vec(2)
    assert env.repo.rows[SPACE].delta is None


async def test_a_forged_page_is_refused(caplog):
    env = _Env(own=ME, seed=None)
    wire = _signed().to_wire()
    wire["rows"] = 1
    sealed = await _sealed_for_member(_signed())
    body = {"page": wire, "vector": base64.urlsafe_b64encode(_vec(2)).decode()}
    sealed = {
        "epoch": 7,
        "payload": (
            await _Crypto().encrypt_chunk(
                space_id=SPACE, sync_id="s-1", plaintext=orjson.dumps(body)
            )
        )[1],
    }
    with caplog.at_level(logging.WARNING):
        ok = await env.svc.receive(
            space_id=SPACE, sync_id="s-1", provider=HOST, sealed=sealed
        )
    assert not ok and env.held() is None
    assert "refused" in caplog.text


async def test_an_old_epoch_page_is_refused():
    env = _Env(own=ME, seed=None)
    ok = await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(_signed(epoch=2)),
    )
    assert not ok and env.held() is None


async def test_an_unknown_page_suite_is_refused():
    env = _Env(own=ME, seed=None)
    wire = _signed().to_wire()
    wire["page_suite"] = "sha256-b512-c1"
    body = {"page": wire, "vector": base64.urlsafe_b64encode(_vec(2)).decode()}
    _e, ct = await _Crypto().encrypt_chunk(
        space_id=SPACE, sync_id="s-1", plaintext=orjson.dumps(body)
    )
    ok = await env.svc.receive(
        space_id=SPACE, sync_id="s-1", provider=HOST, sealed={"epoch": 7, "payload": ct}
    )
    assert not ok and env.held() is None


async def test_a_vector_that_does_not_hash_to_the_root_is_refused():
    env = _Env(own=ME, seed=None)
    sealed = await _sealed_for_member(_signed(vector=_vec(2)), vector=_vec(3))
    assert not await env.svc.receive(
        space_id=SPACE, sync_id="s-1", provider=HOST, sealed=sealed
    )


async def test_a_page_lifted_into_another_session_does_not_open():
    env = _Env(own=ME, seed=None)
    sealed = await _sealed_for_member(_signed(), sync_id="s-other")
    assert not await env.svc.receive(
        space_id=SPACE, sync_id="s-1", provider=HOST, sealed=sealed
    )


async def test_an_older_page_never_replaces_the_newer_one():
    env = _Env(own=ME, seed=None)
    newer = _signed(issued_at=T0 + 100, seq=60, vector=_vec(4))
    older = _signed(issued_at=T0, seq=50, vector=_vec(2))
    for page, vec in ((newer, _vec(4)), (older, _vec(2))):
        await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed=await _sealed_for_member(page, vector=vec),
        )
    await env.svc.wait_idle()
    assert env.held() == newer


async def test_a_delta_applies_to_the_previous_page():
    env = _Env(own=ME, seed=None)
    first = _signed(vector=_vec(2))
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(first),
    )
    new_vec = bytearray(_vec(2))
    new_vec[32 * 3 : 32 * 4] = b"\x07" * 32
    second = _signed(
        vector=bytes(new_vec), seq=60, issued_at=T0 + 1, prev_root=first.root
    )
    delta = [[3, base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("=")]]
    ok = await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(second, delta=delta),
    )
    await env.svc.wait_idle()
    assert ok and env.held() == second
    assert env.repo.rows[SPACE].vector == bytes(new_vec)


async def test_a_bogus_delta_cannot_roll_a_member_back():
    """Review finding: a relay sending a newer page with an unusable delta
    must not make us drop ours (and then take an older page)."""
    env = _Env(own=ME, seed=None)
    p2 = _signed(vector=_vec(2), seq=20, issued_at=T0 + 20)
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(p2),
    )
    p4 = _signed(vector=_vec(4), seq=40, issued_at=T0 + 40, prev_root=b"\x03" * 32)
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider="evil",
        sealed=await _sealed_for_member(p4, delta=[]),
    )
    p1 = _signed(vector=_vec(1), seq=10, issued_at=T0 + 10)
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider="evil",
        sealed=await _sealed_for_member(p1),
    )
    await env.svc.wait_idle()
    assert env.held() == p2


async def test_a_signer_keeps_its_own_page_over_another_signers():
    env = _Env()
    own = await env.svc.seal_if_due(SPACE)
    assert own is not None
    other = _signed(signer=ADMIN, issued_at=T0 + 5_000, seq=999, vector=_vec(7))
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(other, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    assert env.held() == own


async def test_a_page_that_does_not_open_is_a_one_line_refusal(caplog):
    env = _Env(own=ME, seed=None)
    with caplog.at_level(logging.WARNING):
        assert not await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed={"epoch": 99, "payload": "garbage"},
        )
    await env.svc.wait_idle()
    assert "does not open" in caplog.text and "Traceback" not in caplog.text


async def test_a_delta_without_its_previous_page_is_refused_and_ours_kept():
    env = _Env(own=ME, seed=None)
    held = _signed(vector=_vec(2))
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(held),
    )
    second = _signed(vector=_vec(5), seq=70, issued_at=T0 + 9, prev_root=b"\x01" * 32)
    ok = await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(second, delta=[]),
    )
    await env.svc.wait_idle()
    assert not ok and env.held() == held  # next BEGIN names it: full vector


async def test_equivocation_is_detected_logged_and_flagged(caplog):
    env = _Env(own=ME, seed=None)
    a = _signed(vector=_vec(2), seq=50, issued_at=T0)
    b = _signed(vector=_vec(6), seq=50, issued_at=T0 + 5)
    await env.svc.receive(
        space_id=SPACE, sync_id="s-1", provider=HOST, sealed=await _sealed_for_member(a)
    )
    with caplog.at_level(logging.WARNING):
        await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider="member-2",
            sealed=await _sealed_for_member(b, vector=_vec(6)),
        )
    await env.svc.wait_idle()
    assert "EQUIVOCATION" in caplog.text
    assert a.page_hash() in caplog.text and b.page_hash() in caplog.text
    assert env.held() == b  # the newer issued_at is kept
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.conflict


async def test_malformed_page_sealed_is_refused():
    env = _Env(own=ME, seed=None)
    for bad in (
        "x",
        {"epoch": "7", "payload": "x"},
        {"epoch": 7, "payload": "x" * 30000},
    ):
        assert not await env.svc.receive(
            space_id=SPACE, sync_id="s-1", provider=HOST, sealed=bad
        )
    await env.svc.wait_idle()


# ── Compare, bucket listings and status ──────────────────────────────────


async def _take(env: _Env, page, vector) -> bool:
    stored = await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(page, vector=vector),
    )
    await env.svc.wait_idle()
    return stored


OLD = "2026-01-01T00:00:00.000000+00:00"  # before the page (T0 = 2027-01-15)
NEW = "2027-02-01T00:00:00.000000+00:00"  # after the page


def _row(rid: str, version: int = 1, marker: str | None = OLD, resource="posts"):
    leaf = hashlib.sha256(f"{resource}|{rid}|{version}".encode()).digest()
    return ListingRow(resource, rid, leaf, marker)


def _vector_of(rows) -> bytes:
    buckets: list[list[bytes]] = [[] for _ in range(256)]
    for r in rows:
        buckets[row_key(r.resource, r.row_id)[0]].append(r.leaf)
    return b"".join(bucket_hash(i, leaves) for i, leaves in enumerate(buckets))


def _bucket(r) -> int:
    return row_key(r.resource, r.row_id)[0]


def _ids_in_distinct_buckets(n: int, prefix: str) -> list[str]:
    out: list[str] = []
    seen: set[int] = set()
    k = 0
    while len(out) < n:
        rid = f"{prefix}{k}"
        k += 1
        i = row_key("posts", rid)[0]
        if i not in seen:
            seen.add(i)
            out.append(rid)
    return out


class _Listed:
    """A member env whose own rows and the host's page are real rows."""

    def __init__(self, host_rows, member_rows, *, gone=(), signer=HOST) -> None:
        self.env = _Env(own=ME, seed=None)
        self.host_rows = list(host_rows)
        self.page_vector = _vector_of(self.host_rows)
        self.env.state.vec = _vector_of(member_rows)
        self.member_rows = list(member_rows)
        self.gone = set(gone)
        self.forge = None  # a function(records) -> records
        self.signer = signer
        self.rounds = 0

        async def rows_in(space_id, buckets, *, cutoff=None):
            wanted = set(buckets)
            rows = {
                i: [r for r in self.member_rows if _bucket(r) in {i}] for i in wanted
            }
            return BucketRows(rows=rows, gone=set(self.gone))

        self.env.state.rows_in = rows_in

    @property
    def asks(self) -> list[tuple[str, list[int]]]:
        """What the next round's BEGIN to each acting host would ask."""
        out = []
        for host in [*self.env.hosts, HOST, ADMIN]:
            buckets = self.env.svc.buckets_for(SPACE, host)
            if buckets and (host, buckets) not in out:
                out.append((host, buckets))
        return out

    async def take_page(self) -> None:
        page = _signed(vector=self.page_vector, signer=self.signer)
        assert await _take(self.env, page, self.page_vector)

    async def deliver(self) -> None:
        """One round: the BEGIN asks the buckets, the host answers at the end
        of its stream like an honest host (or ``forge``), then the sentinel."""
        ((host, buckets),) = self.asks
        records = [
            listing_to_wire(i, [r for r in self.host_rows if _bucket(r) == i])
            for i in buckets
        ]
        if self.forge is not None:
            records = self.forge(records)
        self.rounds += 1
        sync_id = f"R{self.rounds}"
        await self.env.svc.take_listing(
            space_id=SPACE, sync_id=sync_id, provider=host, records=records
        )
        await self.env.svc.listing_done(space_id=SPACE, sync_id=sync_id, provider=host)

    async def position(self):
        return await self.env.svc.position(self.env.spaces.space, [HOST])


async def test_a_new_page_is_compared_and_a_match_reads_up_to_date():
    rows = [_row(f"p{k}") for k in range(20)]
    t = _Listed(rows, rows)
    await t.take_page()
    assert t.env.state.calls == 1
    assert t.env.state.cutoffs == [None]  # the page's cutoff, not our clock
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing, pos.differing_buckets) == (
        True,
        0,
        0,
    )
    assert state_with_page(SpaceSyncState.HOST_OFFLINE, pos) is (
        SpaceSyncState.UP_TO_DATE
    )
    assert t.asks == [] and t.env.requests == []
    assert t.env.changes[-1] == SPACE  # the status rechecks after the compare


async def test_a_restarted_member_compares_its_held_page_once():
    """Root cause of a demo failure: compare results live in memory, so
    after a restart the page held in the database had no verdict and,
    with no newer page coming, never got one (``match: null`` forever).
    The next sentinel compares a held page that has no verdict — once."""
    rows = [_row("p1")]
    t = _Listed(rows, rows)
    await t.take_page()
    repo, state = t.env.repo, t.env.state
    restarted = _Listed(rows, rows)  # a fresh service, same stored page
    restarted.env.repo.rows = repo.rows
    restarted.env.svc._repo = repo  # the same database
    assert (await restarted.position()).match is None
    for _ in range(2):
        assert not await restarted.env.svc.receive(
            space_id=SPACE, sync_id="s-9", provider=HOST, sealed=None
        )
        await restarted.env.svc.wait_idle()
    assert restarted.env.state.calls == 1  # once, not per sentinel
    assert (await restarted.position()).match is True
    assert state.calls == 1


async def test_reading_the_status_of_an_unjudged_page_compares_it_once():
    """Demo root cause, second half: after a restart the host may refuse our
    BEGINs for a while (its hourly budget), so no sentinel comes. Asking for
    the status is the event: a held page without a verdict is compared."""
    rows = [_row("p1")]
    t = _Listed(rows, rows)
    await t.take_page()
    restarted = _Listed(rows, rows)
    restarted.env.repo.rows = t.env.repo.rows
    restarted.env.svc._repo = t.env.repo
    assert (await restarted.position()).match is None  # starts the compare
    await restarted.env.svc.wait_idle()
    assert (await restarted.position()).match is True
    await restarted.env.svc.wait_idle()
    assert restarted.env.state.calls == 1


async def test_a_repeated_page_is_not_compared_again():
    """A sentinel without a page (we hold it) or the same page again: no
    compare — the next comparison comes only with the next page."""
    rows = [_row("p1")]
    t = _Listed(rows, rows)
    await t.take_page()
    page = _signed(vector=t.page_vector)
    assert not await _take(t.env, page, t.page_vector)
    assert not await t.env.svc.receive(
        space_id=SPACE, sync_id="s-1", provider=HOST, sealed=None
    )
    await t.env.svc.wait_idle()
    assert t.env.state.calls == 1


async def test_a_busy_member_ahead_in_many_buckets_fetches_nothing_and_matches():
    """Live events between pages: every differing bucket holds a row changed
    after the page — explained locally, no listing on the wire."""
    old = [_row(f"p{k}") for k in range(30)]
    newer = [_row(f"live{k}", marker=NEW) for k in range(40)]
    edited = [_row("p3", version=2, marker=NEW)]
    member = [r for r in old if r.row_id != "p3"] + edited + newer
    t = _Listed(old, member)
    await t.take_page()
    pos = await t.position()
    assert pos is not None and pos.differing_buckets and pos.differing_buckets > 30
    assert (pos.match, pos.missing) == (True, 0)
    assert t.asks == [] and t.env.requests == []


async def test_a_signer_not_our_host_and_a_member_ahead_matches():
    old = [_row(f"p{k}") for k in range(5)]
    t = _Listed(old, old + [_row("live", marker=NEW)], signer=ADMIN)
    t.env.hosts = ["owner-x"]
    await t.take_page()
    pos = await t.position()
    assert pos is not None and pos.match is True
    assert t.asks == []


async def test_a_missing_row_is_proven_by_one_listing_on_the_next_round():
    """A bucket that differs with no newer row of ours: only it is asked, on
    the next round's BEGIN — never a BEGIN of its own; the listing proves
    the row missing — behind. No extra BEGIN after it either: the rounds go
    on anyway. Applied stream watermarks play no part."""
    present = [_row(f"p{k}") for k in range(10)]
    lost = _row("lost")
    t = _Listed(present + [lost], present)
    t.env.seqs.applied[HOST] = 10**6  # far past the page: no effect now
    await t.take_page()
    assert t.asks == [(HOST, [_bucket(lost)])]  # only the differing bucket
    pos = await t.position()
    assert pos is not None and pos.match is None  # listing pending
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (False, 1)
    assert state_with_page(SpaceSyncState.UP_TO_DATE, pos) is SpaceSyncState.BEHIND
    assert t.asks == []  # answered: the next round asks nothing


async def test_a_row_the_same_round_delivers_is_not_missing():
    """The listing ends the stream that may itself bring the row: our rows
    are read at the sentinel, after the round's rows are applied."""
    lost = _row("lost")
    t = _Listed([lost], [])
    await t.take_page()
    t.member_rows.append(lost)  # the round's stream delivered it
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (True, 0)


async def test_a_round_without_listings_keeps_them_asked():
    t = _Listed([_row("lost")], [])
    await t.take_page()
    asked = t.asks
    await t.env.svc.listing_done(space_id=SPACE, sync_id="R0", provider=HOST)
    assert t.asks == asked and (await t.position()).match is None


async def test_an_older_local_version_is_missing_a_newer_one_is_ahead():
    host = [_row("p1", version=2, marker="2026-06-01T00:00:00.000000+00:00")]
    t = _Listed(host, [_row("p1", version=1, marker=OLD)])
    await t.take_page()
    await t.deliver()
    assert (await t.position()).match is False
    # The same row edited here after the host's version (still before the
    # page) and a row of ours the page does not list: ahead.
    mine = [
        _row("p1", version=3, marker="2026-07-01T00:00:00.000000+00:00"),
        _row("ours", marker=OLD),
    ]
    t = _Listed(host, mine)
    await t.take_page()
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (True, 0)


async def test_a_row_deleted_here_after_the_page_is_not_missing():
    host = [_row("p1"), _row("p2")]
    t = _Listed(host, [_row("p2")], gone={("posts", "p1")})
    await t.take_page()
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (True, 0)


async def test_at_most_eight_listings_per_compare():
    lost = [_row(rid) for rid in _ids_in_distinct_buckets(12, "lost")]
    t = _Listed(lost, [])
    await t.take_page()
    ((_host, buckets),) = t.asks
    assert len(buckets) == 8
    assert set(buckets) <= {_bucket(r) for r in lost}
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (False, 8)


async def test_a_forged_or_truncated_listing_is_refused(caplog):
    host = [_row("lost"), _row("p1")]
    same = [r for r in host if _bucket(r) == _bucket(host[0])]
    for forge in (
        lambda recs: [{**recs[0], "rows": recs[0]["rows"][:-1]}],  # truncated
        lambda recs: [
            {**recs[0], "rows": [[*recs[0]["rows"][0][:2], "A" * 43, None]]}
        ],  # forged leaf
        lambda recs: [*recs, *recs],  # repeated bucket
    ):
        t = _Listed(host, [r for r in host if r not in same[:1]])
        await t.take_page()
        t.forge = forge
        with caplog.at_level(logging.WARNING):
            await t.deliver()
        pos = await t.position()
        assert pos is not None and pos.match is None  # unresolved, not missing
        assert t.env.requests == []
    assert "refused the listings" in caplog.text


async def test_a_hosts_current_listing_settles_a_changed_bucket():
    """The host changed the bucket since the page (it cannot prove it), so
    it lists the bucket as it is now — trusted like its stream: a row of it
    we lack, or hold in an older version, is missing; ours newer is not."""
    t = _Listed([_row("lost")], [])
    await t.take_page()
    t.forge = lambda recs: [
        listing_to_wire(r["i"], [_row("lost", version=2)], current=True) for r in recs
    ]
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (False, 1)

    mid = "2026-06-01T00:00:00.000000+00:00"
    for ours, expect in (
        (_row("lost", version=1, marker=OLD), (False, 1)),  # older than the host's
        (_row("lost", version=3, marker="2026-07-01T00:00:00.000000+00:00"), (True, 0)),
    ):
        t = _Listed([_row("lost", version=0)], [ours])
        await t.take_page()
        t.forge = lambda recs: [
            listing_to_wire(r["i"], [_row("lost", version=2, marker=mid)], current=True)
            for r in recs
        ]
        await t.deliver()
        pos = await t.position()
        assert pos is not None and (pos.match, pos.missing) == expect


async def test_an_unasked_listing_is_dropped():
    host = [_row("lost")]
    t = _Listed(host, [])
    await t.take_page()
    await t.env.svc.take_listing(
        space_id=SPACE, sync_id="R1", provider="evil", records=[{"i": 0}]
    )
    await t.env.svc.take_listing(
        space_id="sp-other", sync_id="R1", provider=HOST, records=[{"i": 0}]
    )
    await t.deliver()
    assert (await t.position()).missing == 1


async def test_the_host_lists_only_buckets_unchanged_since_the_page():
    env = _Env()
    host_rows = [_row(f"p{k}") for k in range(6)]
    env.state.vec = _vector_of(host_rows)

    async def rows_in(space_id, buckets, *, cutoff=None):
        rows = {i: [r for r in host_rows if _bucket(r) == i] for i in buckets}
        return BucketRows(rows=rows, gone=set())

    env.state.rows_in = rows_in
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    b0, b1 = _bucket(host_rows[0]), _bucket(host_rows[1])
    host_rows[1] = _row("p1", version=2)  # changed here since the page
    recs = await env.svc.listing_records(
        space_id=SPACE,
        page_ref=parse_page_ref(page.ref()),
        buckets=[b0, b1],
        requester_version=V,
    )
    by_bucket = {r["i"]: r for r in recs}
    assert by_bucket[b0]["held"] is True and by_bucket[b1]["held"] is False
    # As the space's host it lists the changed bucket as it is now.
    assert by_bucket[b1]["current"] is True
    assert by_bucket[b1]["rows"][0][1] == "p1"
    # Another page than ours: nothing held.
    other = _signed(vector=_vec(9))
    recs = await env.svc.listing_records(
        space_id=SPACE,
        page_ref=parse_page_ref(other.ref()),
        buckets=[b0],
        requester_version=V,
    )
    assert recs == [{"i": b0, "held": False}]
    # An older requester gets no listing at all.
    assert (
        await env.svc.listing_records(
            space_id=SPACE,
            page_ref=parse_page_ref(page.ref()),
            buckets=[b0],
            requester_version=V - 1,
        )
        == []
    )


async def test_compares_are_coalesced_to_one_per_15_minutes():
    """Pages arriving faster than the seal cadence: the compare after the
    first waits out the 15 min once and then compares the newest page."""
    env = _Env(own=ME, seed=None)
    env.state.vec = _vec(2)
    await _take(env, _signed(vector=_vec(2), seq=10, issued_at=T0), _vec(2))
    assert env.state.calls == 1
    env.mono += 60  # one minute later, two newer pages
    env.state.vec = _vec(5)
    env.gate = asyncio.Event()
    for seq, fill in ((20, 4), (30, 5)):
        assert await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed=await _sealed_for_member(
                _signed(vector=_vec(fill), seq=seq, issued_at=T0 + seq),
                vector=_vec(fill),
            ),
        )
    assert env.state.calls == 1  # waiting out the 15 min
    env.gate.set()
    await env.svc.wait_idle()
    assert env.state.calls == 2  # one deferred compare, not two
    assert env.slept == [PAGE_MIN_INTERVAL_S - 60]
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.seq == 30 and pos.match is True


async def test_the_signer_never_compares_its_own_page():
    env = _Env()
    await env.svc.seal_if_due(SPACE)
    calls = env.state.calls
    assert await env.svc.compare(SPACE) is None
    assert env.state.calls == calls


async def test_wait_idle_never_spins_on_finished_tasks():
    """A finished task leaves the set only when its done-callback runs:
    wait_idle must wait for the unfinished ones, not loop over done ones."""
    env = _Env()
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    env.svc._tasks.add(done)  # finished, its callback not run yet
    await asyncio.wait_for(env.svc.wait_idle(), timeout=1.0)


async def test_no_page_no_position():
    env = _Env(own=ME, seed=None)
    assert await env.svc.position(env.spaces.space, [HOST]) is None

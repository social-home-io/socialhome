"""``SyncPageService`` — seal, carry, verify, store, root check, status
(``docs/protocol/sync-pages.md`` Phase 2)."""

from __future__ import annotations

import base64
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
from socialhome.domain.space_sync_page import StoredSyncPage
from socialhome.federation.sync.space.pages import SyncPageService
from socialhome.sync_page import (
    PAGE_MIN_INTERVAL_S,
    VECTOR_BYTES,
    apply_delta,
    delta_from_wire,
    parse_page,
    parse_page_ref,
    root_of,
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

        async def on_change(space_id):
            self.changes.append(space_id)

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
        )

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


async def test_a_delta_without_its_previous_page_drops_ours_for_a_full_vector():
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
    assert not ok and env.held() is None  # next BEGIN asks for the vector


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


# ── Root check and status ────────────────────────────────────────────────


async def test_position_applied_when_we_applied_the_signers_stream():
    env = _Env(own=ME, seed=None)
    page = _signed(seq=50)
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(page),
    )
    await env.svc.wait_idle()
    env.seqs.applied[HOST] = 49
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.applied is False  # a newer page exists
    assert pos.seq == 50 and pos.signer_role == "owner"
    env.seqs.applied[HOST] = 50
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.applied is True
    pos = await env.svc.position(env.spaces.space, ["someone-else"])
    assert pos is not None and pos.applied is None


async def test_root_check_counts_differing_buckets_once_an_hour():
    env = _Env(own=ME, seed=None)
    env.state.vec = _vec(2)
    page = _signed(vector=_vec(2))
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(page),
    )
    await env.svc.wait_idle()  # the background check ran
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.match is True and pos.differing_buckets == 0
    assert env.state.cutoffs == [None]  # the page's cutoff
    calls = env.state.calls
    assert await env.svc.check_root(SPACE) is None  # matched: never again
    # A new page whose vector differs from ours in 2 buckets.
    other = bytearray(_vec(2))
    other[0:64] = b"\x08" * 64
    newer = _signed(vector=bytes(other), seq=80, issued_at=T0 + 50)
    await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(newer, vector=bytes(other)),
    )
    await env.svc.wait_idle()
    assert env.state.calls == calls  # within the hour: no recompute
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.match is None
    env.mono += 3600
    assert await env.svc.check_root(SPACE) == 2
    pos = await env.svc.position(env.spaces.space, [HOST])
    assert pos is not None and pos.match is False and pos.differing_buckets == 2


async def test_the_signer_never_checks_its_own_page():
    env = _Env()
    await env.svc.seal_if_due(SPACE)
    calls = env.state.calls
    assert await env.svc.check_root(SPACE) is None
    assert env.state.calls == calls


async def test_no_page_no_position():
    env = _Env(own=ME, seed=None)
    assert await env.svc.position(env.spaces.space, [HOST]) is None

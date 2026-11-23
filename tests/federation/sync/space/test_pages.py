"""``SyncPageService`` — seal, carry, verify, store, root check, status
(``docs/protocol/sync-pages.md`` Phase 2)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from dataclasses import replace
from datetime import UTC, datetime

import orjson
import pytest

from socialhome.canonical_json import canonical_str
from socialhome.crypto import ed25519_public_key
from socialhome.domain.federation import FederationEvent, FederationEventType
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
from socialhome.federation.sync.space.pages import (
    PAGE_PUSH_AAD,
    PAGE_PUSH_INTERVAL_S,
    SyncPageService,
)
from socialhome.sync_page import (
    MAX_FETCH_BYTES,
    MAX_FETCH_ROWS,
    PAGE_MIN_INTERVAL_S,
    PAGE_SEAL_SUITE_AESGCM_256,
    VECTOR_BYTES,
    ListingRow,
    bucket_hash,
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


def _order(row: StoredSyncPage) -> tuple[int, int, int]:
    body = orjson.loads(row.page_json)
    return (body.get("epoch", 0), body.get("issued_at", 0), body.get("seq", 0))


class _RowsView:
    """``rows[space]`` — the newest page across signers (what a test reads
    as "the page held"); assigning one stores it as its signer's row."""

    def __init__(self, repo: _Repo) -> None:
        self._repo = repo

    def get(self, space_id):
        rows = self._repo.all(space_id)
        return rows[0] if rows else None

    def __getitem__(self, space_id):
        got = self.get(space_id)
        if got is None:
            raise KeyError(space_id)
        return got

    def __setitem__(self, space_id, page):
        signer = orjson.loads(page.page_json).get("signer", "")
        self._repo.pages[(space_id, signer)] = page

    def __contains__(self, space_id):
        return self.get(space_id) is not None

    def clear(self):
        self._repo.pages.clear()


class _Repo:
    """In-memory ``space_sync_pages``: one row per (space, signer)."""

    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], StoredSyncPage] = {}
        self.rows = _RowsView(self)

    def all(self, space_id):
        rows = [p for (sp, _s), p in self.pages.items() if sp == space_id]
        return sorted(rows, key=_order, reverse=True)

    async def get(self, space_id):
        rows = self.all(space_id)
        return rows[0] if rows else None

    async def list_all(self, space_id):
        return self.all(space_id)

    async def put(self, page, *, key):
        self.rows[page.space_id] = page
        return True


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
        self.probes = 0

    async def list_records(self, space_id):
        return []

    async def iter_changed(self, space_id, since):
        self.probes += 1
        if self.changed_above is not None and self.changed_above > since:
            yield [{"id": "p"}]


class _AdminSeats:
    """The roster mirror's live ``admin`` households (the only households
    the authority seed is shared with)."""

    def __init__(self) -> None:
        self.admins: list[str] = []

    async def list_instances_with_roles(self, space_id, roles):
        return list(self.admins) if "admin" in roles else []


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
        self.seats = _AdminSeats()
        self.svc._seats = self.seats  # the roster mirror (attach_member_fetch)

    def held(self):
        row = self.repo.rows.get(SPACE)
        return None if row is None else parse_page(orjson.loads(row.page_json))


V = FederationCapability.MIN_FOR_SYNC_PAGES


async def _sentinel(env: _Env, *, ref=None, version=V):
    """What a BEGIN naming *ref* gets: the ``SPACE_SYNC_PAGE`` event's
    ``page_sealed`` (sent to that household alone), or ``None``."""
    event = await env.svc.page_for(
        space_id=SPACE,
        requester_version=version,
        requester_ref=parse_page_ref(ref) if ref is not None else None,
    )
    await env.svc.wait_idle()
    return None if event is None else event["page_sealed"]


def _open(env: _Env, sealed, sync_id=PAGE_PUSH_AAD) -> dict:
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
    """*delta*: a body as a pre-final-review signer sent it to the holder of
    its previous page (changed buckets only, no vector) — refused now."""
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
    return {"epoch": epoch, "payload": ct, "seal_suite": PAGE_SEAL_SUITE_AESGCM_256}


# ── Seal (signer) ────────────────────────────────────────────────────────


async def test_the_owner_seals_its_first_page():
    env = _Env()
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    assert (page.signer, page.epoch, page.seq, page.rows) == (HOST, 3, 100, 10)
    assert page.root == root_of(env.state.vec) and page.prev_root is None
    row = env.repo.rows[SPACE]
    assert row.vector == env.state.vec
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


async def test_a_changed_space_seals_after_15_minutes():
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
    assert env.repo.rows[SPACE].vector == env.state.vec  # no delta is kept


async def test_the_quiet_probe_runs_once_per_change_of_the_counter():
    """Final review minor 9: every BEGIN to the signer asks whether a seal
    is due; the "changed since the page?" probe reads the exporters. Its
    answer is kept against the change counter: while ``current_seq`` does
    not move, no BEGIN probes again — and once a change is found for a page,
    it stays found until the next page."""
    env = _Env()
    first = await env.svc.seal_if_due(SPACE)
    assert first is not None
    env.now += (PAGE_MIN_INTERVAL_S + 1) * 1000
    for _ in range(3):
        assert await env.svc.seal_if_due(SPACE) is None  # quiet
    assert env.exporter.probes == 1
    env.seqs.seq = 150  # something changed somewhere
    env.exporter.changed_above = 150
    env.now = first.issued_at + 60_000  # young: no seal yet
    for _ in range(3):
        assert await env.svc.seal_if_due(SPACE) is None
    assert env.exporter.probes == 2
    env.now = first.issued_at + PAGE_MIN_INTERVAL_S * 1000
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and env.exporter.probes == 2


async def test_a_new_cutoff_day_reseals_a_young_page():
    env = _Env()
    env.state.window_cutoff = "2026-10-01 23:59:00"
    await env.svc.seal_if_due(SPACE)
    env.exporter.changed_above = 150
    env.now += 60_000
    env.state.window_cutoff = "2026-10-02 00:01:00"
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.cutoff == "2026-10-02 00:01:00"


async def test_an_admin_holding_the_owners_page_seals_its_own_first_page():
    """Every signer seals by its own stored page only (security re-check):
    an admin holding the owner's fresh page seals its own first page."""
    env = _admin_env()
    other = _signed(signer=HOST, issued_at=T0, vector=_vec(4))
    env.repo.rows[SPACE] = StoredSyncPage(
        SPACE, canonical_str(other.to_wire()), _vec(4)
    )
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.signer == ADMIN and page.prev_root is None


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
    assert set(sealed) == {"epoch", "payload", "seal_suite"}
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


async def test_the_previous_page_gets_the_full_vector():
    """Final review I4: no delta. A requester that holds the signer's
    previous page (or any other) gets the page and the whole vector; one
    that holds the current page gets nothing (its BEGIN ref says so)."""
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
    assert set(body) == {"page", "vector"}
    assert vector_from_wire(body["vector"]) == env.state.vec
    assert await _sentinel(env, ref=second.ref()) is None


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


async def test_a_page_without_its_vector_is_refused_even_after_its_previous():
    """Final review I4: there is no delta any more — a body carrying the
    changed buckets of the page we hold instead of the vector is refused,
    and ours is kept."""
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
    assert not ok and env.held() == first
    assert env.repo.rows[SPACE].vector == _vec(2)


async def test_a_page_without_a_vector_cannot_roll_a_member_back():
    """Review finding: a relay sending a newer page with no usable vector
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


def _admin_env() -> _Env:
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
    return env


async def test_a_delegated_admin_keeps_its_own_page_beside_the_owners():
    """Security re-check: one stored page per signer. A delegated admin
    keeps its own page (and its chain) AND stores the owner's newer page in
    the owner's row — and, as a signer, judges it (M-3): a returning admin
    never seals over content of the owner's page it has not caught up."""
    env = _admin_env()
    own = await env.svc.seal_if_due(SPACE)
    assert own is not None and own.signer == ADMIN
    other = _signed(signer=HOST, issued_at=T0 + 5_000, seq=999, vector=_vec(7))
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(other, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    held = await env.svc._held_all(env.spaces.space)
    assert {h.page.signer: h.page for h in held} == {ADMIN: own, HOST: other}
    check = env.svc._checks.get(SPACE)
    assert check is not None and check.root == other.root  # judged
    pos = await env.svc.position(env.spaces.space)
    assert pos is not None and pos.catching_up  # its status: catching up
    env.exporter.changed_above = 10**9
    env.now += PAGE_MIN_INTERVAL_S * 1000
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.prev_root == own.root  # its own chain


async def test_the_owner_takes_a_newer_page_its_delegated_admin_signed():
    """Phase 3: back after a long absence, the owner stores the newer page
    its delegated admin sealed meanwhile (same epoch) and compares its copy
    with it; its own next page starts a fresh chain (no conflict)."""
    env = _Env()
    _delegating(env)
    own = await env.svc.seal_if_due(SPACE)
    assert own is not None
    other = _signed(signer=ADMIN, issued_at=T0 + 5_000, seq=999, vector=_vec(7))
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(other, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    assert env.held() == other
    assert env.state.calls == 2  # the seal, then the compare
    # An older page of the admin (its previous one) is still refused.
    older = _signed(signer=ADMIN, issued_at=T0 + 1_000, seq=900, vector=_vec(8))
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(older, vector=_vec(8)),
    )
    # 15 min later the space changed: the owner seals on its own chain again.
    env.now = T0 + 5_000 + PAGE_MIN_INTERVAL_S * 1000
    env.exporter.changed_above = 10**9
    page = await env.svc.seal_if_due(SPACE)
    # Its own chain goes on: one stored page per signer (security re-check).
    assert page is not None and page.signer == HOST and page.prev_root == own.root


async def test_a_page_that_does_not_open_is_a_one_line_refusal(caplog):
    env = _Env(own=ME, seed=None)
    with caplog.at_level(logging.WARNING):
        assert not await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed={
                "epoch": 99,
                "payload": "garbage",
                "seal_suite": PAGE_SEAL_SUITE_AESGCM_256,
            },
        )
    await env.svc.wait_idle()
    assert "does not open" in caplog.text and "Traceback" not in caplog.text


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
    pos = await env.svc.position(env.spaces.space)
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
NEW = "2027-01-15T08:01:00.000000+00:00"  # after the page, within the skew


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
        if signer != HOST:
            _delegating(self.env)
        self.host_rows = list(host_rows)
        self.page_vector = _vector_of(self.host_rows)
        self.env.state.vec = _vector_of(member_rows)
        self.member_rows = list(member_rows)
        self.gone = set(gone)
        self.forge = None  # a function(records) -> records
        self.signer = signer
        self.rounds = 0
        #: sync_id → the ``page`` its BEGIN carried when it asked listings (what the
        #: requester's pending-request record holds).
        self.asked: dict[str, dict] = {}
        self.env.svc.attach_asks(self.asked.get)

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
        held = self.env.held()
        ref = held.ref() if held is not None else None
        for host in [*self.env.hosts, HOST, ADMIN]:
            buckets = self.env.svc.buckets_for(SPACE, host, ref)
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
        self.asked[sync_id] = self.env.held().ref()
        await self.env.svc.take_listing(
            space_id=SPACE, sync_id=sync_id, provider=host, records=records
        )
        await self.env.svc.listing_done(space_id=SPACE, sync_id=sync_id, provider=host)

    async def position(self):
        return await self.env.svc.position(self.env.spaces.space)


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
    assert state_with_page(SpaceSyncState.HOST_OFFLINE, pos, now=datetime.now(UTC)) is (
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


async def test_no_listing_export_for_a_page_the_same_round_replaced():
    """Final review minor 9: the round that carries our listings ends with a
    sentinel whose newer page replaces the one they are for. Its listings
    are dropped before our rows are read — no export of the space for
    nothing (the newer page's own compare decides)."""
    present = [_row(f"p{k}") for k in range(10)]
    lost = _row("lost")
    t = _Listed(present + [lost], present)
    await t.take_page()
    ((host, buckets),) = t.asks
    t.asked["R1"] = t.env.held().ref()
    await t.env.svc.take_listing(
        space_id=SPACE,
        sync_id="R1",
        provider=host,
        records=[listing_to_wire(i, []) for i in buckets],
    )
    newer = _signed(vector=_vec(9), seq=60, issued_at=T0 + 1)
    t.env.gate = asyncio.Event()  # its compare waits out the 15-min spacing
    assert await t.env.svc.receive(  # the same round's sentinel
        space_id=SPACE,
        sync_id="R1",
        provider=host,
        sealed=await _sealed_for_member(newer, vector=_vec(9), sync_id="R1"),
    )
    reads: list[list[int]] = []
    real = t.env.state.rows_in

    async def counting(space_id, wanted, *, cutoff=None):
        reads.append(list(wanted))
        return await real(space_id, wanted, cutoff=cutoff)

    t.env.state.rows_in = counting
    await t.env.svc.listing_done(space_id=SPACE, sync_id="R1", provider=host)
    assert reads == []
    t.env.gate.set()
    await t.env.svc.wait_idle()


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
    assert (
        state_with_page(SpaceSyncState.UP_TO_DATE, pos, now=datetime.now(UTC))
        is SpaceSyncState.BEHIND
    )
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


FAR = "2031-01-01T00:00:00.000000+00:00"  # far past T0 + the 300 s skew


async def test_a_far_future_change_time_explains_nothing():
    """Review B3: a member that stamps rows far in the future must not make
    a differing bucket "ahead" (explained) — nor win against the host's row
    in a listing — or one writer could make everyone read up to date."""
    host = [_row("p1", version=1, marker=OLD)]
    t = _Listed(host, [_row("p1", version=2, marker=FAR)])
    await t.take_page()
    assert t.asks == [(HOST, [_bucket(host[0])])]  # not explained
    await t.deliver()
    pos = await t.position()
    assert pos is not None and (pos.match, pos.missing) == (False, 1)
    # Within the skew a newer change time still explains the bucket.
    soon = "2027-01-15T08:05:00.000000+00:00"  # T0 is 08:00:00 UTC
    t = _Listed(host, [_row("p1", version=2, marker=soon)])
    await t.take_page()
    assert t.asks == []
    assert (await t.position()).match is True


async def test_a_listing_of_an_older_ask_is_never_checked_against_a_newer_page():
    """Review A3: a new page's compare replaces the ask while the round that
    carried the older ask is still streaming — that round's listings were
    made for the older page and are dropped, not refused against the new
    one (the new ask stays pending for its own round)."""
    lost = _row("lost")
    t = _Listed([lost], [])
    await t.take_page()
    t.asked["R1"] = t.env.held().ref()
    ((host, buckets),) = t.asks
    old_records = [
        listing_to_wire(i, [r for r in t.host_rows if _bucket(r) == i]) for i in buckets
    ]
    # A newer page (another row lost too) arrives and is compared.
    lost2 = _row("lost-2")
    t.host_rows.append(lost2)
    t.page_vector = _vector_of(t.host_rows)
    page = _signed(vector=t.page_vector, seq=60, issued_at=T0 + 1)
    assert await _take(t.env, page, t.page_vector)
    await t.env.svc.take_listing(
        space_id=SPACE, sync_id="R1", provider=host, records=old_records
    )
    await t.env.svc.listing_done(space_id=SPACE, sync_id="R1", provider=host)
    pos = await t.position()
    assert pos is not None and pos.seq == 60 and pos.match is None
    assert t.asks  # still asked for the new page
    await t.deliver()
    assert (await t.position()).missing == 2


async def test_no_buckets_on_a_begin_naming_a_newer_page_than_the_ask():
    """Re-review: a newer page is held while its compare waits out the
    spacing; the old ask's buckets on a BEGIN that names the new page would
    make the host export for listings we drop. Only the ask's own page
    carries them."""
    t = _Listed([_row("lost")], [])
    await t.take_page()
    old_ref = t.env.held().ref()
    assert t.env.svc.buckets_for(SPACE, HOST, old_ref)
    newer = _signed(vector=_vec(7), seq=60, issued_at=T0 + 1)
    assert t.env.svc.buckets_for(SPACE, HOST, newer.ref()) is None
    assert t.env.svc.buckets_for(SPACE, HOST, None) is None


async def test_a_judged_page_is_not_compared_again_after_the_wait():
    """Review A3 (b): a compare requested while the page's compare ran
    waits out the spacing and then finds the page judged — no second
    full-space export of the same page."""
    rows = [_row("p1")]
    t = _Listed(rows, rows)
    await t.take_page()
    assert t.env.state.calls == 1
    t.env.svc.compare_soon(SPACE)  # e.g. a status read racing the compare
    await t.env.svc.wait_idle()
    assert t.env.state.calls == 1


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
    pos = await env.svc.position(env.spaces.space)
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
    assert await env.svc.position(env.spaces.space) is None


# ── Phase 3: a deliverer's answer is bounded ─────────────────────────────


async def test_a_deliverers_answer_is_bounded_in_rows_bytes_and_record_size():
    """Rows past MAX_FETCH_ROWS or MAX_FETCH_BYTES are left for the next
    ask; a record over the requester's frame cap is never sent; an older
    requester gets nothing."""
    env = _Env()
    rows = [_row(rid) for rid in _ids_in_distinct_buckets(3, "p")]
    env.state.vec = _vector_of(rows)
    big = "x" * 4000

    async def rows_in(space_id, buckets, *, cutoff=None, keep_records=False, skip=()):
        listed = {i: [r for r in rows if _bucket(r) == i] for i in buckets}
        records = {
            i: [("posts", {"id": r.row_id, "content": big}) for _ in range(400)]
            for i in buckets
            for r in listed[i]
        }
        return BucketRows(rows=listed, gone=set(), records=records)

    env.state.rows_in = rows_in
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None
    buckets = [_bucket(r) for r in rows]

    async def fetch(**over):
        args = {
            "space_id": SPACE,
            "page_ref": parse_page_ref(page.ref()),
            "buckets": buckets,
            "requester_version": V,
            "have": frozenset(),
            "max_record_bytes": None,
        }
        return await env.svc.fetch_records(**{**args, **over})

    listings, fetched = await fetch()
    assert [r["held"] for r in listings] == [True, True, True]
    assert len(fetched) <= MAX_FETCH_ROWS
    assert sum(len(orjson.dumps(r)) for r in fetched) <= MAX_FETCH_BYTES
    assert len(fetched) == MAX_FETCH_BYTES // len(orjson.dumps(fetched[0]))
    _listings, fetched = await fetch(max_record_bytes=1000)
    assert fetched == []  # every record over the requester's frame cap
    assert await fetch(requester_version=V - 1) == ([], [])


# ── Push (the signer sends each new page to every member household) ──────


class _PushSender:
    """The federation service's space fan-out and one-target send."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, object, dict, int | None, object]] = []
        self.sends: list[tuple[str, str, object, dict]] = []

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        *,
        min_proto_version=None,
        only_instances=None,
    ):
        self.broadcasts.append(
            (space_id, event_type, payload, min_proto_version, only_instances)
        )

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id
    ):
        self.sends.append((space_id, to_instance_id, event_type, payload))


WRITERS = {"member-a", "member-b"}


def _pushing(env: _Env) -> _PushSender:
    sender = _PushSender()

    async def writers(space_id):
        return set(WRITERS)

    env.svc.attach_push(sender=sender, writers=writers)
    return sender


def _pushed_page(env: _Env, payload: dict):
    blob = base64.b64decode(payload["page_sealed"]["payload"])
    head = f"{SPACE}|{PAGE_PUSH_AAD}|".encode()
    assert blob.startswith(head)
    body = orjson.loads(blob[len(head) :])
    return parse_page(body["page"]), vector_from_wire(body["vector"])


def _change(env: _Env, minutes: float, seq: int) -> None:
    env.exporter.changed_above = 10**9
    env.seqs.seq = seq
    env.now += int(minutes * 60_000)
    env.mono += minutes * 60
    vec = bytearray(env.state.vec)
    vec[0:32] = bytes([seq % 256]) * 32
    env.state.vec = bytes(vec)


async def test_a_seal_pushes_the_page_to_every_member_household():
    """The signer pushes a new page at once to every member household —
    never a follower (the writer seats), only at v_95 — as the one
    ``SPACE_SYNC_PAGE`` event, the page and its vector sealed under the
    space content key."""
    env = _Env()
    sender = _pushing(env)
    page = await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    [(space_id, event_type, payload, min_v, only)] = sender.broadcasts
    assert space_id == SPACE
    assert event_type is FederationEventType.SPACE_SYNC_PAGE
    assert min_v == FederationCapability.MIN_FOR_SYNC_PAGES
    assert set(only) == WRITERS
    assert set(payload) == {"page_sealed"}
    got, vector = _pushed_page(env, payload)
    assert got == page and vector == env.state.vec


async def test_pushes_coalesce_to_one_an_hour_with_the_latest_page():
    """A seal within the hour after the last push schedules ONE push of the
    newest page at the hour mark (the scheduler's tick), never a queue."""
    env = _Env()
    sender = _pushing(env)
    await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    assert len(sender.broadcasts) == 1
    for k, minutes in enumerate((16, 16, 16)):  # three more seals in the hour
        _change(env, minutes, 200 + k)
        assert await env.svc.seal_if_due(SPACE) is not None
        await env.svc.wait_idle()
        await env.svc.push_due()
    assert len(sender.broadcasts) == 1  # none before the hour mark
    env.mono += PAGE_PUSH_INTERVAL_S
    await env.svc.push_due()
    await env.svc.push_due()
    assert len(sender.broadcasts) == 2
    latest, _vector = _pushed_page(env, sender.broadcasts[-1][2])
    assert latest == env.held()


async def test_a_begin_naming_a_page_older_than_the_pushed_one_gets_it():
    """One receive path, two triggers: a member that missed a push is caught
    by its BEGIN ``page`` ref — older than the page last pushed — with the
    same event sent to it alone. A ref of the last pushed page gets nothing
    (a newer page waits for its hourly push); a follower or an older
    household gets nothing."""
    env = _Env()
    sender = _pushing(env)
    first = await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    older = _signed(issued_at=T0 - 60_000, seq=1, vector=_vec(8))
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(older.ref()),
    )
    await env.svc.offer_page(
        space_id=SPACE, requester="member-b", requester_version=V, requester_ref=None
    )
    assert [(t, e) for _s, t, e, _p in sender.sends] == [
        ("member-a", FederationEventType.SPACE_SYNC_PAGE),
        ("member-b", FederationEventType.SPACE_SYNC_PAGE),
    ]
    got, _v = _pushed_page(env, sender.sends[0][3])
    assert got == first
    _change(env, 16, 300)
    assert await env.svc.seal_if_due(SPACE) is not None  # pending, not pushed
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(first.ref()),
    )
    await env.svc.offer_page(
        space_id=SPACE,
        requester="follower-f",
        requester_version=V,
        requester_ref=None,
    )
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-b",
        requester_version=V - 1,
        requester_ref=None,
    )
    assert len(sender.sends) == 2


async def test_a_member_relays_the_page_it_holds_to_a_stale_ref():
    """A member household (no seal of its own) catches a co-member's stale
    ref up — with the page it holds, the same event — only in a verified
    fetch session (the requester's hosts are silent; security check 2,
    M1)."""
    env = _Env(own=ME, seed=None)
    sender = _pushing(env)
    page = _signed()
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id=PAGE_PUSH_AAD,
        provider=HOST,
        sealed=await _sealed_for_member(page, sync_id=PAGE_PUSH_AAD),
    )
    await env.svc.wait_idle()
    assert sender.broadcasts == []  # a member never pushes to everyone
    older = _signed(issued_at=T0 - 60_000, seq=1, vector=_vec(8))
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(older.ref()),
    )
    assert sender.sends == []  # an ordinary BEGIN: only a signer answers
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(older.ref()),
        fetch=True,
    )
    [(_s, to, _e, payload)] = sender.sends
    assert to == "member-a" and _pushed_page(env, payload)[0] == page


async def test_the_page_event_handler_takes_a_pushed_page():
    """The dispatch registry's ``SPACE_SYNC_PAGE`` handler: the one receive
    path — verified against the pinned key, stored, compared."""
    env = _Env(own=ME, seed=None)
    env.spaces.local = ["u-me"]
    env.spaces.members = [
        SpaceMember(
            space_id=SPACE,
            user_id="u-me",
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    ]
    page = _signed()
    event = FederationEvent(
        msg_id="m-1",
        event_type=FederationEventType.SPACE_SYNC_PAGE,
        from_instance=HOST,
        to_instance=ME,
        timestamp="2027-01-15T08:00:00+00:00",
        payload={"page_sealed": await _sealed_for_member(page, sync_id=PAGE_PUSH_AAD)},
        space_id=SPACE,
    )
    await env.svc.on_page_event(event)
    await env.svc.wait_idle()
    assert env.held() == page
    # A payload without a page is ignored.
    await env.svc.on_page_event(replace(event, payload={}))
    assert env.held() == page


# ── Security check 2: two signers kept (owner decision) ──────────────────


def _admin_page(*, issued_at, seq=500, vector=None):
    return _signed(
        signer=ADMIN,
        issued_at=issued_at,
        seq=seq,
        vector=vector if vector is not None else _vec(7),
    )


def _delegating(env: _Env) -> None:
    env.spaces.space = _space(
        features=replace(SpaceFeatures(), delegated_admin_authority=True)
    )
    env.seats.admins = [ADMIN]


async def test_m1_a_stale_ref_gets_the_last_pushed_page_from_the_signer_only():
    """M1: a member's stale ref is answered by the signer alone — never by
    every member provider of a tick's BEGINs — and with the page LAST
    PUSHED, never a newer one waiting for its hour mark. One send per
    stale requester and round."""
    env = _Env()
    sender = _pushing(env)
    first = await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    _change(env, 16, 300)
    assert await env.svc.seal_if_due(SPACE) is not None  # pending, not pushed
    older = _signed(issued_at=T0 - 60_000, seq=1, vector=_vec(8))
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(older.ref()),
    )
    [(_s, to, _e, payload)] = sender.sends
    assert to == "member-a" and _pushed_page(env, payload)[0] == first
    # N member households holding a newer page than the requester: none of
    # them answers an ordinary BEGIN's stale ref.
    for k in range(5):
        member = _Env(own=f"member-{k}", seed=None)
        msender = _pushing(member)
        assert await member.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed=await _sealed_for_member(_signed(), sync_id="s-1"),
        )
        await member.svc.offer_page(
            space_id=SPACE,
            requester="member-a",
            requester_version=V,
            requester_ref=parse_page_ref(older.ref()),
        )
        assert msender.sends == []


async def test_a_stale_ref_is_judged_per_signer():
    """Security re-check: a BEGIN's ``page`` names, per signer, the newest
    page the requester holds (``signers``). Each signer judges only ITS
    entry: an admin whose own page is newest still names our page, so it is
    not resent; an owner that sealed after its admin names none of the
    admin's (or an older one) and gets it (H-1, H-2)."""
    env = _Env()
    sender = _pushing(env)
    page = await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    admins_own = _signed(signer="member-a", issued_at=T0 + 60_000, seq=900)
    holds_ours = {
        **admins_own.ref(),
        "signers": [
            ["member-a", admins_own.epoch, admins_own.issued_at, admins_own.seq],
            [HOST, page.epoch, page.issued_at, page.seq],
        ],
    }
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(holds_ours),
    )
    assert sender.sends == []
    lacks_ours = {
        **admins_own.ref(),
        "signers": [
            ["member-a", admins_own.epoch, admins_own.issued_at, admins_own.seq]
        ],
    }
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(lacks_ours),
    )
    [(_s, to, _e, payload)] = sender.sends
    assert to == "member-a" and _pushed_page(env, payload)[0] == page


async def test_l5_a_follower_household_drops_a_pushed_page():
    """L5: the receiving half of "no page to a follower": a household
    holding no member seat drops a page it was sent by mistake."""
    env = _Env(own=ME, seed=None)
    env.spaces.local = ["u-me"]
    env.spaces.members = [
        SpaceMember(
            space_id=SPACE,
            user_id="u-me",
            role=SpaceRole.SUBSCRIBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    ]
    page = _signed()
    stored = await env.svc.take_pushed(
        space_id=SPACE,
        provider=HOST,
        payload={"page_sealed": await _sealed_for_member(page, sync_id=PAGE_PUSH_AAD)},
    )
    assert not stored and env.held() is None


async def test_l1_a_nay_counts_only_from_a_household_we_can_ask_now():
    """L1: a bucket is dry when every member household we can ask NOW said
    ``held: false`` — the nay of one removed since (or no longer at v_95)
    no longer counts."""
    env = _Env(own=ME, seed=None)
    root = b"\x01" * 32
    env.svc._eligible[SPACE] = frozenset({"m-a", "m-b"})
    env.svc._note_nays(SPACE, root, "m-a", {5})
    env.svc._eligible[SPACE] = frozenset({"m-b", "m-c"})  # m-a left
    env.svc._note_nays(SPACE, root, "m-b", {5})
    assert env.svc._paused(SPACE, root) == set()  # m-c has not answered
    env.svc._note_nays(SPACE, root, "m-c", {5})
    assert env.svc._paused(SPACE, root) == {5}


@pytest.mark.parametrize("suite", [None, "aes128-something"])
async def test_l3_a_page_without_a_known_seal_suite_is_refused(suite, caplog):
    """L3: ``page_sealed`` carries its ``seal_suite``; a receiver refuses a
    missing or unknown one — never a default — and keeps its page."""
    env = _Env(own=ME, seed=None)
    sealed = await _sealed_for_member(_signed())
    if suite is None:
        sealed.pop("seal_suite")
    else:
        sealed["seal_suite"] = suite
    with caplog.at_level(logging.WARNING):
        assert not await env.svc.receive(
            space_id=SPACE, sync_id="s-1", provider=HOST, sealed=sealed
        )
    assert env.held() is None and "refused the page" in caplog.text


# ── Security re-check: judge every foreign page once; no seal holds ─────


async def test_the_owner_judges_an_admin_page_older_than_its_own():
    """The owner sealed its own page meanwhile (back, or partitioned); its
    delegated admin's page arrives OLDER. It is stored in the admin's row
    (one row per signer) and judged; the owner's status position reads it
    until nothing it commits is missing. Delivered again: not compared
    again."""
    env = _Env()
    _delegating(env)
    own = await env.svc.seal_if_due(SPACE)
    assert own is not None
    other = _admin_page(issued_at=own.issued_at - 60_000)
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(other, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    assert env.held() == own  # newest across signers: unchanged
    check = env.svc._checks.get(SPACE)
    assert check is not None and check.root == other.root  # judged
    pos = await env.svc.position(env.spaces.space)
    assert pos is not None and pos.signer_role == "admin" and pos.catching_up
    calls = env.state.calls
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(other, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    assert env.state.calls == calls


async def test_an_old_relayed_page_of_a_signer_is_ignored():
    """M-1: a 90-day-old page of a signer, relayed by anyone, is older than
    the page we hold FROM THAT SIGNER: neither stored nor judged — it never
    parks a catch-up."""
    env = _Env()
    _delegating(env)
    await env.svc.seal_if_due(SPACE)
    current = _admin_page(issued_at=T0 - 3_600_000, seq=900)
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(current, vector=_vec(7)),
    )
    await env.svc.wait_idle()
    root = env.svc._checks[SPACE].root
    old = _admin_page(issued_at=T0 - 90 * 86_400_000, seq=3, vector=_vec(9))
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider="relay-x",
        sealed=await _sealed_for_member(old, vector=_vec(9)),
    )
    await env.svc.wait_idle()
    assert env.svc._checks[SPACE].root == root == current.root


async def test_a_member_never_judges_an_older_page():
    """A plain member compares only with the newest page overall (it would
    roll its newer copy back to an older page's versions): an older page of
    the same signer is ignored."""
    env = _Env(own=ME, seed=None)
    newer = _signed(issued_at=T0 + 10, seq=60, vector=_vec(2))
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(newer, vector=_vec(2)),
    )
    await env.svc.wait_idle()
    older = _signed(issued_at=T0, seq=50, vector=_vec(3))
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(older, vector=_vec(3)),
    )
    await env.svc.wait_idle()
    assert env.svc._checks[SPACE].root == newer.root


@pytest.mark.parametrize("minutes", [60, 14])
async def test_an_admins_page_cadence_never_stops_the_owners_seals(minutes):
    """M-1 / M-2: hourly admin pages (an unprovable bucket each time), or a
    seed-holding admin sealing every 14 min, never stop the owner's seals —
    it seals by its own lazy interval (≥ 15 min apart) whatever it holds."""
    env = _Env()
    _delegating(env)
    assert await env.svc.seal_if_due(SPACE) is not None
    seals = 0
    for k in range(8):
        _change(env, minutes, 1000 + k)
        page = _admin_page(issued_at=env.now - 1_000, seq=2000 + k, vector=_vec(k + 20))
        await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=ADMIN,
            sealed=await _sealed_for_member(page, vector=_vec(k + 20)),
        )
        await env.svc.wait_idle()
        if await env.svc.seal_if_due(SPACE) is not None:
            seals += 1
    # Never stopped: every change once the own 15-min interval allows it.
    assert seals >= (8 if minutes >= 15 else 4)


async def test_a_restarting_owner_still_seals():
    """Restart loops: no timer or hold survives in memory to block it — a
    restarted owner holding its admin's page (no verdict yet) seals by its
    lazy rule."""
    env = _Env()
    _delegating(env)
    other = _admin_page(issued_at=T0 - 3 * 3600_000)
    env.repo.rows[SPACE] = StoredSyncPage(
        SPACE, canonical_str(other.to_wire()), _vec(7)
    )
    for _ in range(3):
        restarted = SyncPageService(
            repo=env.repo,
            spaces=env.spaces,
            seqs=env.seqs,
            crypto=env.crypto,
            state=env.state,
            exporters={"posts": env.exporter},
            own_instance_id=HOST,
            clock_ms=lambda: env.now,
            monotonic=lambda: env.mono,
        )
        env.exporter.changed_above = 10**9
        env.now += PAGE_MIN_INTERVAL_S * 1000
        assert await restarted.seal_if_due(SPACE) is not None
        await restarted.close()


@pytest.mark.parametrize("bad", [["x"], {"a": 1}, 7])
async def test_an_unhashable_seal_suite_is_a_clean_refusal(bad, caplog):
    """L-1: a ``seal_suite`` of the wrong type is refused like an unknown
    one (one WARNING), never a TypeError."""
    env = _Env(own=ME, seed=None)
    sealed = await _sealed_for_member(_signed())
    sealed["seal_suite"] = bad
    with caplog.at_level(logging.WARNING):
        assert not await env.svc.receive(
            space_id=SPACE, sync_id="s-1", provider=HOST, sealed=sealed
        )
    assert "refused the page" in caplog.text and "Traceback" not in caplog.text


async def test_a_pushed_page_of_an_older_epoch_is_never_sent_again():
    """L-2: after a key or epoch change the payload pushed under the old
    epoch is dropped — a stale ref never gets it."""
    env = _Env()
    sender = _pushing(env)
    await env.svc.seal_if_due(SPACE)
    await env.svc.wait_idle()
    env.spaces.space = _space(authority_key_epoch=4)  # rotated; old page gone
    env.repo.rows.clear()
    _change(env, 1, 5000)
    newer = await env.svc.seal_if_due(SPACE)  # pending: within the hour
    assert newer is not None and newer.epoch == 4
    older = _signed(issued_at=T0 - 60_000, seq=1, epoch=4, vector=_vec(8))
    await env.svc.offer_page(
        space_id=SPACE,
        requester="member-a",
        requester_version=V,
        requester_ref=parse_page_ref(older.ref()),
    )
    for _s, _to, _e, payload in sender.sends:
        got, _v = _pushed_page(env, payload)
        assert got.epoch == 4
    assert SPACE not in env.svc._pushed_payload or (env.svc._pushed[SPACE][1][0] == 4)


# ── Security re-check 3: who may sign, our own id, member resends ───────


async def test_ghost_signers_are_refused():
    """M-1: a seed holder mints pages under 300 made-up signer ids. Only the
    owner, or — delegation on — a household holding a live admin seat in
    OUR roster, may sign: none is stored, the rows stay the real signers'.
    A real admin's page is still taken."""
    env = _Env(own=ME, seed=None)
    _delegating(env)
    for i in range(300):
        ghost = _signed(signer=f"ghost-{i}", seq=i + 1, vector=_vec(9))
        assert not await env.svc.receive(
            space_id=SPACE,
            sync_id="s-1",
            provider=HOST,
            sealed=await _sealed_for_member(ghost, vector=_vec(9)),
        )
    await env.svc.wait_idle()
    assert await env.svc._held_all(env.spaces.space) == []
    real = _admin_page(issued_at=T0)
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(real, vector=_vec(7)),
    )


async def test_an_admin_page_needs_delegation():
    """M-1: without delegated admin authority nobody but the owner signs."""
    env = _Env(own=ME, seed=None)
    env.seats.admins = [ADMIN]
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(_admin_page(issued_at=T0), vector=_vec(7)),
    )


async def test_a_page_under_our_own_id_from_the_wire_is_ignored():
    """M-2: our own row is written only by our own seal. A page from the
    wire signed under OUR id (a seed holder forging it) is never stored —
    a plain member does not turn into a "signer" by it."""
    env = _Env(own=ME, seed=None)
    _delegating(env)
    env.seats.admins = [ADMIN, ME]  # even with an admin seat of ours
    forged = _signed(signer=ME, issued_at=T0, seq=7, vector=_vec(5))
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(forged, vector=_vec(5)),
    )
    assert await env.svc._held_all(env.spaces.space) == []
    assert not await env.svc._signs(env.spaces.space)


async def test_a_row_of_our_id_without_the_seed_does_not_make_us_a_signer():
    """M-2: "signer" means holding the seed (or owning the space), never
    "a row with my id is stored" — a stale own row of a demoted admin
    neither judges nor answers stale refs."""
    env = _Env(own=ME, seed=None)
    _delegating(env)
    mine = _signed(signer=ME, issued_at=T0, seq=7, vector=_vec(5))
    env.repo.rows[SPACE] = StoredSyncPage(SPACE, canonical_str(mine.to_wire()), _vec(5))
    assert not await env.svc._signs(env.spaces.space)
    assert await env.svc._own(env.spaces.space) is None


async def test_a_forged_owner_page_cannot_stop_the_owners_seals():
    """M-2: a page under the owner's id with a huge ``seq`` and
    ``issued_at`` a minute ahead, from the wire, is ignored by the owner —
    its next seal goes out as due."""
    env = _Env()
    own = await env.svc.seal_if_due(SPACE)
    assert own is not None
    forged = _signed(
        signer=HOST, issued_at=env.now + 60_000, seq=10**12, vector=_vec(6)
    )
    assert not await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=ADMIN,
        sealed=await _sealed_for_member(forged, vector=_vec(6)),
    )
    env.exporter.changed_above = 10**9
    env.now += PAGE_MIN_INTERVAL_S * 1000
    page = await env.svc.seal_if_due(SPACE)
    assert page is not None and page.prev_root == own.root


async def test_a_member_never_resends_a_page_the_requester_holds():
    """L-1: a plain member relaying in a fetch session reads the
    requester's ``signers`` entry for the held page's signer — a requester
    whose main ref is an older catch-up page of another signer but which
    holds our page already gets nothing; one lacking it gets the page."""
    env = _Env(own=ME, seed=None)
    _delegating(env)
    newest = _signed(issued_at=T0 + 10, seq=60, vector=_vec(2))
    assert await env.svc.receive(
        space_id=SPACE,
        sync_id="s-1",
        provider=HOST,
        sealed=await _sealed_for_member(newest, vector=_vec(2)),
    )
    catch_up = _admin_page(issued_at=T0)
    holds = {
        **catch_up.ref(),
        "signers": [
            [HOST, newest.epoch, newest.issued_at, newest.seq],
            [ADMIN, catch_up.epoch, catch_up.issued_at, catch_up.seq],
        ],
    }
    assert (
        await env.svc.page_for(
            space_id=SPACE, requester_version=V, requester_ref=parse_page_ref(holds)
        )
        is None
    )
    lacks = {
        **catch_up.ref(),
        "signers": [[ADMIN, catch_up.epoch, catch_up.issued_at, catch_up.seq]],
    }
    assert (
        await env.svc.page_for(
            space_id=SPACE, requester_version=V, requester_ref=parse_page_ref(lacks)
        )
        is not None
    )

"""SpaceMemberDirectory: member cards + the host's directory (v_89), unit layer.

In-memory fakes only (no network, no disk). The end-to-end behaviour on the
real wire is in ``tests/federation/test_invite_bootstrap_gfs_e2e.py`` (v_89).
"""

from __future__ import annotations

from types import SimpleNamespace

import orjson

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.gfs_mailbox import OwnMailbox
from socialhome.federation.gfs_mailbox import derive_mailbox_keys, new_mailbox_root
from socialhome.federation.member_card import build_card, verify_card
from socialhome.services import space_member_directory as smd
from socialhome.services.gfs_envelope_sender import normalize_gfs_base
from socialhome.services.space_member_directory import (
    HELLO_RESNAPSHOT_S,
    SpaceMemberDirectory,
)

GFS = "https://gfs.example.org"
OTHER_GFS = "https://other-gfs.example.org"
SPACE = "space-1"
SPACE_2 = "space-2"


# ── Households + cards ───────────────────────────────────────────────────


def _hh() -> dict:
    ident = generate_identity_keypair()
    kw = generate_x25519_keypair()
    return {
        "seed": ident.private_key,
        "pk": ident.public_key,
        "id": derive_instance_id(ident.public_key),
        "kw_pub": kw.public_key,
        "kw_sig": b64url_encode(sign_ed25519(ident.private_key, kw.public_key)),
    }


def _card(h: dict, *, rev: int = 1, gfs: str | None = GFS, proto: int = 89):
    mailbox = None
    if gfs is not None:
        mailbox = (derive_mailbox_keys(new_mailbox_root(), "g" * 32, 1), gfs)
    return build_card(
        instance_id=h["id"],
        identity_seed=h["seed"],
        identity_pk=h["pk"],
        keywrap_pk=h["kw_pub"],
        keywrap_sig=h["kw_sig"],
        proto_version=proto,
        rev=rev,
        mailbox=mailbox,
    )


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeSpaceRepo:
    def __init__(self) -> None:
        self.owner: dict[str, str] = {}
        self.local: dict[str, list[str]] = {}
        self.rows: dict[str, dict[str, str | None]] = {}

    async def get(self, space_id):
        owner = self.owner.get(space_id)
        return SimpleNamespace(owner_instance_id=owner) if owner else None

    async def list_local_member_user_ids(self, space_id):
        return list(self.local.get(space_id, []))

    async def list_member_instances(self, space_id):
        return list(self.rows.get(space_id, {}))

    async def add_space_instance(self, space_id, instance_id):
        self.rows.setdefault(space_id, {}).setdefault(instance_id, None)

    async def set_space_instance_mailbox(self, space_id, instance_id, value):
        # UPDATE-only: never creates membership.
        if instance_id in self.rows.get(space_id, {}):
            self.rows[space_id][instance_id] = value

    async def get_space_instance_mailbox(self, space_id, instance_id):
        return self.rows.get(space_id, {}).get(instance_id)

    async def list_space_instance_mailboxes(self):
        return [
            (s, i, v)
            for s, insts in self.rows.items()
            for i, v in insts.items()
            if v is not None
        ]

    # test helpers
    def doc(self, space_id, instance_id) -> dict:
        raw = self.rows.get(space_id, {}).get(instance_id)
        return orjson.loads(raw) if raw else {}

    def put_card(self, space_id, card) -> None:
        self.rows.setdefault(space_id, {})[card.instance_id] = orjson.dumps(
            {"card": card.to_wire()}
        ).decode()


class FakeFederationRepo:
    def __init__(self) -> None:
        self.instances: dict[str, SimpleNamespace] = {}
        self.proto_set: list[tuple[str, int]] = []

    async def get_instance(self, instance_id):
        return self.instances.get(instance_id)

    async def set_proto_version(self, instance_id, version):
        self.proto_set.append((instance_id, version))


class FakeWriters:
    def __init__(self) -> None:
        self.writers: dict[str, set[str]] = {}

    async def writer_households(self, space_id):
        return frozenset(self.writers.get(space_id, set()))


class FakeMailboxes:
    def __init__(self) -> None:
        self.rev = 0
        self._boxes: dict[str, tuple] = {}

    def add_box(self, url: str) -> None:
        keys = derive_mailbox_keys(new_mailbox_root(), "g" * 32, 1)
        own = OwnMailbox(gen=1, gfs_instance_id="g" * 32, gfs_url=url)
        self._boxes[normalize_gfs_base(url)] = (keys, own)

    def mailbox_at(self, url):
        return self._boxes.get(normalize_gfs_base(url)) if url else None

    def mailbox_gfs_urls(self):
        return set(self._boxes)

    def attach_needed(self, fn) -> None:
        pass

    def attach_after_sweep(self, fn) -> None:
        pass

    def attach_on_change(self, fn) -> None:
        self.on_change = fn


class FakeRegistry:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, event_type, handler) -> None:
        self.handlers[event_type] = handler


class FakeFederation:
    def __init__(self) -> None:
        self._event_registry = FakeRegistry()
        self.directories: list[dict] = []
        self.cards: list[dict] = []
        self.unsupported: set[str] = set()

    async def broadcast_to_space_members(
        self, space_id, event_type, payload, *, min_proto_version, only_instances
    ):
        (to,) = only_instances
        self.directories.append(
            {
                "space": space_id,
                "type": event_type,
                "to": to,
                "payload": payload,
                "min": min_proto_version,
            }
        )
        return SimpleNamespace(attempted=1, terminal_failures=())

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id
    ):
        self.cards.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space": space_id,
            }
        )
        return SimpleNamespace(ok=True)

    async def space_member_supports(self, instance_id, *, min_version):
        return instance_id not in self.unsupported

    def to(self, instance_id) -> list[dict]:
        return [d for d in self.directories if d["to"] == instance_id]


def _ids(directory: dict) -> set[str]:
    return {c["instance_id"] for c in directory["payload"]["cards"]}


def _event(event_type, sender, to, payload, space_id=SPACE) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=sender,
        to_instance=to,
        timestamp="2026-11-09T00:00:00+00:00",
        payload=payload,
        space_id=space_id,
    )


class World:
    """One household (``me``) running the directory, with fakes around it."""

    def __init__(self, *, host: dict | None = None) -> None:
        self.me = _hh()
        self.host = host or self.me
        self.spaces = FakeSpaceRepo()
        self.fed_repo = FakeFederationRepo()
        self.writers = FakeWriters()
        self.mailboxes = FakeMailboxes()
        self.fed = FakeFederation()
        self.seated: list[str] = []
        self.dir = SpaceMemberDirectory(
            space_repo=self.spaces,
            federation_repo=self.fed_repo,
            writer_households=self.writers,
            mailboxes=self.mailboxes,
            instance_id=self.me["id"],
            identity_seed=self.me["seed"],
            identity_pk=self.me["pk"],
            keywrap_pk=self.me["kw_pub"],
            keywrap_sig=self.me["kw_sig"],
        )
        self.dir.attach_to(self.fed)

        async def seater(card):
            self.seated.append(card.instance_id)

        self.dir.attach_seater(seater)
        self.spaces.owner[SPACE] = self.host["id"]
        self.spaces.local[SPACE] = ["u-local"]
        self.spaces.rows[SPACE] = {self.me["id"]: None, self.host["id"]: None}

    def member(self, *households: dict, space_id: str = SPACE) -> None:
        for h in households:
            self.spaces.rows.setdefault(space_id, {}).setdefault(h["id"], None)

    async def card_from(self, h: dict, card) -> None:
        await self.dir._on_member_card(
            _event(
                FederationEventType.SPACE_MEMBER_CARD,
                h["id"],
                self.me["id"],
                {"card": card.to_wire()},
            )
        )

    async def directory(self, payload: dict, *, sender: str | None = None) -> None:
        await self.dir._on_directory(
            _event(
                FederationEventType.SPACE_MAILBOX_DIRECTORY,
                sender or self.host["id"],
                self.me["id"],
                payload,
            )
        )


# ── Host ─────────────────────────────────────────────────────────────────


async def test_attach_registers_both_events():
    w = World()
    assert set(w.fed._event_registry.handlers) == {
        FederationEventType.SPACE_MEMBER_CARD,
        FederationEventType.SPACE_MAILBOX_DIRECTORY,
    }


async def test_host_stores_a_card_and_sends_snapshots_then_deltas():
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.writers.writers[SPACE] = {a["id"], b["id"]}

    card_a = _card(a, gfs=GFS)
    await w.card_from(a, card_a)
    assert w.spaces.doc(SPACE, a["id"])["card"] == card_a.to_wire()
    # A (has a mailbox) gets the "all" snapshot: our own (mailbox-less) card.
    (snap_a,) = w.fed.to(a["id"])
    assert snap_a["payload"]["snapshot"] is True
    assert snap_a["type"] is FederationEventType.SPACE_MAILBOX_DIRECTORY
    assert snap_a["min"] == 89
    assert _ids(snap_a) == {w.me["id"]}
    # B (no card, no mailbox) gets only the cards carrying a mailbox.
    (snap_b,) = w.fed.to(b["id"])
    assert snap_b["payload"]["snapshot"] is True
    assert _ids(snap_b) == {a["id"]}
    assert w.spaces.doc(SPACE, a["id"])["snap"] == "all"
    assert w.spaces.doc(SPACE, b["id"])["snap"] == "mbx"

    # B's (mailbox-less) card: A, an "all" recipient, gets it as a delta.
    w.fed.directories.clear()
    card_b = _card(b, gfs=None)
    await w.card_from(b, card_b)
    (delta_a,) = w.fed.to(a["id"])
    assert "snapshot" not in delta_a["payload"]
    assert delta_a["payload"]["cards"] == [card_b.to_wire()]
    assert delta_a["payload"]["removed"] == []
    # B's card is its "I am seated": B is sent a fresh snapshot.
    (again_b,) = w.fed.to(b["id"])
    assert again_b["payload"]["snapshot"] is True
    assert _ids(again_b) == {a["id"]}


async def test_a_mailbox_less_card_is_not_a_delta_for_a_mailbox_only_recipient():
    w = World()
    a, b, c = _hh(), _hh(), _hh()
    w.member(a, b, c)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.writers.writers[SPACE] = {a["id"], b["id"], c["id"]}
    await w.dir.reconcile(SPACE)
    assert w.spaces.doc(SPACE, b["id"])["snap"] == "mbx"
    w.fed.directories.clear()

    await w.card_from(c, _card(c, gfs=None))
    assert w.fed.to(b["id"]) == []  # mbx recipient: no use for C's card
    (delta_a,) = w.fed.to(a["id"])
    assert _ids(delta_a) == {c["id"]}


async def test_a_follower_household_never_receives_a_directory():
    w = World()
    a, follower = _hh(), _hh()
    w.member(a, follower)
    w.writers.writers[SPACE] = {a["id"]}  # the follower holds no writer seat
    await w.card_from(follower, _card(follower, gfs=GFS))
    await w.card_from(a, _card(a, gfs=GFS))
    await w.dir.reconcile(SPACE)
    assert w.fed.directories, "the writer got its directory"
    assert all(d["to"] != follower["id"] for d in w.fed.directories)
    # ... and the follower's card is not passed on to the writers either.
    assert all(follower["id"] not in _ids(d) for d in w.fed.directories)


async def test_a_recipient_below_v89_is_skipped():
    w = World()
    a, old = _hh(), _hh()
    w.member(a, old)
    w.writers.writers[SPACE] = {a["id"], old["id"]}
    w.fed.unsupported.add(old["id"])
    await w.card_from(a, _card(a, gfs=GFS))
    assert w.fed.to(old["id"]) == []
    assert w.fed.to(a["id"])


async def test_host_refuses_a_card_for_another_household():
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    await w.card_from(a, _card(b, gfs=GFS))  # A hands over B's card
    assert w.spaces.doc(SPACE, b["id"]) == {}
    assert w.spaces.doc(SPACE, a["id"]) == {}
    assert w.fed.directories == []


async def test_host_ignores_a_card_from_a_non_member_household():
    w = World()
    stranger = _hh()
    w.writers.writers[SPACE] = {stranger["id"]}
    await w.card_from(stranger, _card(stranger, gfs=GFS))
    assert stranger["id"] not in w.spaces.rows[SPACE]
    assert w.fed.directories == []


async def test_a_member_does_not_act_as_host_for_a_card():
    host = _hh()
    w = World(host=host)
    a = _hh()
    w.member(a)
    w.writers.writers[SPACE] = {a["id"]}
    await w.card_from(a, _card(a, gfs=GFS))
    assert w.spaces.doc(SPACE, a["id"]) == {}
    assert w.fed.directories == []


async def test_reconcile_sends_nothing_while_no_card_has_a_mailbox():
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    card_a = _card(a, gfs=None)
    await w.card_from(a, card_a)
    await w.dir.reconcile(SPACE)
    assert w.spaces.doc(SPACE, a["id"])["card"] == card_a.to_wire()
    assert w.fed.directories == []


async def test_a_card_with_a_lower_rev_does_not_replace_a_newer_one():
    w = World()
    a = _hh()
    w.member(a)
    newer = _card(a, rev=5, gfs=GFS)
    await w.card_from(a, newer)
    await w.card_from(a, _card(a, rev=4, gfs=OTHER_GFS))
    assert w.spaces.doc(SPACE, a["id"])["card"] == newer.to_wire()


async def test_snapshot_is_bounded_and_carries_mailbox_cards_first(monkeypatch):
    monkeypatch.setattr(smd, "SNAPSHOT_MAX", 2)
    w = World()
    r, m1, m2, n1, n2 = (_hh() for _ in range(5))
    w.member(r, m1, m2, n1, n2)
    for h, gfs in ((r, GFS), (m1, GFS), (m2, GFS), (n1, None), (n2, None)):
        w.spaces.put_card(SPACE, _card(h, gfs=gfs))
    w.writers.writers[SPACE] = {h["id"] for h in (r, m1, m2, n1, n2)}
    await w.dir.reconcile(SPACE)
    (snap,) = w.fed.to(r["id"])
    assert snap["payload"]["snapshot"] is True
    assert _ids(snap) == {m1["id"], m2["id"]}


async def test_a_household_that_stops_being_a_writer_is_removed_once():
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.spaces.put_card(SPACE, _card(b, gfs=GFS))
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    await w.dir.reconcile(SPACE)
    assert b["id"] in _ids(w.fed.to(a["id"])[0])
    w.fed.directories.clear()

    w.writers.writers[SPACE] = {a["id"]}
    await w.dir.reconcile(SPACE)
    (delta,) = w.fed.to(a["id"])
    assert delta["payload"]["removed"] == [b["id"]]
    assert delta["payload"]["cards"] == []
    assert w.fed.to(b["id"]) == []
    w.fed.directories.clear()

    await w.dir.reconcile(SPACE)
    assert w.fed.directories == []


async def test_our_own_card_change_goes_out_as_a_delta():
    w = World()
    w.mailboxes.add_box(GFS)
    w.mailboxes.rev = 5
    a = _hh()
    w.member(a)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    # A is a GFS-only member we seated by link: our card names our mailbox
    # at ITS relay (v_89: only a seat's relay counts, never another card's).
    w.fed_repo.instances[a["id"]] = SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via=GFS,
        proto_version=89,
    )
    w.writers.writers[SPACE] = {a["id"]}
    await w.dir.reconcile(SPACE)
    (snap,) = w.fed.to(a["id"])
    (own,) = snap["payload"]["cards"]
    assert own["instance_id"] == w.me["id"] and own["rev"] == 5
    assert own["mailbox"]["gfs"] == GFS
    w.fed.directories.clear()

    await w.dir.reconcile(SPACE)  # nothing moved
    assert w.fed.directories == []

    w.mailboxes.rev = 6
    await w.dir.reconcile(SPACE)
    (delta,) = w.fed.to(a["id"])
    assert "snapshot" not in delta["payload"]
    (own,) = delta["payload"]["cards"]
    assert own["rev"] == 6
    assert verify_card(own, expected_instance_id=w.me["id"]).rev == 6


# ── Member ───────────────────────────────────────────────────────────────


def _member_world() -> tuple[World, dict]:
    host = _hh()
    w = World(host=host)
    return w, host


async def test_member_ignores_a_directory_not_from_the_host():
    w, _host = _member_world()
    a = _hh()
    await w.directory({"cards": [_card(a).to_wire()], "snapshot": True}, sender=a["id"])
    assert a["id"] not in w.spaces.rows[SPACE]
    assert w.seated == [] and w.fed.cards == []


async def test_member_ignores_a_directory_for_a_space_it_does_not_use():
    w, _host = _member_world()
    w.spaces.local[SPACE] = []
    a = _hh()
    await w.directory({"cards": [_card(a).to_wire()], "snapshot": True})
    assert a["id"] not in w.spaces.rows[SPACE]


async def test_member_verifies_stores_and_seats_directory_cards():
    w, host = _member_world()
    a, b, c, d = _hh(), _hh(), _hh(), _hh()
    tampered = _card(c, gfs=GFS).to_wire()
    tampered["rev"] = 99
    w.fed_repo.instances[b["id"]] = SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        relay_via=None,
        proto_version=89,
    )
    host_card = _card(host, gfs=GFS)
    await w.directory(
        {
            "cards": [
                _card(a, gfs=GFS).to_wire(),
                _card(b, gfs=GFS).to_wire(),
                tampered,
                _card(d, gfs=None).to_wire(),
                host_card.to_wire(),
            ],
            "snapshot": True,
        }
    )
    for h in (a, b, d):
        assert w.spaces.doc(SPACE, h["id"])["card"]["instance_id"] == h["id"]
    assert c["id"] not in w.spaces.rows[SPACE]
    assert w.spaces.doc(SPACE, host["id"])["card"] == host_card.to_wire()
    # A seated; B keeps its confirmed row; D has no mailbox and neither do
    # we (it is reached through the host); the host is never seated here.
    assert w.seated == [a["id"]]


async def test_a_member_with_a_mailbox_seats_mailbox_less_households_too():
    w, _host = _member_world()
    w.mailboxes.add_box(GFS)
    w.mailboxes.rev = 3
    d = _hh()
    await w.directory({"cards": [_card(d, gfs=None).to_wire()], "snapshot": True})
    assert w.seated == [d["id"]]


async def test_a_snapshot_clears_unlisted_cards_but_never_the_hosts():
    w, host = _member_world()
    gone, a = _hh(), _hh()
    host_card = _card(host, gfs=GFS)
    w.spaces.put_card(SPACE, host_card)
    w.spaces.put_card(SPACE, _card(gone, gfs=GFS))
    await w.directory({"cards": [_card(a, gfs=GFS).to_wire()], "snapshot": True})
    assert w.spaces.doc(SPACE, gone["id"]) == {}
    assert w.spaces.doc(SPACE, host["id"])["card"] == host_card.to_wire()
    assert w.spaces.doc(SPACE, a["id"])["card"]["instance_id"] == a["id"]


async def test_a_delta_keeps_unlisted_cards_and_removed_clears():
    w, host = _member_world()
    keep, gone = _hh(), _hh()
    host_card = _card(host, gfs=GFS)
    w.spaces.put_card(SPACE, host_card)
    w.spaces.put_card(SPACE, _card(keep, gfs=GFS))
    w.spaces.put_card(SPACE, _card(gone, gfs=GFS))
    await w.directory({"cards": [], "removed": [gone["id"], host["id"]]})
    assert w.spaces.doc(SPACE, gone["id"]) == {}
    assert w.spaces.doc(SPACE, keep["id"])["card"]["instance_id"] == keep["id"]
    assert w.spaces.doc(SPACE, host["id"])["card"] == host_card.to_wire()


async def test_oversized_directories_are_ignored(monkeypatch):
    w, _host = _member_world()
    monkeypatch.setattr(smd, "SNAPSHOT_MAX", 1)
    monkeypatch.setattr(smd, "REMOVED_MAX", 1)
    a, b, kept1, kept2 = _hh(), _hh(), _hh(), _hh()
    w.spaces.put_card(SPACE, _card(kept1, gfs=GFS))
    w.spaces.put_card(SPACE, _card(kept2, gfs=GFS))
    await w.directory(
        {"cards": [_card(a).to_wire(), _card(b).to_wire()], "snapshot": True}
    )
    await w.directory({"cards": [], "removed": [kept1["id"], kept2["id"]]})
    assert a["id"] not in w.spaces.rows[SPACE]
    assert b["id"] not in w.spaces.rows[SPACE]
    assert w.spaces.doc(SPACE, kept1["id"])["card"]["instance_id"] == kept1["id"]
    assert w.spaces.doc(SPACE, kept2["id"])["card"]["instance_id"] == kept2["id"]
    assert w.fed.cards == []


async def test_member_publishes_its_card_to_the_host_once_per_rev():
    w, host = _member_world()
    w.mailboxes.add_box(GFS)
    w.mailboxes.rev = 7
    # We joined by link: the host is a seat reached through the GFS.
    w.fed_repo.instances[host["id"]] = SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via=GFS,
        proto_version=89,
    )
    w.spaces.put_card(SPACE, _card(host, gfs=GFS))
    a = _hh()
    payload = {
        "cards": [_card(a, gfs=GFS).to_wire(), _card(host, gfs=GFS).to_wire()],
        "snapshot": True,
    }
    await w.directory(payload)
    (sent,) = w.fed.cards
    assert sent["to"] == host["id"]
    assert sent["type"] is FederationEventType.SPACE_MEMBER_CARD
    assert sent["space"] == SPACE
    card = verify_card(sent["payload"]["card"], expected_instance_id=w.me["id"])
    assert card.rev == 7 and card.mailbox is not None
    assert w.spaces.doc(SPACE, host["id"])["sent"] == 7

    await w.directory(payload)
    assert len(w.fed.cards) == 1  # the host has this rev

    w.mailboxes.rev = 8
    await w.directory(payload)
    assert len(w.fed.cards) == 2
    assert w.fed.cards[-1]["payload"]["card"]["rev"] == 8


async def test_a_new_card_proto_version_updates_a_direct_seat():
    w, _host = _member_world()
    a = _hh()
    w.fed_repo.instances[a["id"]] = SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via=None,
        proto_version=89,
    )
    w.spaces.put_card(SPACE, _card(a, rev=1, proto=89))
    await w.directory({"cards": [_card(a, rev=2, proto=90).to_wire()]})
    assert w.fed_repo.proto_set == [(a["id"], 90)]


async def test_a_mailbox_less_member_publishes_its_card_only_once():
    w, _host = _member_world()
    assert w.mailboxes.rev == 0
    a = _hh()
    payload = {"cards": [_card(a, gfs=GFS).to_wire()], "snapshot": True}
    await w.directory(payload)
    await w.directory(payload)
    assert len(w.fed.cards) == 1


# ── Transport checks ─────────────────────────────────────────────────────


async def test_sender_allowed_needs_the_space_the_roster_and_a_card():
    w, _host = _member_world()
    a, b, stranger = _hh(), _hh(), _hh()
    w.member(a, b)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    assert await w.dir.sender_allowed(a["id"], SPACE)
    # A member household without a card anywhere: refused.
    assert not await w.dir.sender_allowed(b["id"], SPACE)
    # A member's card from another space does NOT count: a removal (its
    # card for THIS space cleared) must take effect at once.
    w.spaces.rows[SPACE_2] = {}
    w.spaces.put_card(SPACE_2, _card(b, gfs=GFS))
    assert not await w.dir.sender_allowed(b["id"], SPACE)
    # …the host's does (a second space joined over the existing seat).
    w.spaces.put_card(SPACE_2, _card(_host, gfs=GFS))
    assert await w.dir.sender_allowed(_host["id"], SPACE)
    # …and on the HOST, a member household's card from another space does
    # (its first card for this space arrives over that very gate).
    hw = World()
    m = _hh()
    hw.member(m)
    hw.spaces.rows[SPACE_2] = {}
    hw.spaces.put_card(SPACE_2, _card(m, gfs=GFS))
    assert await hw.dir.sender_allowed(m["id"], SPACE)
    assert not await hw.dir.sender_allowed(_hh()["id"], SPACE)
    # A card held but not a member household of this space: refused.
    w.spaces.put_card(SPACE_2, _card(stranger, gfs=GFS))
    assert not await w.dir.sender_allowed(stranger["id"], SPACE)
    # We are not in the space (no local member): refused.
    w.spaces.local[SPACE] = []
    assert not await w.dir.sender_allowed(a["id"], SPACE)


async def test_target_for_falls_back_to_another_spaces_mailbox_card():
    w, _host = _member_world()
    a, nobody = _hh(), _hh()
    w.member(a)
    w.spaces.put_card(SPACE, _card(a, gfs=None))
    assert await w.dir.target_for(a["id"], SPACE) is None
    with_box = _card(a, gfs=GFS)
    w.spaces.rows[SPACE_2] = {}
    w.spaces.put_card(SPACE_2, with_box)
    for space in (SPACE, None):
        target = await w.dir.target_for(a["id"], space)
        assert target is not None
        assert target.mailbox_id == with_box.mailbox.mailbox_id
        assert target.kem_pk == with_box.mailbox.kem_pk
        assert target.gfs_url == GFS
    assert await w.dir.target_for(nobody["id"], SPACE) is None


async def test_verify_seat_card_pins_the_leg_gfs_and_the_household():
    a, b = _hh(), _hh()
    card = _card(a, gfs=GFS)
    wire = card.to_wire()
    verify = SpaceMemberDirectory.verify_seat_card
    assert verify(wire, instance_id=a["id"], gfs_url=GFS + "/") == card
    assert verify(wire, instance_id=a["id"], gfs_url="https://GFS.example.org") == card
    assert verify(wire, instance_id=a["id"], gfs_url=OTHER_GFS) is None
    assert verify(wire, instance_id=b["id"], gfs_url=GFS) is None
    assert verify(None, instance_id=a["id"], gfs_url=GFS) is None
    no_box = _card(a, gfs=None).to_wire()
    assert verify(no_box, instance_id=a["id"], gfs_url=OTHER_GFS) is not None


async def test_a_mesh_routed_directory_and_card_name_their_space_inside():
    """A mesh-routed envelope carries no plaintext routing ``space_id``: a
    directory / card that came over the mesh names its space inside the
    encrypted, origin-authenticated payload — and a payload copy that
    disagrees with the routing field is refused."""
    w, host = _member_world()
    a = _hh()
    payload = {"space_id": SPACE, "cards": [_card(a).to_wire()], "snapshot": True}
    await w.dir._on_directory(
        _event(
            FederationEventType.SPACE_MAILBOX_DIRECTORY,
            host["id"],
            w.me["id"],
            payload,
            space_id=None,
        )
    )
    assert w.spaces.doc(SPACE, a["id"])["card"]["instance_id"] == a["id"]
    b = _hh()
    await w.dir._on_directory(
        _event(
            FederationEventType.SPACE_MAILBOX_DIRECTORY,
            host["id"],
            w.me["id"],
            {"space_id": "another-space", "cards": [_card(b).to_wire()]},
        )
    )
    assert b["id"] not in w.spaces.rows[SPACE]
    # The host side takes a mesh-routed card the same way.
    hw = World()
    m = _hh()
    hw.member(m)
    await hw.dir._on_member_card(
        _event(
            FederationEventType.SPACE_MEMBER_CARD,
            m["id"],
            hw.me["id"],
            {"space_id": SPACE, "card": _card(m).to_wire()},
            space_id=None,
        )
    )
    assert hw.spaces.doc(SPACE, m["id"])["card"]["instance_id"] == m["id"]


# ── Review round 1 ───────────────────────────────────────────────────────


def _seat_row(relay_via: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via=relay_via,
        proto_version=89,
    )


async def test_only_a_seat_relay_keeps_a_mailbox_alive_never_another_card():
    """Two paired members whose cards both name a mailbox at G must not keep
    each other's mailbox alive once the GFS-only member is gone: only OUR
    sealed-redeem seats' relays count."""
    w, host = _member_world()
    a = _hh()
    w.member(a)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.fed_repo.instances[a["id"]] = _seat_row(None)  # a directory seat
    assert await w.dir.needed_gfs() == set()
    w.spaces.put_card(SPACE, _card(host, gfs=GFS))
    w.fed_repo.instances[host["id"]] = _seat_row(GFS)  # we joined by link
    assert await w.dir.needed_gfs() == {GFS}


async def test_a_card_queued_for_an_offline_host_counts_as_sent():
    w, host = _member_world()
    w.mailboxes.rev = 3

    async def queued(**kw):
        w.fed.cards.append(kw)
        return SimpleNamespace(ok=False, error=DELIVERY_ERROR_QUEUED)

    w.fed.send_with_mesh_fallback = queued
    a = _hh()
    payload = {"cards": [_card(a, gfs=GFS).to_wire()], "snapshot": True}
    await w.directory(payload)
    await w.directory(payload)
    await w.dir.publish_cards()
    assert len(w.fed.cards) == 1  # never one copy per directory / sweep
    assert w.spaces.doc(SPACE, host["id"])["sent"] == 3


async def test_a_newer_card_is_a_delta_and_a_repeated_one_resnapshots_rarely():
    now = [1000.0]
    w = World()
    w.dir._clock = lambda: now[0]
    a, b = _hh(), _hh()
    w.member(a, b)
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    await w.card_from(a, _card(a, gfs=GFS, rev=1))
    w.fed.directories.clear()
    # A rotation (newer rev): a delta to B, no new snapshot for A.
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert w.fed.to(a["id"]) == []
    assert [d["payload"]["cards"][0]["rev"] for d in w.fed.to(b["id"])] == [2]
    # The same card again ("seated", past the first card's window): one
    # snapshot, then none for a while.
    now[0] += HELLO_RESNAPSHOT_S
    w.fed.directories.clear()
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert [d["payload"].get("snapshot") for d in w.fed.to(a["id"])] == [True]
    w.fed.directories.clear()
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert w.fed.to(a["id"]) == []
    now[0] += HELLO_RESNAPSHOT_S
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert [d["payload"].get("snapshot") for d in w.fed.to(a["id"])] == [True]


async def test_a_big_snapshot_goes_in_parts_that_fit_the_relay(monkeypatch):
    monkeypatch.setattr(smd, "SNAPSHOT_PART_MAX", 2)
    w = World()
    others = [_hh() for _ in range(4)]
    newcomer = _hh()
    w.member(newcomer, *others)
    for h in others:
        w.spaces.put_card(SPACE, _card(h, gfs=GFS))
    w.writers.writers[SPACE] = {newcomer["id"], *(h["id"] for h in others)}
    w.fed.directories.clear()
    await w.card_from(newcomer, _card(newcomer, gfs=GFS))
    parts = w.fed.to(newcomer["id"])
    assert [len(p["payload"]["cards"]) for p in parts] == [2, 2, 1]
    # Only a one-part snapshot is authoritative (clears what it omits).
    assert {p["payload"].get("snapshot") for p in parts} == {False}
    assert w.spaces.doc(SPACE, newcomer["id"])["snap"] == "all"


async def test_a_card_that_lost_its_mailbox_reaches_mailbox_only_recipients():
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    await w.card_from(a, _card(a, gfs=GFS, rev=1))
    await w.card_from(b, _card(b, gfs=None, rev=1))  # B gets "mbx" only
    w.fed.directories.clear()
    await w.card_from(a, _card(a, gfs=None, rev=2))  # A's mailbox is gone
    (delta,) = w.fed.to(b["id"])
    assert delta["payload"]["cards"][0]["mailbox"] is None

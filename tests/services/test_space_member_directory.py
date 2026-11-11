"""SpaceMemberDirectory: member cards + the host's directory (v_89), unit layer.

In-memory fakes only (no network, no disk). The end-to-end behaviour on the
real wire is in ``tests/federation/test_invite_bootstrap_gfs_e2e.py`` (v_89).
"""

from __future__ import annotations

import asyncio
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
from socialhome.domain.space import SpaceRole
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
    async def list_all(self):
        return [
            SimpleNamespace(id=sid, owner_instance_id=owner)
            for sid, owner in self.owner.items()
        ]

    async def list_members(self, space_id):
        # Our local people hold a member's (writer) seat.
        return [
            SimpleNamespace(user_id=u, role=SpaceRole.MEMBER)
            for u in self.local.get(space_id, [])
        ]

    def __init__(self) -> None:
        self.owner: dict[str, str] = {}
        self.dissolved: set[str] = set()
        self.local: dict[str, list[str]] = {}
        self.rows: dict[str, dict[str, str | None]] = {}

    async def get(self, space_id):
        owner = self.owner.get(space_id)
        if not owner:
            return None
        return SimpleNamespace(
            owner_instance_id=owner,
            dissolved=space_id in self.dissolved,
            archived=space_id in self.dissolved,
            archived_reason="dissolved" if space_id in self.dissolved else None,
        )

    async def list_local_member_user_ids(self, space_id):
        return list(self.local.get(space_id, []))

    async def list_member_instances(self, space_id):
        return list(self.rows.get(space_id, {}))

    async def add_space_instance(self, space_id, instance_id):
        self.rows.setdefault(space_id, {}).setdefault(instance_id, None)

    async def remove_space_instance(self, space_id, instance_id):
        self.rows.get(space_id, {}).pop(instance_id, None)

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

    mints = 0

    def mint_soon(self) -> None:
        self.mints += 1

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

    def raw_to(self, instance_id) -> list[dict]:
        """Every directory frame sent to *instance_id*, as sent."""
        return [d for d in self.directories if d["to"] == instance_id]

    def to(self, instance_id) -> list[dict]:
        """The frames to *instance_id*, the parts of one snapshot (its
        ``members`` part and its card parts, one ``dir_seq``) merged."""
        out: list[dict] = []
        for d in self.raw_to(instance_id):
            prev = out[-1] if out else None
            if (
                prev is not None
                and d["payload"].get("snapshot")
                and prev["payload"].get("snapshot")
                and prev["payload"]["dir_seq"] == d["payload"]["dir_seq"]
            ):
                merged = dict(prev["payload"])
                merged["cards"] = merged["cards"] + d["payload"]["cards"]
                if "members" in d["payload"]:
                    merged["members"] = d["payload"]["members"]
                # One merged frame: a whole snapshot in one part.
                merged["part"], merged["parts"] = 0, 1
                if "gfs_reached" in d["payload"]:
                    merged["gfs_reached"] = (
                        merged.get("gfs_reached", []) + d["payload"]["gfs_reached"]
                    )
                out[-1] = {**prev, "payload": merged}
            else:
                out.append(d)
        return out


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
        self._seq = 0
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

    def seat_all(self, space_id: str = SPACE) -> None:
        """Every writer household already told us it is seated (its card or
        a sync) — directory frames go only to such a household."""
        for iid in self.writers.writers.get(space_id, set()):
            row = self.spaces.rows.setdefault(space_id, {})
            doc = orjson.loads(row[iid]) if row.get(iid) else {}
            doc["seated"] = True
            row[iid] = orjson.dumps(doc).decode()

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
        if "dir_seq" not in payload:
            self._seq += 1
            payload = {**payload, "dir_seq": self._seq}
        if not payload.get("snapshot") and "base_seq" not in payload:
            # In order: the delta builds on the version we hold.
            held = self.spaces.doc(SPACE, self.host["id"]).get("dir_seq") or 0
            payload = {**payload, "base_seq": held}
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
    w.seat_all()

    card_a = _card(a, gfs=GFS)
    await w.card_from(a, card_a)
    assert w.spaces.doc(SPACE, a["id"])["card"] == card_a.to_wire()
    # A's card is its "seated": A (has a mailbox) gets the snapshot of every
    # card (our own, mailbox-less) under the directory's version.
    (snap_a,) = w.fed.to(a["id"])
    assert snap_a["payload"]["snapshot"] is True
    assert snap_a["type"] is FederationEventType.SPACE_MAILBOX_DIRECTORY
    assert snap_a["min"] == 89
    assert _ids(snap_a) == {w.me["id"]}
    assert set(snap_a["payload"]["members"]) == {w.me["id"]}
    seq_1 = snap_a["payload"]["dir_seq"]
    # B (no card, no mailbox) gets the new mailbox card as a delta.
    (delta_b,) = w.fed.to(b["id"])
    assert "snapshot" not in delta_b["payload"]
    assert _ids(delta_b) == {a["id"]}

    # B's (mailbox-less) card: A gets it as a delta under a newer version;
    # B's card is its "seated": B gets a snapshot (mailbox cards only, all
    # members named).
    w.fed.directories.clear()
    card_b = _card(b, gfs=None)
    await w.card_from(b, card_b)
    (delta_a,) = w.fed.to(a["id"])
    assert delta_a["payload"]["cards"] == [card_b.to_wire()]
    assert delta_a["payload"]["dir_seq"] > seq_1
    (snap_b,) = w.fed.to(b["id"])
    assert snap_b["payload"]["snapshot"] is True
    assert _ids(snap_b) == {a["id"]}
    assert set(snap_b["payload"]["members"]) == {a["id"], w.me["id"]}


async def test_a_mailbox_less_card_is_not_a_delta_for_a_mailbox_only_recipient():
    w = World()
    a, b, c = _hh(), _hh(), _hh()
    w.member(a, b, c)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.writers.writers[SPACE] = {a["id"], b["id"], c["id"]}
    w.seat_all()
    await w.dir.reconcile(SPACE)
    w.fed.directories.clear()

    await w.card_from(c, _card(c, gfs=None))
    assert w.fed.to(b["id"]) == []  # mailbox-only recipient: no use for it
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
    w.seat_all()
    await w.dir.reconcile(SPACE, snapshot_to={r["id"]})
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
    w.seat_all()
    await w.dir.reconcile(SPACE, snapshot_to={a["id"]})
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
    w.seat_all()
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
    ended: list[str] = []

    async def ender(iid):
        ended.append(iid)

    w.dir.attach_seat_ender(ender)
    await w.directory(
        {
            "cards": [_card(a, gfs=GFS).to_wire()],
            "members": [a["id"], host["id"]],
            "snapshot": True,
        }
    )
    # Review round 1 (#1336): the household's ROW goes too (no fan-out of
    # the space reaches it any more) and its seat is ended if unused.
    assert gone["id"] not in w.spaces.rows[SPACE]
    assert ended == [gone["id"]]
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
    assert gone["id"] not in w.spaces.rows[SPACE]
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


async def test_target_for_uses_another_spaces_card_only_space_less():
    w, _host = _member_world()
    a, nobody = _hh(), _hh()
    w.member(a)
    w.spaces.put_card(SPACE, _card(a, gfs=None))
    assert await w.dir.target_for(a["id"], SPACE) is None
    with_box = _card(a, gfs=GFS)
    w.spaces.rows[SPACE_2] = {}
    w.spaces.put_card(SPACE_2, with_box)
    # Only a SPACE-LESS envelope may use another space's card: one of space
    # X never goes to a household on the strength of its card in Y.
    assert await w.dir.target_for(a["id"], SPACE) is None
    target = await w.dir.target_for(a["id"], None)
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
    payload = {
        "space_id": SPACE,
        "cards": [_card(a).to_wire()],
        "snapshot": True,
        "dir_seq": 1,
    }
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
            {"space_id": "another-space", "cards": [_card(b).to_wire()], "dir_seq": 2},
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
    w.seat_all()
    await w.card_from(a, _card(a, gfs=GFS, rev=1))
    w.fed.directories.clear()
    # A rotation (newer rev): a delta to B; A gets no snapshot, only its
    # cardless version bump.
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert [d["payload"]["cards"] for d in w.fed.to(a["id"])] == [[]]
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
    w.seat_all()
    w.fed.directories.clear()
    await w.card_from(newcomer, _card(newcomer, gfs=GFS))
    parts = [
        p
        for p in w.fed.raw_to(newcomer["id"])
        if p["payload"].get("snapshot") and p["payload"]["cards"]
    ]
    assert [len(p["payload"]["cards"]) for p in parts] == [2, 2, 1]
    # One version; the members (ids only) ride their own LAST part, with
    # the count of cards the snapshot carried.
    every = w.fed.raw_to(newcomer["id"])
    assert len({p["payload"]["dir_seq"] for p in every}) == 1
    assert len(every[-1]["payload"]["members"]) == 5
    assert [p["payload"]["part"] for p in every] == [0, 1, 2, 3]
    assert {p["payload"]["parts"] for p in every} == {4}
    assert all("members" not in p["payload"] for p in parts)


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


async def test_no_first_snapshot_before_the_household_says_it_is_seated():
    """Review round 1 (#1336) — the race the demo hit: a snapshot sent when
    a household's seat goes live can beat the seat at the receiver and be
    dropped. The host sends a household its first snapshot only once its
    card ("seated") arrived."""
    w = World()
    a, newcomer = _hh(), _hh()
    w.member(a, newcomer)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.writers.writers[SPACE] = {a["id"], newcomer["id"]}
    await w.dir.reconcile(SPACE)
    assert w.fed.to(newcomer["id"]) == []
    await w.card_from(newcomer, _card(newcomer, gfs=None))
    (snap,) = w.fed.to(newcomer["id"])
    assert snap["payload"]["snapshot"] is True


async def test_a_member_gives_its_card_once_to_every_host_it_holds_a_seat_with():
    """Every member hands its card to the host once per space and ``rev`` —
    with no GFS member in the space yet too (that is its "seated")."""
    w, host = _member_world()
    w.mailboxes.rev = 4
    assert await w.dir.publish_cards() == 1
    assert [c["to"] for c in w.fed.cards] == [host["id"]]
    assert await w.dir.publish_cards() == 0
    w.spaces.local[SPACE] = []  # no seat of ours any more
    w.mailboxes.rev = 5
    assert await w.dir.publish_cards() == 0


async def test_a_mesh_only_host_gets_our_card_on_its_acks_version():
    """Found by the demo: a host we reach only over the mesh has no row we
    could read its version from, so our "seated" card never went out and no
    snapshot ever came. Its ACK names its version; we keep it on its row and
    the sweep uses it later too."""
    w, host = _member_world()
    w.fed.unsupported.add(host["id"])  # no row: version unknown
    w.mailboxes.rev = 2
    await w.dir.seated(SPACE, host["id"], host_proto=89)
    assert [c["to"] for c in w.fed.cards] == [host["id"]]
    w.mailboxes.rev = 3
    assert await w.dir.publish_cards() == 1
    # A host below v_89 is never sent one.
    w2, host2 = _member_world()
    w2.fed.unsupported.add(host2["id"])
    await w2.dir.seated(SPACE, host2["id"], host_proto=88)
    assert w2.fed.cards == []


# ── Review round 2 (#1336): dir_seq, sync repair, removals ────────────────


def _begin(sender: dict, to: str, dir_seq: int) -> FederationEvent:
    return _event(
        FederationEventType.SPACE_SYNC_BEGIN,
        sender["id"],
        to,
        {"space_id": SPACE, "sync_id": "s", "dir_seq": dir_seq},
    )


async def test_the_host_answers_a_joiner_seated_by_link_at_once():
    """Finding 1: the joiner's "seated" card can reach the host before the
    host's seat row exists (the commit follows the ACK). It is kept aside
    and answered when the seat's card is stored at commit; without it, no
    snapshot goes at commit (one then could beat the joiner's own seat)."""
    w = World()
    j, a, k = _hh(), _hh(), _hh()
    w.member(a)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.writers.writers[SPACE] = {j["id"], a["id"], k["id"]}
    await w.card_from(j, _card(j, gfs=GFS))  # before the row: kept aside
    assert w.fed.to(j["id"]) == []
    w.member(j, k)
    await w.dir.store_seat_card(SPACE, _card(j, gfs=GFS))
    (snap,) = [d for d in w.fed.to(j["id"]) if d["payload"].get("snapshot")]
    assert _ids(snap) == {a["id"], w.me["id"]}
    await w.dir.store_seat_card(SPACE, _card(k, gfs=GFS))
    assert [d for d in w.fed.to(k["id"]) if d["payload"].get("snapshot")] == []


def _seated_log(w: World) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def hook(space_id: str, instance_id: str) -> None:
        calls.append((space_id, instance_id))

    w.dir.attach_on_seated(hook)
    return calls


async def test_a_seated_card_tells_the_seated_hook():
    """The demo's ``gfs-private-channel`` (#1336 regression): the host's
    channel-grant roster snapshot, sent when the joiner's seat went live,
    beat the joiner's own seat and was refused by its mailbox gate. The
    joiner's "seated" card is the moment its gate takes the host's frames:
    the host hears of it through the seated hook (the grant goes again)."""
    now = [1000.0]
    w = World()
    w.dir._clock = lambda: now[0]
    calls = _seated_log(w)
    a = _hh()
    w.member(a)
    w.writers.writers[SPACE] = {a["id"]}
    await w.card_from(a, _card(a, gfs=GFS, rev=1))
    assert calls == [(SPACE, a["id"])]
    # A rotation (a newer card) is not a "seated".
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert calls == [(SPACE, a["id"])]
    # The same card again: rarely, like the directory snapshot.
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert calls == [(SPACE, a["id"])]
    now[0] += HELLO_RESNAPSHOT_S
    await w.card_from(a, _card(a, gfs=GFS, rev=2))
    assert calls == [(SPACE, a["id"])] * 2


async def test_an_early_seated_card_tells_the_hook_once_its_seat_commits():
    """A "seated" card that beat the host's seat row is answered at commit
    (:meth:`store_seat_card`) — the hook with it; a seat card stored with
    no "seated" before it tells nobody (the joiner is not seated yet)."""
    w = World()
    calls = _seated_log(w)
    j, k = _hh(), _hh()
    w.writers.writers[SPACE] = {j["id"], k["id"]}
    await w.card_from(j, _card(j, gfs=GFS))  # before the row: kept aside
    assert calls == []
    w.member(j, k)
    await w.dir.store_seat_card(SPACE, _card(j, gfs=GFS))
    assert calls == [(SPACE, j["id"])]
    await w.dir.store_seat_card(SPACE, _card(k, gfs=GFS))
    assert calls == [(SPACE, j["id"])]


async def test_a_first_sync_from_a_household_tells_the_seated_hook():
    w = World()
    calls = _seated_log(w)
    a = _hh()
    w.member(a)
    w.writers.writers[SPACE] = {a["id"]}
    await w.dir.on_sync_begin(_begin(a, w.me["id"], 0))
    assert calls == [(SPACE, a["id"])]
    # Already seated: a later sync is no news.
    await w.dir.on_sync_begin(_begin(a, w.me["id"], 0))
    assert calls == [(SPACE, a["id"])]


async def test_a_failing_seated_hook_never_breaks_the_card():
    w = World()

    async def boom(space_id: str, instance_id: str) -> None:
        raise RuntimeError("down")

    w.dir.attach_on_seated(boom)
    a = _hh()
    w.member(a)
    w.writers.writers[SPACE] = {a["id"]}
    await w.card_from(a, _card(a, gfs=GFS))
    assert w.spaces.doc(SPACE, a["id"])["card"]["instance_id"] == a["id"]
    assert [d["payload"].get("snapshot") for d in w.fed.to(a["id"])] == [True]


async def test_a_member_behind_on_dir_seq_gets_a_snapshot_on_its_sync():
    """Findings 1 + 2: repair rides the periodic §25.6 sync — a BEGIN that
    echoes an older ``dir_seq`` (a lost delta, a snapshot that raced the
    seat, a fresh joiner at 0) gets one snapshot; an up-to-date one none."""
    w = World()
    a, b = _hh(), _hh()
    w.member(a, b)
    w.spaces.put_card(SPACE, _card(a, gfs=GFS))
    w.writers.writers[SPACE] = {a["id"], b["id"]}
    w.seat_all()
    await w.dir.reconcile(SPACE)
    seq = w.spaces.doc(SPACE, w.me["id"])["dir_seq"]
    w.fed.directories.clear()
    await w.dir.on_sync_begin(_begin(a, w.me["id"], seq))
    assert w.fed.directories == []
    await w.dir.on_sync_begin(_begin(a, w.me["id"], 0))
    (snap,) = w.fed.to(a["id"])
    assert snap["payload"]["snapshot"] is True
    assert snap["payload"]["dir_seq"] == seq


async def test_a_lost_removal_is_repaired_by_the_next_sync():
    """Finding 2: the ``removed`` delta never arrived; the member's next
    sync gets the whole directory and the gone household loses its card,
    row and seat there."""
    host = World()
    member, gone = _hh(), _hh()
    host.member(member, gone)
    host.spaces.put_card(SPACE, _card(member, gfs=GFS))
    host.spaces.put_card(SPACE, _card(gone, gfs=GFS))
    host.writers.writers[SPACE] = {member["id"], gone["id"]}
    await host.dir.reconcile(SPACE)
    host.writers.writers[SPACE] = {member["id"]}
    await host.dir.reconcile(SPACE)  # the removal delta — lost
    host.fed.directories.clear()
    await host.dir.on_sync_begin(_begin(member, host.me["id"], 1))
    (snap,) = host.fed.to(member["id"])
    assert gone["id"] not in snap["payload"]["members"]

    m = World(host=host.me)
    m.member(gone)
    m.spaces.put_card(SPACE, _card(gone, gfs=GFS))
    ended: list[str] = []

    async def ender(iid):
        ended.append(iid)

    m.dir.attach_seat_ender(ender)
    await m.directory(snap["payload"])
    assert gone["id"] not in m.spaces.rows[SPACE]
    assert ended == [gone["id"]]


async def test_a_late_old_delta_never_re_adds_a_removed_household():
    """Finding 3: deltas are ordered by ``dir_seq``."""
    w, _host = _member_world()
    x = _hh()
    w.member(x)
    await w.directory({"cards": [], "removed": [x["id"]], "dir_seq": 10})
    assert x["id"] not in w.spaces.rows[SPACE]
    await w.directory({"cards": [_card(x, gfs=GFS).to_wire()], "dir_seq": 5})
    assert x["id"] not in w.spaces.rows[SPACE]
    await w.directory({"cards": [_card(x, gfs=GFS).to_wire()], "dir_seq": 11})
    assert x["id"] in w.spaces.rows[SPACE]


async def test_a_dissolved_or_left_space_drops_its_directory():
    """Finding 4a: on a remote dissolve (archived as dissolved) and on our
    own leave, the space's cards, rows and seats go, and the card gate no
    longer passes its ex-members."""
    for leave in ("dissolved", "left"):
        w, host = _member_world()
        x = _hh()
        w.member(x)
        w.spaces.put_card(SPACE, _card(x, gfs=GFS))
        w.spaces.put_card(SPACE, _card(host, gfs=GFS))
        ended: list[str] = []

        async def ender(iid, ended=ended):
            ended.append(iid)

        w.dir.attach_seat_ender(ender)
        assert await w.dir.sender_allowed(x["id"], SPACE)
        if leave == "dissolved":
            w.spaces.dissolved.add(SPACE)
        else:
            w.spaces.local[SPACE] = []
        assert not await w.dir.sender_allowed(x["id"], SPACE)
        await w.dir.sweep_once()
        assert x["id"] not in w.spaces.rows[SPACE]
        assert ended == [x["id"]]
        assert "card" not in w.spaces.doc(SPACE, host["id"])


async def test_a_mailbox_only_snapshot_keeps_listed_members_rows():
    """Finding 4b: a snapshot of mailbox cards only names every member; a
    listed household we hold a mailbox-less card of keeps its row."""
    w, _host = _member_world()
    paired, gfs_only = _hh(), _hh()
    w.member(paired)
    w.spaces.put_card(SPACE, _card(paired, gfs=None))
    await w.directory(
        {
            "cards": [_card(gfs_only, gfs=GFS).to_wire()],
            "members": [paired["id"], gfs_only["id"]],
            "snapshot": True,
        }
    )
    assert w.spaces.doc(SPACE, paired["id"])["card"]["instance_id"] == paired["id"]


async def test_no_directory_frame_goes_to_a_household_not_yet_seated():
    """Found by ``all``: a delta sent when a joiner's seat goes live on the
    host can beat the joiner's own seat and be dropped as from a stranger.
    Directory frames go only to a household that said it is seated (its
    card, or a §25.6 BEGIN); a BEGIN from an unseated one answers with a
    snapshot."""
    w = World()
    a, j = _hh(), _hh()
    w.member(a, j)
    card_a = _card(a, gfs=GFS)
    w.spaces.put_card(SPACE, card_a)
    w.writers.writers[SPACE] = {a["id"], j["id"]}
    await w.dir.reconcile(SPACE, changed=[card_a])  # a mailbox card: a delta
    assert w.fed.to(j["id"]) == []
    await w.dir.on_sync_begin(_begin(j, w.me["id"], 0))
    (snap,) = w.fed.to(j["id"])
    assert snap["payload"]["snapshot"] is True


# ── Review round 3 (#1336) ───────────────────────────────────────────────


async def test_an_oversized_directory_snapshot_drops_nobody(monkeypatch):
    """Past ``MEMBERS_MAX`` households (the scale limit) the snapshot cannot
    name them all: it is sent without ``members`` and the receiver drops
    nobody (later deltas carry the removals)."""
    monkeypatch.setattr(smd, "MEMBERS_MAX", 2)
    host = World()
    to, *others = (_hh() for _ in range(4))
    host.member(to, *others)
    for h in others:
        host.spaces.put_card(SPACE, _card(h, gfs=GFS))
    host.writers.writers[SPACE] = {to["id"], *(h["id"] for h in others)}
    host.seat_all()
    await host.dir.reconcile(SPACE, snapshot_to={to["id"]})
    (snap,) = [d for d in host.fed.to(to["id"]) if d["payload"].get("snapshot")]
    assert all("members" not in d["payload"] for d in host.fed.raw_to(to["id"]))

    m, _h = _member_world()
    kept = _hh()
    m.member(kept)
    m.spaces.put_card(SPACE, _card(kept, gfs=GFS))
    await m.directory({**snap["payload"], "dir_seq": 5})
    assert kept["id"] in m.spaces.rows[SPACE]


async def test_a_delta_of_only_my_own_card_still_moves_my_version():
    """A delta that holds only the recipient's own card still reaches it,
    cardless, so its ``dir_seq`` keeps up (or every rotation of its own
    mailbox would cost it a full snapshot at its next sync)."""
    w = World()
    a = _hh()
    w.member(a)
    w.writers.writers[SPACE] = {a["id"]}
    w.seat_all()
    await w.card_from(a, _card(a, gfs=GFS, rev=1))
    before = w.spaces.doc(SPACE, w.me["id"])["dir_seq"]
    w.fed.directories.clear()
    await w.card_from(a, _card(a, gfs=GFS, rev=2))  # a rotation: newer card
    (frame,) = w.fed.to(a["id"])
    assert frame["payload"]["cards"] == []
    assert frame["payload"]["base_seq"] == before  # builds on the last one
    assert frame["payload"]["dir_seq"] == w.spaces.doc(SPACE, w.me["id"])["dir_seq"]


async def test_an_early_seated_card_is_forgotten_after_a_while():
    now = [1000.0]
    w = World()
    w.dir._clock = lambda: now[0]
    j = _hh()
    w.writers.writers[SPACE] = {j["id"]}
    await w.card_from(j, _card(j, gfs=GFS))  # before its row: kept aside
    assert w.dir._early_hello
    now[0] += smd.EARLY_HELLO_TTL_S + 1
    k = _hh()
    await w.card_from(k, _card(k, gfs=GFS))  # any later one prunes
    assert (SPACE, j["id"]) not in w.dir._early_hello


async def test_a_lost_removal_is_repaired_in_a_space_past_500_households():
    """Review round 4: past ``SNAPSHOT_MAX`` cards the snapshot still names
    every household — ids only, in its own first part (capped by bytes to
    fit one relay envelope) — so a lost removal is repaired there too."""
    host = World()
    member, gone = _hh(), _hh()
    others = [_hh() for _ in range(520)]
    host.member(member, gone, *others)
    for h in (member, gone, *others):
        host.spaces.put_card(SPACE, _card(h, gfs=GFS))
    host.writers.writers[SPACE] = {h["id"] for h in (member, gone, *others)}
    host.seat_all()
    await host.dir.reconcile(SPACE)
    host.writers.writers[SPACE].discard(gone["id"])
    await host.dir.reconcile(SPACE)  # the removal delta — lost
    host.fed.directories.clear()
    await host.dir.on_sync_begin(_begin(member, host.me["id"], 1))
    parts = [d["payload"] for d in host.fed.raw_to(member["id"])]
    assert parts[-1]["snapshot"] is True and parts[-1]["cards"] == []
    assert len(parts[-1]["members"]) == 521  # 520 others + our own
    assert gone["id"] not in parts[-1]["members"]
    assert all("members" not in p for p in parts[:-1])
    assert len({p["dir_seq"] for p in parts}) == 1

    m = World(host=host.me)
    m.member(gone)
    m.spaces.put_card(SPACE, _card(gone, gfs=GFS))
    await m.directory(parts[-1])
    assert gone["id"] not in m.spaces.rows[SPACE]


# ── Owner decision 2026-11-09: paired members own a mailbox too ─────────


async def test_the_directory_marks_which_households_reach_the_space_by_gfs():
    """The host knows which members reach the space only through a GFS (its
    sealed-redeem seats, with a relay): every directory frame names those
    among its cards in ``gfs_reached``."""
    w = World()
    gfs_only, paired = _hh(), _hh()
    w.member(gfs_only, paired)
    w.spaces.put_card(SPACE, _card(gfs_only, gfs=GFS))
    w.spaces.put_card(SPACE, _card(paired, gfs=GFS))
    w.fed_repo.instances[gfs_only["id"]] = _seat_row(GFS)
    w.writers.writers[SPACE] = {gfs_only["id"], paired["id"]}
    w.seat_all()
    await w.dir.reconcile(SPACE, snapshot_to={paired["id"]})
    (snap,) = [d for d in w.fed.to(paired["id"]) if d["payload"].get("snapshot")]
    assert snap["payload"]["gfs_reached"] == [gfs_only["id"]]


async def test_a_paired_member_needs_a_mailbox_where_a_gfs_reached_member_is():
    """A paired / mesh member connected to the GFS a GFS-reached member's
    card names needs its ONE mailbox there (minted at once); another paired
    member's mailbox never keeps it alive, so two of them cannot hold each
    other's mailboxes open once the GFS-reached member is gone."""
    w, _host = _member_world()
    gfs_only, paired = _hh(), _hh()
    await w.directory(
        {
            "cards": [_card(paired, gfs=GFS).to_wire()],
            "gfs_reached": [],
        }
    )
    assert await w.dir.needed_gfs() == set()
    await w.directory(
        {
            "cards": [_card(gfs_only, gfs=GFS).to_wire()],
            "gfs_reached": [gfs_only["id"]],
        }
    )
    assert await w.dir.needed_gfs() == {GFS}
    # Minted at once on receipt (no random timing rules, owner 2026-11-09).
    assert w.mailboxes.mints == 1
    await w.directory({"cards": [], "removed": [gfs_only["id"]]})
    assert await w.dir.needed_gfs() == set()


async def test_a_paired_members_card_names_its_mailbox_there_with_a_new_rev():
    """Once it holds that mailbox, its card for the space names it — under a
    newer ``rev`` even when the mailbox itself is older (minted for another
    space), so the host takes it."""
    w, _host = _member_world()
    w.mailboxes.rev = 3
    assert await w.dir.publish_cards() == 1
    first = w.fed.cards[-1]["payload"]["card"]
    assert first["mailbox"] is None and first["rev"] == 3
    w.mailboxes.add_box(GFS)  # held already (another space), rev unchanged
    gfs_only = _hh()
    await w.directory(
        {
            "cards": [_card(gfs_only, gfs=GFS).to_wire()],
            "gfs_reached": [gfs_only["id"]],
        }
    )
    second = w.fed.cards[-1]["payload"]["card"]
    assert second["mailbox"]["gfs"] == GFS
    assert second["rev"] > first["rev"]
    assert await w.dir.publish_cards() == 0  # nothing new since


async def test_a_member_no_longer_gfs_reached_frees_the_mailbox():
    """The host's next frame with that card drops the flag (the household
    paired with the host): our mailbox there is no longer needed."""
    w, _host = _member_world()
    other = _hh()
    card = _card(other, gfs=GFS).to_wire()
    await w.directory({"cards": [card], "gfs_reached": [other["id"]]})
    assert await w.dir.needed_gfs() == {GFS}
    await w.directory({"cards": [card], "gfs_reached": [], "snapshot": True})
    assert await w.dir.needed_gfs() == set()


# ── Review round 5 ──────────────────────────────────────────────────────


async def test_a_lost_snapshot_card_part_is_repaired_by_the_next_sync(monkeypatch):
    """Only a complete snapshot (every ``part`` of ``parts``) moves the
    member's ``dir_seq``. A lost card part leaves the member behind, so its
    next sync echo gets a fresh snapshot."""
    monkeypatch.setattr(smd, "SNAPSHOT_PART_MAX", 2)
    host = World()
    member, *others = (_hh() for _ in range(4))
    host.member(member, *others)
    for h in others:
        host.spaces.put_card(SPACE, _card(h, gfs=GFS))
    host.writers.writers[SPACE] = {member["id"], *(h["id"] for h in others)}
    host.seat_all()
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})
    parts = [
        d["payload"]
        for d in host.fed.raw_to(member["id"])
        if d["payload"].get("snapshot")
    ]
    assert len(parts) == 3 and "members" in parts[-1]
    seq = parts[-1]["dir_seq"]

    m = World(host=host.me)
    m.member(member)
    for part in (parts[1], parts[2]):  # parts[0] lost
        await m.directory(part)
    echo = await m.dir.directory_seq(SPACE, host.me["id"])
    assert echo != seq  # behind: the snapshot is not complete

    host.fed.directories.clear()
    host.dir._hello_at.clear()
    await host.dir.on_sync_begin(_begin(member, host.me["id"], echo or 0))
    again = [d["payload"] for d in host.fed.raw_to(member["id"])]
    for part in again:
        await m.directory(part)
    assert await m.dir.directory_seq(SPACE, host.me["id"]) == again[-1]["dir_seq"]
    assert all(m.spaces.doc(SPACE, h["id"]).get("card") for h in others)


async def test_a_gfs_reached_flag_change_goes_out_as_a_delta():
    """The host keeps who it flagged: a household that stops (or starts)
    being GFS-reached — its seat paired, no card change — goes out as a
    delta with the new flag, so no stale flag keeps a mailbox alive."""
    w = World()
    gfs_only, paired = _hh(), _hh()
    w.member(gfs_only, paired)
    w.spaces.put_card(SPACE, _card(gfs_only, gfs=GFS))
    w.spaces.put_card(SPACE, _card(paired, gfs=GFS))
    w.fed_repo.instances[gfs_only["id"]] = _seat_row(GFS)
    w.writers.writers[SPACE] = {gfs_only["id"], paired["id"]}
    w.seat_all()
    await w.dir.reconcile(SPACE)
    w.fed.directories.clear()
    await w.dir.reconcile(SPACE)
    assert w.fed.raw_to(paired["id"]) == []  # nothing changed
    w.fed_repo.instances[gfs_only["id"]] = _seat_row("")  # paired since
    await w.dir.reconcile(SPACE)
    (delta,) = w.fed.raw_to(paired["id"])
    assert [c["instance_id"] for c in delta["payload"]["cards"]] == [gfs_only["id"]]
    assert delta["payload"]["gfs_reached"] == []


# ── Review round 6 ──────────────────────────────────────────────────────


def _snapshot_world(monkeypatch, n_others=4):
    monkeypatch.setattr(smd, "SNAPSHOT_PART_MAX", 2)
    host = World()
    member, *others = (_hh() for _ in range(n_others + 1))
    host.member(member, *others)
    for h in others:
        host.spaces.put_card(SPACE, _card(h, gfs=GFS))
    host.writers.writers[SPACE] = {member["id"], *(h["id"] for h in others)}
    host.seat_all()
    m = World(host=host.me)
    m.member(member)
    return host, member, others, m


async def test_a_delta_never_completes_an_unfinished_snapshot(monkeypatch):
    """Snapshot S part 0 arrives, then delta S+1 (built on S), then the rest
    of S: the delta must not move us past the unfinished snapshot — it is
    taken only on the version it builds on (``base_seq``), so we stay
    behind and our next sync echo gets a fresh snapshot."""
    host, member, others, m = _snapshot_world(monkeypatch)
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})
    snap = [d["payload"] for d in host.fed.raw_to(member["id"])]
    seq = snap[-1]["dir_seq"]
    host.fed.directories.clear()
    newer = _card(others[0], gfs=GFS, rev=9)
    host.spaces.put_card(SPACE, newer)
    await host.dir.reconcile(SPACE, changed=[newer])
    (delta,) = [d["payload"] for d in host.fed.raw_to(member["id"])]
    assert delta["base_seq"] == seq and delta["dir_seq"] > seq

    await m.directory(snap[0])
    await m.directory(delta)  # out of order: dropped
    for part in snap[1:]:
        await m.directory(part)
    echo = await m.dir.directory_seq(SPACE, host.me["id"])
    assert echo == seq  # the snapshot, not the delta
    host.fed.directories.clear()
    host.dir._hello_at.clear()
    await host.dir.on_sync_begin(_begin(member, host.me["id"], echo))
    assert host.fed.raw_to(member["id"])  # repaired by a fresh snapshot


async def test_a_joiner_whose_snapshot_was_lost_is_repaired(monkeypatch):
    """The joiner's whole snapshot is lost; a later delta must not stand in
    for it."""
    host, member, others, m = _snapshot_world(monkeypatch)
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})  # all lost
    host.fed.directories.clear()
    newer = _card(others[0], gfs=GFS, rev=9)
    host.spaces.put_card(SPACE, newer)
    await host.dir.reconcile(SPACE, changed=[newer])
    (delta,) = [d["payload"] for d in host.fed.raw_to(member["id"])]
    m.spaces.put_card(SPACE, _card(host.me, gfs=GFS))  # the host's, from the ACK
    await m.directory(delta)
    echo = await m.dir.directory_seq(SPACE, host.me["id"])
    assert echo == 0
    host.fed.directories.clear()
    host.dir._hello_at.clear()
    await host.dir.on_sync_begin(_begin(member, host.me["id"], echo))
    assert host.fed.raw_to(member["id"])


async def test_a_paired_joiner_whose_first_snapshot_was_lost_is_repaired():
    """The demo's ``gfs-private-channel``: b (paired with the host, no
    mailbox) joins after the GFS-only e. The host decides b's first
    snapshot, then stops before it leaves; restarted, it holds b as seated
    and never sends it again. b holds NO card of the space — a paired
    joiner gets no host card — so it used to echo nothing on its §25.6
    sync and was never repaired: it never held e's card and dropped e's
    host hops. A writer seat with no directory echoes 0."""
    host = World()
    e, b = _hh(), _hh()
    host.member(e, b)
    host.spaces.put_card(SPACE, _card(e, gfs=GFS))
    host.writers.writers[SPACE] = {e["id"], b["id"]}
    await host.dir.store_seat_card(SPACE, _card(b, gfs=None))
    await host.card_from(b, _card(b, gfs=None))  # its "seated": snapshot
    assert host.fed.to(b["id"])  # ... which never arrives
    host.fed.directories.clear()
    host.dir._hello_at.clear()  # the host restarted

    m = World(host=host.me)
    m.member(e)
    assert m.spaces.doc(SPACE, host.me["id"]) == {}
    echo = await m.dir.directory_seq(SPACE, host.me["id"])
    assert echo == 0
    await host.dir.on_sync_begin(_begin(b, host.me["id"], echo))
    (snap,) = host.fed.to(b["id"])
    for part in host.fed.raw_to(b["id"]):
        await m.directory(part["payload"])
    assert _ids(snap) == {e["id"]}
    assert m.spaces.doc(SPACE, e["id"])["card"]["instance_id"] == e["id"]


class YieldingFederation(FakeFederation):
    """A send that takes a moment on the wire, as a real one does: another
    handler's directory frames can be sent while ours is in flight."""

    async def broadcast_to_space_members(self, *args, **kwargs):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await super().broadcast_to_space_members(*args, **kwargs)


async def test_a_seated_snapshot_and_a_joiners_delta_reach_a_member_in_order(
    monkeypatch,
):
    """The demo's ``gfs-private-channel``: the GFS-only e says it is seated
    (its card — the host answers with a snapshot) while the paired b's card
    comes in (a delta to every seated member, e included). Both run at
    once on the host. Every frame to e must build on the one before it —
    a delta on the version e holds, never between a snapshot's parts nor
    ahead of the snapshot it builds on — or e drops b's card and, the host
    having just answered e's "seated", nobody repairs it before e's next
    sync: e's host hops to b fail for minutes."""
    host = World()
    host.fed = YieldingFederation()
    host.dir.attach_to(host.fed)
    e, b = _hh(), _hh()
    host.member(e, b)
    host.writers.writers[SPACE] = {e["id"], b["id"]}
    card_e = _card(e, gfs=GFS)
    host.spaces.put_card(SPACE, card_e)  # stored at e's redeem

    await asyncio.gather(
        host.card_from(e, card_e),  # e: "seated" → snapshot
        # b's join stores its card: a delta to every seated member.
        host.dir.store_seat_card(SPACE, _card(b, gfs=None)),
    )

    m = World(host=host.me)
    m.member(b)
    for frame in host.fed.raw_to(e["id"]):
        await m.directory(frame["payload"])
    # e holds b's card — what its host hop to b is sealed to.
    assert m.spaces.doc(SPACE, b["id"]).get("card", {}).get("instance_id") == b["id"]
    held = await m.dir.directory_seq(SPACE, host.me["id"])
    assert held == host.spaces.doc(SPACE, host.me["id"])["dir_seq"]


async def test_a_follower_or_a_space_we_left_echoes_no_directory():
    w, host = _member_world()
    w.spaces.local[SPACE] = []  # no person of ours holds a writer seat
    assert await w.dir.directory_seq(SPACE, host["id"]) is None
    assert await w.dir.directory_seq(SPACE, _hh()["id"]) is None  # not the host


async def test_a_member_missing_its_directory_asks_the_host_at_once():
    """A delta on a version we do not hold (our snapshot was lost): we give
    the host our card again — the "seated" it answers with a snapshot —
    at most once per :data:`HELLO_RESNAPSHOT_S` for the space."""
    now = [1000.0]
    w, host = _member_world()
    w.dir._clock = lambda: now[0]
    await w.dir.seated(SPACE, host["id"], host_proto=89)
    assert len(w.fed.cards) == 1
    a = _hh()
    delta = {"cards": [_card(a, gfs=GFS).to_wire()], "dir_seq": 5, "base_seq": 4}
    await w.directory(delta)
    assert [c["to"] for c in w.fed.cards] == [host["id"]] * 2
    assert w.fed.cards[-1]["payload"]["card"] == w.fed.cards[0]["payload"]["card"]
    await w.directory({**delta, "dir_seq": 6, "base_seq": 5})
    await w.dir.ask_host(SPACE)
    assert len(w.fed.cards) == 2  # rate-limited
    now[0] += HELLO_RESNAPSHOT_S
    await w.dir.ask_host(SPACE)
    assert len(w.fed.cards) == 3


async def test_asking_the_host_is_a_no_op_where_we_are_the_host_or_not_in_it():
    w = World()  # we host SPACE
    await w.dir.ask_host(SPACE)
    await w.dir.ask_host("no-such-space")
    assert w.fed.cards == []


async def test_a_repeated_part_never_stands_in_for_a_lost_one(monkeypatch):
    host, member, _others, m = _snapshot_world(monkeypatch)
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})
    snap = [d["payload"] for d in host.fed.raw_to(member["id"])]
    assert len(snap) == 3  # 2 + 2 cards, then members
    for part in (snap[0], snap[0], snap[2]):  # part 1 lost, part 0 twice
        await m.directory(part)
    assert await m.dir.directory_seq(SPACE, host.me["id"]) != snap[-1]["dir_seq"]


async def test_a_snapshot_claiming_too_many_parts_is_ignored(monkeypatch):
    w, _host = _member_world()
    a = _hh()
    await w.directory(
        {
            "cards": [_card(a, gfs=GFS).to_wire()],
            "snapshot": True,
            "part": 0,
            "parts": smd.SNAPSHOT_MAX + 2,
        }
    )
    assert a["id"] not in w.spaces.rows[SPACE]


async def test_a_forgotten_space_drops_its_unfinished_snapshot(monkeypatch):
    host, member, _others, m = _snapshot_world(monkeypatch)
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})
    snap = [d["payload"] for d in host.fed.raw_to(member["id"])]
    await m.directory(snap[0])
    assert SPACE in m.dir._snap_in
    m.spaces.local[SPACE] = []  # we left
    await m.dir.sweep_once()
    assert SPACE not in m.dir._snap_in


async def test_our_own_mailbox_change_reaches_hosted_spaces_and_hosts_at_once():
    """No card delay (owner 2026-11-09): a mailbox change reconciles the
    spaces we host and gives every other host our card in the same call."""
    w, host = _member_world()
    w.mailboxes.rev = 3
    await w.mailboxes.on_change()
    assert [c["to"] for c in w.fed.cards] == [host["id"]]


async def test_a_mailbox_only_member_takes_mailbox_deltas_after_its_snapshot():
    """Round 7: a household given mailbox cards only builds on
    ``dir_seq_mbx`` — its snapshot must carry that version, or the next
    mailbox delta (``base_seq`` = ``dir_seq_mbx``) would be dropped."""
    host = World()
    m_only, carded, plain = _hh(), _hh(), _hh()
    host.member(m_only, carded, plain)
    host.spaces.put_card(SPACE, _card(carded, gfs=GFS))
    host.writers.writers[SPACE] = {m_only["id"], carded["id"], plain["id"]}
    host.seat_all()
    await host.dir.reconcile(SPACE)
    plain_card = _card(plain, gfs=None)
    host.spaces.put_card(SPACE, plain_card)  # a non-mailbox change
    host.seat_all()
    await host.dir.reconcile(SPACE, changed=[plain_card])
    own = host.spaces.doc(SPACE, host.me["id"])
    assert own["dir_seq"] != own["dir_seq_mbx"], own
    host.fed.directories.clear()
    host.dir._hello_at.clear()
    await host.dir.reconcile(SPACE, snapshot_to={m_only["id"]})
    snap = [d["payload"] for d in host.fed.raw_to(m_only["id"])]
    host.fed.directories.clear()
    newer = _card(carded, gfs=GFS, rev=9)
    host.spaces.put_card(SPACE, newer)
    await host.dir.reconcile(SPACE, changed=[newer])
    (delta,) = [d["payload"] for d in host.fed.raw_to(m_only["id"])]
    assert delta["base_seq"] == snap[-1]["dir_seq"]

    m = World(host=host.me)
    m.member(m_only)
    for part in snap:
        await m.directory(part)
    await m.directory(delta)
    assert m.spaces.doc(SPACE, carded["id"])["card"]["rev"] == 9


async def test_a_part_of_an_older_snapshot_is_dropped(monkeypatch):
    """Round 7: a late part of an OLDER snapshot never mixes into the one
    coming in."""
    host, member, others, m = _snapshot_world(monkeypatch)
    await host.dir.reconcile(SPACE, snapshot_to={member["id"]})
    old = [d["payload"] for d in host.fed.raw_to(member["id"])]
    host.fed.directories.clear()
    host.dir._hello_at.clear()
    newer = _card(others[0], gfs=GFS, rev=9)
    host.spaces.put_card(SPACE, newer)
    await host.dir.reconcile(SPACE, changed=[newer], snapshot_to={member["id"]})
    new = [d["payload"] for d in host.fed.raw_to(member["id"])]
    assert new[0]["dir_seq"] > old[0]["dir_seq"]
    await m.directory(new[0])
    await m.directory(old[1])  # late, older: dropped
    await m.directory(new[1])
    await m.directory(new[2])
    assert await m.dir.directory_seq(SPACE, host.me["id"]) == new[-1]["dir_seq"]
    assert m.dir._snap_in == {}
    late = old[0]
    await m.directory(late)
    assert m.dir._snap_in == {}

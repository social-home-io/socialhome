"""Host-sequenced space pages (§4.4.4.1, v_48).

Every member household holds a copy of each space page, and anyone with a
writer seat may edit it — but only ONE household decides the order of
versions: the space's **host** (``space.owner_instance_id``). A decentral
merge was shown to diverge (non-associative merges, lost undos, history
caps, split conflict sets); a single sequencer cannot.

* **Host** — :meth:`PageConflictService.sequence` takes each edit (a local
  one, or a member household's *proposal*) under the page lock, after the
  inbound gates: a duplicate is acknowledged; a proposal based on the
  current version fast-forwards; one based on an older version is merged
  three-way against that version (looked up by hash in the history) with a
  bounded paragraph diff3; anything it cannot merge becomes a **conflict
  side** (one per user, at most :data:`MAX_CONFLICT_SIDES`, overflow moves
  to history — never refused, never lost). Each committed change bumps
  ``seq`` and is broadcast as the canonical version, with the conflict list.
  Conflicts never block edits.
* **Member** — a local edit is an optimistic **draft** (``pending_base_seq``
  set) that :class:`~.page_proposal_forwarder.PageProposalForwarder` sends
  to the host, stop-and-wait. :meth:`PageConflictService.mirror` applies the
  host's versions by ``seq`` only (newer → applied, older → ignored), keeps
  an unacknowledged draft on top, and settles it when the host's version
  that sequenced it (or a refusal) comes back.
* **Legacy** — a host below v_48: last write wins, as before.

The version hash (title + content + cover) only *recognises* versions
(base check, duplicates, acknowledgements); ``seq`` alone orders them.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    PageConflictEmitted,
    PageCreated,
    PageProposalSettled,
    PageUpdated,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.page import Page, PageVersion
from ..domain.page_version import (
    SCALAR_CONFLICT,
    DraftBase,
    PageConflictSide,
    is_version_hash,
    merge_scalar,
    version_hash,
)
from ..rate_limiter import RateLimiter
from ..repositories.page_repo import AbstractPageRepo, PageNotFoundError
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: Largest body (UTF-8 bytes, per side) the automatic merge looks at; a
#: bigger one becomes a conflict side instead.
MERGE_MAX_BYTES = 128 * 1024

#: Most paragraphs (per side) the automatic merge looks at.
MERGE_MAX_PARAGRAPHS = 2000

#: Work budget of one bounded diff (Myers steps + trace copies). Exhausted
#: → conflict side, never an unbounded CPU burn under the page lock.
OPS_BUDGET = 200_000

#: Most versions an open conflict holds; the oldest moves to history.
MAX_CONFLICT_SIDES = 3

#: Most bytes (UTF-8, all sides together) an open conflict holds.
SIDES_MAX_BYTES = 160 * 1024

#: Most side ids a proposal may resolve.
MAX_RESOLVES = 5

#: Proposals the host takes per (household, space) per minute.
PROPOSALS_PER_MINUTE = 120

#: Refusals the host sends per (household, space) per minute — on top of
#: one per (household, page) — so varying the page id floods nobody.
REFUSALS_PER_SENDER_PER_MINUTE = 30

#: Refusals that carry no page state: the proposer keeps (or restores) its
#: own copy, and nothing of the page is revealed to the sender.
_STATELESS_REFUSALS = frozenset({"rate_limited", "archived"})

#: The largest ``seq`` anyone commits or accepts (a JSON-safe integer).
MAX_SEQ = 2**53

#: How far one proposal may raise a (restored) host's seq floor. A larger
#: claim is a lie or a corrupt copy: it does not move ``seq`` — the edit is
#: kept as a side one step up — so nobody can push a page to ``MAX_SEQ``.
MAX_FLOOR_STEP = 2**20

#: ``resolve_conflict`` resolutions. ``side`` keeps a version by hash;
#: ``mine`` / ``theirs`` are the two-way names (the shown body / the newest
#: other side).
RESOLUTIONS = ("side", "merged_content", "mine", "theirs")

#: Refusal reasons a host acknowledges a proposal with.
REFUSAL_REASONS = frozenset({"access", "archived", "gone", "rate_limited", "bad_base"})


# ─── Errors ──────────────────────────────────────────────────────────────


class PageConflictError(Exception):
    """Base class for conflict-resolution errors."""


class NoActiveConflictError(PageConflictError):
    """``resolve_conflict`` called but the page has no open conflict."""


class PageConflictStaleError(PageConflictError):
    """The resolver saw other sides than the conflict holds now (409
    ``STALE``) — reload and pick again."""

    def __init__(self, sides: Sequence[str]) -> None:
        super().__init__("the conflicting versions changed — reload and pick again")
        self.sides = list(sides)


# ─── Bounded diff3 ───────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MergeResult:
    """Outcome of a three-way merge."""

    #: The merged body ("" when ``has_conflict``).
    content: str

    #: The two sides changed the same region differently, or a body is
    #: over the merge caps / the diff over its budget: nothing was merged.
    has_conflict: bool


#: A change of one side against the base: replace ``base[i1:i2]`` with
#: ``repl`` (``i1 == i2`` is an insertion).
_Hunk = tuple[int, int, tuple[str, ...]]


class _OverBudget(Exception):
    """A diff ran out of :data:`OPS_BUDGET`."""


def _split_paragraphs(body: str) -> list[str]:
    """Split ``body`` into paragraph blocks (two or more newlines apart);
    trailing whitespace dropped."""
    if not body:
        return []
    normalised = body.replace("\r\n", "\n").rstrip()
    parts = re.split(r"\n\s*\n", normalised)
    return [p for p in parts if p.strip()]


def _join_paragraphs(parts: Sequence[str]) -> str:
    return "\n\n".join(parts)


def _myers_moves(a: Sequence[int], b: Sequence[int], budget: int) -> list[str]:
    """The edit script of ``a`` → ``b`` as moves ``"="`` / ``"-"`` / ``"+"``
    (Myers O(ND)). Raises :class:`_OverBudget` past ``budget`` steps."""
    n, m = len(a), len(b)
    v: dict[int, int] = {1: 0}
    trace: list[dict[int, int]] = []
    ops = 0
    found = False
    for d in range(n + m + 1):
        trace.append(dict(v))
        ops += len(v)
        for k in range(-d, d + 1, 2):
            ops += 1
            if k == -d or (k != d and v[k - 1] < v[k + 1]):
                x = v[k + 1]
            else:
                x = v[k - 1] + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
                ops += 1
            v[k] = x
            if x >= n and y >= m:
                found = True
                break
        if ops > budget:
            raise _OverBudget
        if found:
            break
    moves: list[str] = []
    x, y = n, m
    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            moves.append("=")
            x -= 1
            y -= 1
        if d > 0:
            moves.append("+" if x == prev_x else "-")
        x, y = prev_x, prev_y
    moves.reverse()
    return moves


def _bounded_hunks(
    base: list[str], other: list[str], ids: dict[str, int]
) -> list[_Hunk]:
    """``other``'s changes against ``base`` as hunks: common prefix and
    suffix trimmed, the rest diffed with a budgeted Myers."""
    a = [ids.setdefault(p, len(ids)) for p in base]
    b = [ids.setdefault(p, len(ids)) for p in other]
    n, m = len(a), len(b)
    pre = 0
    while pre < n and pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and a[n - 1 - suf] == b[m - 1 - suf]:
        suf += 1
    moves = _myers_moves(a[pre : n - suf], b[pre : m - suf], OPS_BUDGET)
    hunks: list[_Hunk] = []
    i = j = pre
    start: tuple[int, int] | None = None
    for move in [*moves, "="]:
        if move == "=":
            if start is not None:
                hunks.append((start[0], i, tuple(other[start[1] : j])))
                start = None
            i += 1
            j += 1
            continue
        if start is None:
            start = (i, j)
        if move == "-":
            i += 1
        else:
            j += 1
    return hunks


def _touch(a: _Hunk, b: _Hunk) -> bool:
    """Do two hunks of different sides compete for the same base region?
    Overlapping ranges do, and so do two insertions at one point; an
    insertion at the edge of the other's range does not."""
    a1, a2, _ = a
    b1, b2, _ = b
    if a1 < b2 and b1 < a2:
        return True
    return a1 == a2 == b1 == b2


def _too_big(*bodies: str) -> bool:
    return any(len(b.encode("utf-8")) > MERGE_MAX_BYTES for b in bodies)


def diff3_merge(base: str, current: str, proposal: str) -> MergeResult:
    """Paragraph-level three-way merge of a ``proposal`` into the host's
    ``current`` body, both made from ``base``.

    Each side's changes against the base are bounded Myers hunks.
    Non-overlapping hunks both apply; identical changes apply once; two
    different insertions at one point both apply — the current body's
    first, then the proposal's (the sequencer's order). Any other overlap,
    a body over :data:`MERGE_MAX_BYTES` / :data:`MERGE_MAX_PARAGRAPHS`, or
    a diff over :data:`OPS_BUDGET` is a conflict: nothing is merged.
    """
    if current == proposal:
        return MergeResult(content=current, has_conflict=False)
    if proposal == base:
        return MergeResult(content=current, has_conflict=False)
    if current == base:
        return MergeResult(content=proposal, has_conflict=False)
    if _too_big(base, current, proposal):
        return MergeResult(content="", has_conflict=True)
    b = _split_paragraphs(base)
    c = _split_paragraphs(current)
    p = _split_paragraphs(proposal)
    if max(len(b), len(c), len(p)) > MERGE_MAX_PARAGRAPHS:
        return MergeResult(content="", has_conflict=True)
    ids: dict[str, int] = {}
    try:
        ours = _bounded_hunks(b, c, ids)
        theirs = _bounded_hunks(b, p, ids)
    except _OverBudget:
        return MergeResult(content="", has_conflict=True)

    partners_of_ours: list[list[int]] = [[] for _ in ours]
    partners_of_theirs: list[list[int]] = [[] for _ in theirs]
    for i, h in enumerate(ours):
        for j, o in enumerate(theirs):
            if o[0] > h[1]:
                break
            if _touch(h, o):
                partners_of_ours[i].append(j)
                partners_of_theirs[j].append(i)

    edits: list[_Hunk] = []
    for i, h in enumerate(ours):
        partners = partners_of_ours[i]
        if not partners:
            edits.append(h)
            continue
        if len(partners) != 1 or len(partners_of_theirs[partners[0]]) != 1:
            return MergeResult(content="", has_conflict=True)
        o = theirs[partners[0]]
        if h == o:
            edits.append(h)
        elif h[0] == h[1] == o[0] == o[1]:
            edits.append((h[0], h[1], h[2] + o[2]))
        else:
            return MergeResult(content="", has_conflict=True)
    edits.extend(o for j, o in enumerate(theirs) if not partners_of_theirs[j])
    edits.sort(key=lambda e: (e[0], e[1]))

    out: list[str] = []
    pos = 0
    for i1, i2, repl in edits:
        out.extend(b[pos:i1])
        out.extend(repl)
        pos = i2
    out.extend(b[pos:])
    return MergeResult(content=_join_paragraphs(out), has_conflict=False)


# ─── Wire shapes ─────────────────────────────────────────────────────────


class PageMode(enum.Enum):
    """How this household treats a space's pages."""

    #: This household hosts the space: it sequences every page version.
    HOST = "host"
    #: A v_48 host sequences; this household proposes and mirrors.
    MEMBER = "member"
    #: The host is below v_48 (or unknown): last write wins, as before.
    LEGACY = "legacy"


@dataclass(slots=True, frozen=True)
class Sequenced:
    """Which proposal a host version answers (``sequenced`` on the wire)."""

    proposer_instance: str
    proposal_hash: str
    outcome: str  # "applied" | "refused"
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "proposer_instance": self.proposer_instance,
            "proposal_hash": self.proposal_hash,
            "outcome": self.outcome,
        }
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_wire(cls, raw: object) -> "Sequenced | None":
        if not isinstance(raw, Mapping):
            return None
        outcome = raw.get("outcome")
        proposer = raw.get("proposer_instance")
        ph = raw.get("proposal_hash")
        reason = raw.get("reason")
        if outcome not in ("applied", "refused") or not isinstance(proposer, str):
            return None
        if not is_version_hash(ph):
            return None
        if reason is not None and reason not in REFUSAL_REASONS:
            return None
        return cls(
            proposer_instance=proposer,
            proposal_hash=str(ph),
            outcome=str(outcome),
            reason=reason if isinstance(reason, str) else None,
        )


@dataclass(slots=True, frozen=True)
class Proposal:
    """A member's version of a page, sent to the host for sequencing."""

    title: str
    content: str
    cover_image_url: str | None = None
    actor_user_id: str = ""
    #: The canonical ``seq`` it was made from; ``None`` from a pre-v_48
    #: sender (treated as based on the current version: last write wins).
    base_seq: int | None = None
    base_hash: str | None = None
    #: Side ids (hashes) this proposal resolves.
    resolves: tuple[str, ...] = ()
    #: For a proposed create: the creator.
    created_by: str = ""

    @property
    def hash(self) -> str:
        return version_hash(self.title, self.content, self.cover_image_url)


@dataclass(slots=True, frozen=True)
class CanonicalVersion:
    """The host's version of a page (or its refusal) as a member reads it."""

    seq: int
    title: str = ""
    content: str = ""
    cover_image_url: str | None = None
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_editor_user_id: str = ""
    conflict: tuple[PageConflictSide, ...] = ()
    sequenced: Sequenced | None = None
    #: The payload carried a page state (a ``gone`` refusal carries none).
    has_state: bool = True


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def proposal_from_wire(p: Mapping[str, Any]) -> Proposal | None:
    """Parse a proposal; ``None`` when malformed (a bad base, too many
    resolves). A payload without ``base_seq`` is a pre-v_48 sender's."""
    base_seq: int | None = None
    base_hash: str | None = None
    if "base_seq" in p:
        base_seq = _int(p.get("base_seq"))
        if base_seq is None or base_seq < 0 or base_seq > MAX_SEQ:
            return None
        raw_hash = p.get("base_hash")
        if raw_hash is not None:
            if not is_version_hash(raw_hash):
                return None
            base_hash = str(raw_hash)
    resolves: tuple[str, ...] = ()
    if "resolves" in p:
        raw = p.get("resolves")
        if (
            not isinstance(raw, list)
            or len(raw) > MAX_RESOLVES
            or not all(is_version_hash(r) for r in raw)
        ):
            return None
        resolves = tuple(dict.fromkeys(str(r) for r in raw))
    cover = p.get("cover_image_url")
    return Proposal(
        title=str(p.get("title") or ""),
        content=str(p.get("content") or ""),
        cover_image_url=cover if isinstance(cover, str) and cover else None,
        actor_user_id=str(p.get("actor_user_id") or ""),
        base_seq=base_seq,
        base_hash=base_hash,
        resolves=resolves,
        created_by=str(p.get("created_by") or ""),
    )


def _side_from_wire(raw: object) -> PageConflictSide | None:
    if not isinstance(raw, Mapping):
        return None
    title, content, by, at = (
        raw.get("title"),
        raw.get("content"),
        raw.get("by"),
        raw.get("at"),
    )
    if not all(isinstance(v, str) for v in (title, content, by, at)) or not at:
        return None
    cover = raw.get("cover")
    cover_s = cover if isinstance(cover, str) and cover else None
    base_seq = _int(raw.get("base_seq")) or 0
    return PageConflictSide(
        hash=version_hash(str(title), str(content), cover_s),
        title=str(title),
        content=str(content),
        cover_image_url=cover_s,
        by=str(by),
        at=str(at),
        base_seq=max(0, base_seq),
    )


def canonical_from_wire(p: Mapping[str, Any]) -> CanonicalVersion | None:
    """Parse a host version; ``None`` when malformed. A record without
    ``seq`` is not a v_48 host version."""
    seq = _int(p.get("seq"))
    if seq is None or seq < 0 or seq > MAX_SEQ:
        return None
    raw_conflict = p.get("conflict", [])
    if not isinstance(raw_conflict, list) or len(raw_conflict) > MAX_CONFLICT_SIDES:
        return None
    sides = []
    for raw in raw_conflict:
        side = _side_from_wire(raw)
        if side is None:
            return None
        sides.append(side)
    sequenced = None
    if "sequenced" in p:
        sequenced = Sequenced.from_wire(p.get("sequenced"))
        if sequenced is None:
            return None
    title = p.get("title")
    has_state = isinstance(title, str) and bool(title)
    cover = p.get("cover_image_url")
    return CanonicalVersion(
        seq=seq,
        title=str(title or ""),
        content=str(p.get("content") or ""),
        cover_image_url=cover if isinstance(cover, str) and cover else None,
        created_by=str(p.get("created_by") or ""),
        created_at=str(p.get("created_at") or p.get("occurred_at") or ""),
        updated_at=str(p.get("updated_at") or p.get("occurred_at") or ""),
        last_editor_user_id=str(p.get("last_editor_user_id") or ""),
        conflict=tuple(sides),
        sequenced=sequenced,
        has_state=has_state,
    )


def side_to_wire(side: PageConflictSide) -> dict[str, Any]:
    return {
        "side_id": side.hash,
        "title": side.title,
        "content": side.content,
        "cover": side.cover_image_url,
        "by": side.by,
        "base_seq": side.base_seq,
        "at": side.at,
    }


def canonical_extras(
    page: Page, sides: Sequence[PageConflictSide], sequenced: Sequenced | None = None
) -> dict[str, Any]:
    """The v_48 fields of a host version of ``page`` (beside id, title,
    content): what a member needs to mirror it exactly."""
    out: dict[str, Any] = {
        "cover_image_url": page.cover_image_url,
        "updated_at": page.updated_at,
        "last_editor_user_id": page.last_editor_user_id or page.created_by,
        "seq": page.seq,
        "version_hash": version_hash(page.title, page.content, page.cover_image_url),
        "conflict": [side_to_wire(s) for s in sides],
    }
    if sequenced is not None:
        out["sequenced"] = sequenced.to_wire()
    return out


# ─── Engine ──────────────────────────────────────────────────────────────


class SequenceOutcome(enum.StrEnum):
    """What :meth:`PageConflictService.sequence` did with an edit."""

    #: Already here (the current body or a side): acknowledged, unchanged.
    DUPLICATE = "duplicate"
    #: Became the canonical body (fast-forward or clean merge).
    APPLIED = "applied"
    #: Recorded as a conflict side.
    SIDE = "side"
    #: Refused (``reason`` says why).
    REFUSED = "refused"


@dataclass(slots=True, frozen=True)
class SequenceResult:
    outcome: SequenceOutcome
    reason: str | None = None
    page: Page | None = None
    #: The proposal created the page (its first version).
    created: bool = False
    #: The open sides as committed with this version (snapshotted under
    #: the page lock, so version N ships with N's sides).
    sides: tuple[PageConflictSide, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _editor(page: Page) -> str:
    return page.last_editor_user_id or page.created_by


def _page_hash(page: Page) -> str:
    return version_hash(page.title, page.content, page.cover_image_url)


def _hash_versions(versions: list[PageVersion]) -> list[tuple[str, PageVersion]]:
    """``(hash, version)`` newest first."""
    return [
        (version_hash(v.title, v.content, v.cover_image_url), v)
        for v in sorted(versions, key=lambda v: v.version, reverse=True)
    ]


@dataclass(slots=True)
class _HostState:
    """One sequencing step's working copy (module-local)."""

    page: Page
    sides: list[PageConflictSide]
    retired: list[PageConflictSide] = field(default_factory=list)


class PageConflictService(BusPublisherMixin):
    """Sequence (host) or mirror (member) space-page versions, and record /
    resolve the conflicts concurrent edits leave."""

    __slots__ = (
        "_pages",
        "_bus",
        "_locks",
        "_federation",
        "_own_instance_id",
        "_spaces",
        "_limiter",
    )

    def __init__(
        self, page_repo: AbstractPageRepo, *, bus: "EventBus | None" = None
    ) -> None:
        self._pages = page_repo
        self._bus = bus
        self._locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._federation: "FederationService | None" = None
        self._own_instance_id = ""
        self._spaces: "AbstractSpaceRepo | None" = None
        self._limiter = RateLimiter()

    def attach_federation(
        self,
        federation: "FederationService",
        *,
        own_instance_id: str,
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        """Wire the federation side. Without it this household sequences
        every page itself (a lone household is its own host)."""
        self._federation = federation
        self._own_instance_id = own_instance_id
        self._spaces = space_repo

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    # ─── Shared state ─────────────────────────────────────────────────────

    def lock_for(self, space_id: str, page_id: str) -> asyncio.Lock:
        """The lock serialising every change of one space page here.
        Callers hold the returned lock while they use it (the registry keeps
        only weak references)."""
        key = (space_id, page_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def mode(self, space_id: str) -> tuple[PageMode, str]:
        """``(mode, host instance id)`` for ``space_id`` here: HOST when this
        household hosts it (or no federation is wired), MEMBER under a v_48
        host, LEGACY otherwise."""
        if self._federation is None or self._spaces is None:
            return PageMode.HOST, self._own_instance_id
        space = await self._spaces.get(space_id)
        if space is None:
            return PageMode.LEGACY, ""
        owner = space.owner_instance_id
        if owner == self._own_instance_id:
            return PageMode.HOST, owner
        if await self._federation.peer_supports(
            owner, min_version=FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES
        ):
            return PageMode.MEMBER, owner
        return PageMode.LEGACY, owner

    async def has_active_conflict(self, page_id: str, *, space_id: str) -> bool:
        return await self._pages.has_active_conflict(page_id, space_id=space_id)

    async def sides(self, space_id: str, page_id: str) -> list[PageConflictSide]:
        """The open conflict's versions (empty: no conflict)."""
        return await self._pages.list_conflict_sides(page_id, space_id=space_id)

    async def _history(
        self, space_id: str, page_id: str
    ) -> list[tuple[str, PageVersion]]:
        versions = await self._pages.list_versions(page_id, space_id=space_id)
        return await asyncio.to_thread(_hash_versions, versions)

    async def _remember(
        self,
        page: Page,
        *,
        title: str,
        content: str,
        cover_image_url: str | None,
        edited_by: str,
        space_id: str,
    ) -> None:
        """Write one version of ``page`` into its history."""
        await self._pages.save_version(
            PageVersion(
                id=uuid.uuid4().hex,
                page_id=page.id,
                version=await self._pages.next_version_number(page.id),
                title=title,
                content=content,
                edited_by=edited_by or page.created_by,
                edited_at=_now(),
                space_id=space_id,
                cover_image_url=cover_image_url,
            )
        )

    async def _remember_page(self, page: Page, space_id: str) -> None:
        await self._remember(
            page,
            title=page.title,
            content=page.content,
            cover_image_url=page.cover_image_url,
            edited_by=_editor(page),
            space_id=space_id,
        )

    async def _remember_side(
        self, page: Page, side: PageConflictSide, space_id: str
    ) -> None:
        await self._remember(
            page,
            title=side.title,
            content=side.content,
            cover_image_url=side.cover_image_url,
            edited_by=side.by,
            space_id=space_id,
        )

    # ─── Host: sequence an edit ───────────────────────────────────────────

    async def sequence(
        self,
        *,
        space_id: str,
        page_id: str,
        proposal: Proposal,
        proposer_instance: str = "",
    ) -> SequenceResult:
        """Sequence ``proposal`` (after every gate) — a local edit
        (``proposer_instance`` empty or our own) or a member household's.
        Commits and broadcasts a new canonical version when anything
        changed; a remote proposer that learns nothing from the broadcast
        gets a targeted acknowledgement."""
        remote = bool(proposer_instance) and proposer_instance != self._own_instance_id
        if remote and not self._limiter.is_allowed(
            f"page-proposals:{proposer_instance}:{space_id}",
            limit=PROPOSALS_PER_MINUTE,
            window_s=60,
        ):
            log.warning(
                "page %s in space %s: proposals from %s over the rate limit — refused",
                page_id,
                space_id,
                proposer_instance,
            )
            await self.refuse(
                space_id=space_id,
                page_id=page_id,
                proposal_hash=proposal.hash,
                proposer_instance=proposer_instance,
                reason="rate_limited",
            )
            return SequenceResult(SequenceOutcome.REFUSED, "rate_limited")
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None:
                result = await self._sequence_create(space_id, page_id, proposal)
            else:
                result = await self._sequence_edit(space_id, page, proposal)
        await self._publish_result(
            result,
            space_id=space_id,
            page_id=page_id,
            proposal=proposal,
            proposer_instance=proposer_instance,
        )
        return result

    async def commit_local(
        self,
        *,
        space_id: str,
        page_id: str,
        actor_user_id: str,
        patch: Mapping[str, Any],
        resolves: Sequence[str] = (),
        precheck: "Callable[[Page], Awaitable[None]] | None" = None,
    ) -> Page:
        """A local edit on the host: ``patch`` over the current version,
        sequenced as a fast-forward (``precheck`` runs on the page under the
        lock first and may raise). Returns the page as it now is."""
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None:
                raise PageNotFoundError(page_id)
            if precheck is not None:
                await precheck(page)
            proposal = Proposal(
                title=str(patch.get("title", page.title)),
                content=str(patch.get("content", page.content) or ""),
                cover_image_url=patch.get("cover_image_url", page.cover_image_url),
                actor_user_id=actor_user_id,
                base_seq=page.seq,
                resolves=tuple(resolves),
            )
            result = await self._sequence_edit(space_id, page, proposal)
        await self._publish_result(
            result,
            space_id=space_id,
            page_id=page_id,
            proposal=proposal,
            proposer_instance="",
        )
        return result.page or page

    async def _publish_result(
        self,
        result: SequenceResult,
        *,
        space_id: str,
        page_id: str,
        proposal: Proposal,
        proposer_instance: str,
    ) -> None:
        remote = bool(proposer_instance) and proposer_instance != self._own_instance_id
        sequenced = Sequenced(
            proposer_instance=proposer_instance or self._own_instance_id,
            proposal_hash=proposal.hash,
            outcome="refused"
            if result.outcome is SequenceOutcome.REFUSED
            else "applied",
            reason=result.reason,
        )
        committed = result.outcome in (SequenceOutcome.APPLIED, SequenceOutcome.SIDE)
        if committed and result.page is not None:
            await self._broadcast(
                result.page,
                space_id=space_id,
                actor=proposal.actor_user_id,
                sequenced=sequenced,
                created=result.created,
                sides=result.sides,
            )
            if result.outcome is SequenceOutcome.SIDE:
                await self._emit(
                    PageConflictEmitted(
                        page_id=page_id,
                        space_id=space_id,
                        theirs=proposal.content,
                        theirs_by=proposal.actor_user_id,
                        federated=remote,
                    )
                )
        elif remote:
            await self._ack(
                space_id=space_id,
                page_id=page_id,
                page=result.page,
                sequenced=sequenced,
                proposer_instance=proposer_instance,
            )

    async def _sequence_create(
        self, space_id: str, page_id: str, proposal: Proposal
    ) -> SequenceResult:
        if proposal.base_seq not in (None, 0) or not proposal.created_by:
            return SequenceResult(SequenceOutcome.REFUSED, "gone")
        now = _now()
        page = Page(
            id=page_id,
            title=proposal.title,
            content=proposal.content,
            created_by=proposal.created_by,
            created_at=now,
            updated_at=now,
            space_id=space_id,
            cover_image_url=proposal.cover_image_url,
            last_editor_user_id=proposal.actor_user_id or proposal.created_by,
            last_edited_at=now,
            seq=1,
        )
        if not await self._pages.save(page, space_id=space_id):
            return SequenceResult(SequenceOutcome.REFUSED, "gone")
        stored = await self._pages.get_space_page(page_id, space_id=space_id)
        return SequenceResult(
            SequenceOutcome.APPLIED, None, stored or page, created=True
        )

    async def _sequence_edit(
        self, space_id: str, page: Page, proposal: Proposal
    ) -> SequenceResult:
        state = _HostState(page=page, sides=await self.sides(space_id, page.id))
        before_sides = [s.hash for s in state.sides]
        cur_hash = await asyncio.to_thread(_page_hash, page)
        ph = await asyncio.to_thread(
            version_hash, proposal.title, proposal.content, proposal.cover_image_url
        )
        if proposal.resolves:
            keep = [s for s in state.sides if s.hash not in proposal.resolves]
            state.retired.extend(s for s in state.sides if s.hash in proposal.resolves)
            state.sides = keep
        side_hashes = {s.hash for s in state.sides}
        # A page the host never sequenced (seq 0: a pre-v_48 row) gets its
        # first canonical version even from a duplicate — and is broadcast.
        unsequenced = page.seq == 0
        if (
            (ph == cur_hash or ph in side_hashes)
            and not state.retired
            and not unsequenced
            and not (proposal.base_seq is not None and proposal.base_seq > page.seq)
        ):
            return SequenceResult(SequenceOutcome.DUPLICATE, None, page)
        claimed = proposal.base_seq if proposal.base_seq is not None else page.seq
        regressed = claimed > page.seq
        if regressed and claimed - page.seq > MAX_FLOOR_STEP:
            # Beyond any plausible restore: the claim moves nothing. The
            # edit is kept as a side one step up (its base is not ours).
            log.warning(
                "page %s in space %s: a proposal claims seq %s, %s past ours "
                "— beyond the floor step; kept as a side, seq not raised",
                page.id,
                space_id,
                claimed,
                claimed - page.seq,
            )
            proposal = replace(proposal, base_seq=page.seq, base_hash=None)
            beyond_step = True
        else:
            beyond_step = False
        if regressed and not beyond_step:
            # The member holds a newer seq than ours: we were restored from
            # a backup. seq must never regress — raise the floor above it
            # and commit (merged, or as a side), so every member takes it.
            log.warning(
                "page %s in space %s: a proposal is based on seq %s, ahead of "
                "ours (%s) — restored host; raising the floor",
                page.id,
                space_id,
                proposal.base_seq,
                page.seq,
            )
            page = replace(page, seq=min(int(proposal.base_seq or 0), MAX_SEQ - 1))
        if page.seq >= MAX_SEQ:
            return SequenceResult(SequenceOutcome.REFUSED, "bad_base", page)
        new_body: tuple[str, str, str | None] | None = None
        as_side = False
        absorbed = False
        if ph == cur_hash:
            new_body = None  # a resolution keeping the current body
        elif ph in side_hashes:
            # A resolution keeping a side: it simply becomes current.
            new_body = (proposal.title, proposal.content, proposal.cover_image_url)
            state.sides = [s for s in state.sides if s.hash != ph]
        elif proposal.base_seq is None or (
            proposal.base_seq == page.seq
            and (
                proposal.base_hash == cur_hash
                or (proposal.base_hash is None and not regressed)
            )
        ):
            new_body = (proposal.title, proposal.content, proposal.cover_image_url)
        else:
            base = (
                None
                if proposal.base_hash in side_hashes
                else await self._find_base(space_id, page, proposal, cur_hash)
            )
            merged = await self._merge(base, page, proposal) if base else None
            if merged is not None:
                new_body = merged
                # Absorbed: kept in history so the proposer's next draft
                # (made on top of what it sent) finds its base here.
                absorbed = True
            else:
                self._add_side(state, proposal, ph)
                as_side = True
        changed_body = new_body is not None and new_body != (
            page.title,
            page.content,
            page.cover_image_url,
        )
        changed_sides = [s.hash for s in state.sides] != before_sides
        if not changed_body and not changed_sides and not unsequenced and not regressed:
            return SequenceResult(SequenceOutcome.DUPLICATE, None, page)
        now = _now()
        history = [self._version_of(page, side, space_id) for side in state.retired]
        updated = replace(page, seq=page.seq + 1, updated_at=now)
        if changed_body:
            assert new_body is not None
            history.append(
                self._version_row(
                    page,
                    space_id,
                    title=page.title,
                    content=page.content,
                    cover_image_url=page.cover_image_url,
                    edited_by=_editor(page),
                )
            )
            if absorbed:
                history.append(
                    self._version_row(
                        page,
                        space_id,
                        title=proposal.title,
                        content=proposal.content,
                        cover_image_url=proposal.cover_image_url,
                        edited_by=proposal.actor_user_id,
                    )
                )
            title, content, cover = new_body
            updated = replace(
                updated,
                title=title,
                content=content,
                cover_image_url=cover,
                last_editor_user_id=proposal.actor_user_id or page.last_editor_user_id,
                last_edited_at=now,
            )
        # One transaction: history, the page row and its sides together;
        # the canonical version is published only after it commits.
        if not await self._pages.commit_version(
            updated, space_id=space_id, history=history, sides=state.sides
        ):
            return SequenceResult(SequenceOutcome.REFUSED, "gone", page)
        stored = await self._pages.get_space_page(page.id, space_id=space_id)
        outcome = SequenceOutcome.SIDE if as_side else SequenceOutcome.APPLIED
        return SequenceResult(
            outcome,
            None,
            stored or updated,
            created=unsequenced,
            sides=tuple(state.sides),
        )

    @staticmethod
    def _version_row(
        page: Page,
        space_id: str,
        *,
        title: str,
        content: str,
        cover_image_url: str | None,
        edited_by: str,
    ) -> PageVersion:
        """A history row (numbered by the repo inside the transaction)."""
        return PageVersion(
            id=uuid.uuid4().hex,
            page_id=page.id,
            version=0,
            title=title,
            content=content,
            edited_by=edited_by or page.created_by,
            edited_at=_now(),
            space_id=space_id,
            cover_image_url=cover_image_url,
        )

    def _version_of(
        self, page: Page, side: PageConflictSide, space_id: str
    ) -> PageVersion:
        return self._version_row(
            page,
            space_id,
            title=side.title,
            content=side.content,
            cover_image_url=side.cover_image_url,
            edited_by=side.by,
        )

    def _add_side(self, state: _HostState, proposal: Proposal, ph: str) -> None:
        """One side per user (a newer one retires the older), then the
        caps: the oldest sides move to history — never refused."""
        by = proposal.actor_user_id
        for old in [s for s in state.sides if s.by == by]:
            state.sides.remove(old)
            state.retired.append(old)
        when = datetime.now(timezone.utc)
        taken = {s.at for s in state.sides}
        stamp = when.isoformat(timespec="microseconds")
        while stamp in taken:
            when += timedelta(microseconds=1)
            stamp = when.isoformat(timespec="microseconds")
        state.sides.append(
            PageConflictSide(
                hash=ph,
                title=proposal.title,
                content=proposal.content,
                cover_image_url=proposal.cover_image_url,
                by=by,
                at=stamp,
                base_seq=proposal.base_seq or 0,
            )
        )

        def _bytes() -> int:
            return sum(
                len(s.content.encode("utf-8")) + len(s.title.encode("utf-8"))
                for s in state.sides
            )

        while len(state.sides) > 1 and (
            len(state.sides) > MAX_CONFLICT_SIDES or _bytes() > SIDES_MAX_BYTES
        ):
            state.retired.append(state.sides.pop(0))

    async def _find_base(
        self, space_id: str, page: Page, proposal: Proposal, cur_hash: str
    ) -> tuple[str, str, str | None] | None:
        """The version a proposal was made from: the current one, or one in
        the history (by hash). ``None``: unknown here — or one of the open
        sides (the proposer continues a version kept apart: it stays a
        side, replacing theirs)."""
        if proposal.base_hash is None:
            return None
        if proposal.base_hash == cur_hash:
            return (page.title, page.content, page.cover_image_url)
        for h, v in await self._history(space_id, page.id):
            if h == proposal.base_hash:
                return (v.title, v.content, v.cover_image_url)
        return None

    async def _merge(
        self, base: tuple[str, str, str | None], page: Page, proposal: Proposal
    ) -> tuple[str, str, str | None] | None:
        title = merge_scalar(base[0], page.title, proposal.title)
        cover = merge_scalar(base[2], page.cover_image_url, proposal.cover_image_url)
        if title is SCALAR_CONFLICT or cover is SCALAR_CONFLICT:
            return None
        result = await asyncio.to_thread(
            diff3_merge, base[1], page.content, proposal.content
        )
        if result.has_conflict:
            return None
        assert isinstance(title, str) or title is None
        assert isinstance(cover, str) or cover is None
        return (title or page.title, result.content, cover)

    async def refuse(
        self,
        *,
        space_id: str,
        page_id: str,
        proposal_hash: str,
        proposer_instance: str,
        reason: str,
    ) -> None:
        """Tell ``proposer_instance`` its proposal was refused (``reason``) —
        at most once per (sender, page) per minute, so a flood of bad
        proposals cannot turn the host into an amplifier, and at most
        :data:`REFUSALS_PER_SENDER_PER_MINUTE` per (sender, space).
        ``rate_limited`` and ``archived`` carry no page state."""
        if not self._limiter.is_allowed(
            f"page-refusals:{proposer_instance}:{space_id}:{page_id}",
            limit=1,
            window_s=60,
        ) or not self._limiter.is_allowed(
            f"page-refusals:{proposer_instance}:{space_id}",
            limit=REFUSALS_PER_SENDER_PER_MINUTE,
            window_s=60,
        ):
            return
        page = (
            None
            if reason in _STATELESS_REFUSALS
            else await self._pages.get_space_page(page_id, space_id=space_id)
        )
        await self._ack(
            space_id=space_id,
            page_id=page_id,
            page=page,
            sequenced=Sequenced(
                proposer_instance=proposer_instance,
                proposal_hash=proposal_hash,
                outcome="refused",
                reason=reason,
            ),
            proposer_instance=proposer_instance,
        )

    async def on_archived_write(self, event: Any, space: Any) -> None:
        """The archived-space gate refused a write. A member's page proposal
        to us (the host) is answered ``refused/archived`` — without page
        state — so the member stops waiting for it. The CALLER has checked
        that the sender holds a live writer seat (anyone else hears
        nothing: no oracle)."""
        if space is None or space.owner_instance_id != self._own_instance_id:
            return
        if event.event_type not in (
            FederationEventType.SPACE_PAGE_CREATED,
            FederationEventType.SPACE_PAGE_UPDATED,
        ):
            return
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if "base_seq" not in payload or "sequenced" in payload:
            return
        proposal = proposal_from_wire(payload)
        page_id = str(payload.get("id") or payload.get("page_id") or "")
        if proposal is None or not page_id:
            return
        await self.refuse(
            space_id=str(space.id),
            page_id=page_id,
            proposal_hash=proposal.hash,
            proposer_instance=str(event.from_instance or ""),
            reason="archived",
        )

    async def _ack(
        self,
        *,
        space_id: str,
        page_id: str,
        page: Page | None,
        sequenced: Sequenced,
        proposer_instance: str,
    ) -> None:
        """A targeted answer to one proposer: the current state + seq."""
        fed = self._federation
        if fed is None or not proposer_instance:
            return
        if not await fed.peer_supports(
            proposer_instance,
            min_version=FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES,
        ):
            return
        payload: dict[str, Any] = {
            "id": page_id,
            "page_id": page_id,
            "space_id": space_id,
        }
        if page is not None:
            sides = await self.sides(space_id, page_id)
            payload.update(
                title=page.title,
                content=page.content,
                created_by=page.created_by,
                actor_user_id=page.last_editor_user_id or page.created_by,
                **canonical_extras(page, sides, sequenced),
            )
        else:
            payload.update(seq=0, conflict=[], sequenced=sequenced.to_wire())
        try:
            await fed.send_with_mesh_fallback(
                to_instance_id=proposer_instance,
                event_type=FederationEventType.SPACE_PAGE_UPDATED,
                payload=payload,
                space_id=space_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("page ack to %s failed: %s", proposer_instance, exc)

    async def _broadcast(
        self,
        page: Page,
        *,
        space_id: str,
        actor: str,
        sequenced: Sequenced | None,
        created: bool,
        sides: Sequence[PageConflictSide] = (),
    ) -> None:
        """Publish the canonical version — the outbound broadcasts it to
        every member household (it doubles as the proposer's ack).
        ``sides`` are the ones committed with it."""
        extras = canonical_extras(page, sides, sequenced)
        if created:
            await self._emit(
                PageCreated(
                    page_id=page.id,
                    space_id=space_id,
                    title=page.title,
                    content=page.content,
                    actor_user_id=actor or page.created_by,
                    canonical={**extras, "created_by": page.created_by},
                )
            )
            return
        await self._emit(
            PageUpdated(
                page_id=page.id,
                space_id=space_id,
                title=page.title,
                content=page.content,
                actor_user_id=actor,
                canonical=extras,
            )
        )

    async def raise_floor(self, space_id: str, page_id: str, seq: int) -> bool:
        """The host learns a member household mirrors ``page_id`` at
        ``seq`` (its §25.6 sync record — never its content). A ``seq`` ahead
        of ours means we were restored from a backup (or took over from
        another host): raise our floor to it, so our next commit lands
        above every version members hold and they take it. Only ever
        raised, by at most :data:`MAX_FLOOR_STEP` per signal and never to
        :data:`MAX_SEQ`; a live page held here only. ``True`` when it
        moved. The caller has checked that this household hosts the space.
        """
        if seq <= 0 or seq > MAX_SEQ:
            return False
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None or seq <= page.seq:
                return False
            if seq - page.seq > MAX_FLOOR_STEP:
                log.warning(
                    "page %s in space %s: a member reports seq %s, %s past ours "
                    "— beyond the floor step; not raised",
                    page_id,
                    space_id,
                    seq,
                    seq - page.seq,
                )
                return False
            floor = min(seq, MAX_SEQ - 1)
            raised = await self._pages.raise_seq(page_id, space_id=space_id, seq=floor)
        if raised:
            log.warning(
                "page %s in space %s: a member mirrors seq %s, ahead of ours "
                "(%s) — restored host; floor raised",
                page_id,
                space_id,
                floor,
                page.seq,
            )
        return raised

    async def delete_page(
        self, space_id: str, page_id: str, *, deleted_by: str = ""
    ) -> bool:
        """Tombstone a space page (migration 0073) under its lock, so no
        sequencing or mirroring step interleaves with the delete. ``False``
        when no live page of ``space_id`` was there."""
        async with self.lock_for(space_id, page_id):
            return await self._pages.delete(
                page_id, space_id=space_id, deleted_by=deleted_by
            )

    async def adopt_draft(self, space_id: str, page_id: str) -> bool:
        """This household hosts the space now, yet still holds a draft of
        its own made under the previous host: commit it as host and clear
        ``pending_base_seq``. The draft is sequenced like a proposal on the
        base it was made from, against the newest version mirrored since
        (``seq`` stays above every version we saw), so it fast-forwards,
        merges, or is kept as a side — never lost. ``True`` when a draft
        was adopted."""
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None or page.pending_base_seq is None:
                return False
            base = await self._pages.get_draft_base(page_id, space_id=space_id)
            proposal = Proposal(
                title=page.title,
                content=page.content,
                cover_image_url=page.cover_image_url,
                actor_user_id=page.last_editor_user_id or page.created_by,
            )
            await self._pages.clear_draft_base(page_id, space_id=space_id)
            if page.pending_base_seq == 0 or base is None or not base.title:
                # Our own create, never sequenced: its first version.
                first = replace(
                    page, seq=min(page.seq + 1, MAX_SEQ), pending_base_seq=None
                )
                await self._pages.save(first, space_id=space_id)
                stored = await self._pages.get_space_page(page_id, space_id=space_id)
                result = SequenceResult(
                    SequenceOutcome.APPLIED, None, stored or first, created=True
                )
            else:
                current = await self._canonical_under_draft(space_id, page, base)
                # The draft's base is an ancestor of the current version:
                # keep it in history, where the merge looks bases up.
                await self._remember(
                    page,
                    title=base.title,
                    content=base.content,
                    cover_image_url=base.cover_image_url,
                    edited_by=base.by,
                    space_id=space_id,
                )
                proposal = replace(
                    proposal,
                    base_seq=page.pending_base_seq,
                    base_hash=version_hash(
                        base.title, base.content, base.cover_image_url
                    ),
                    resolves=base.resolves,
                )
                result = await self._sequence_edit(space_id, current, proposal)
                if result.outcome not in (
                    SequenceOutcome.APPLIED,
                    SequenceOutcome.SIDE,
                ):
                    # Nothing to commit (the draft was the current version
                    # already): just drop the draft.
                    await self._pages.save(current, space_id=space_id)
        log.info(
            "page %s in space %s: this household hosts the space now — our "
            "draft is sequenced here (%s)",
            page_id,
            space_id,
            result.outcome,
        )
        await self._publish_result(
            result,
            space_id=space_id,
            page_id=page_id,
            proposal=proposal,
            proposer_instance="",
        )
        return True

    async def _canonical_under_draft(
        self, space_id: str, page: Page, base: DraftBase
    ) -> Page:
        """The canonical version a draft row sits on: the draft's base, or —
        when a newer host version was mirrored under the draft — that
        version (the newest history row: a member writes history only when
        it mirrors a host version)."""
        title, content, cover = base.title, base.content, base.cover_image_url
        editor = base.by
        if page.seq > base.seq:
            versions = await self._pages.list_versions(page.id, space_id=space_id)
            newest = max(versions, key=lambda v: v.version, default=None)
            if newest is not None:
                title, content, cover = (
                    newest.title,
                    newest.content,
                    newest.cover_image_url,
                )
                editor = newest.edited_by
        return replace(
            page,
            title=title,
            content=content,
            cover_image_url=cover,
            last_editor_user_id=editor or page.last_editor_user_id,
            pending_base_seq=None,
        )

    async def host_create(self, page: Page, *, actor_user_id: str) -> Page:
        """A page created here, on its host: sequenced as ``seq`` 1."""
        async with self.lock_for(page.space_id or "", page.id):
            first = replace(page, seq=1, pending_base_seq=None)
            await self._pages.save(first, space_id=page.space_id)
            stored = await self._pages.get_space_page(
                page.id, space_id=page.space_id or ""
            )
        result = stored or first
        await self._broadcast(
            result,
            space_id=page.space_id or "",
            actor=actor_user_id,
            sequenced=None,
            created=True,
        )
        return result

    # ─── Member: a local draft ────────────────────────────────────────────

    async def member_draft(
        self,
        page: Page,
        updated: Page,
        *,
        space_id: str,
        actor_user_id: str,
        resolves: Sequence[str] = (),
    ) -> Page:
        """Store a local edit as an optimistic draft on top of the host's
        version (call under :meth:`lock_for`). The draft's base stays the
        canonical version it was first made from until the host answers."""
        open_now = (
            {s.hash for s in await self.sides(space_id, page.id)} if resolves else set()
        )

        def _capped(*groups: Sequence[str]) -> tuple[str, ...]:
            # Only sides still open, at most what one proposal may carry.
            merged = dict.fromkeys(r for g in groups for r in g if r in open_now)
            return tuple(merged)[:MAX_RESOLVES]

        if page.pending_base_seq is None:
            await self._pages.set_draft_base(
                page.id,
                space_id=space_id,
                base=DraftBase(
                    title=page.title,
                    content=page.content,
                    cover_image_url=page.cover_image_url,
                    seq=page.seq,
                    by=_editor(page),
                    resolves=_capped(resolves),
                ),
            )
        elif resolves:
            base = await self._pages.get_draft_base(page.id, space_id=space_id)
            if base is not None:
                await self._pages.set_draft_base(
                    page.id,
                    space_id=space_id,
                    base=replace(base, resolves=_capped(resolves, base.resolves)),
                )
        draft = replace(
            updated,
            seq=page.seq,
            pending_base_seq=(
                page.pending_base_seq if page.pending_base_seq is not None else page.seq
            ),
        )
        await self._pages.save(draft, space_id=space_id)
        stored = await self._pages.get_space_page(page.id, space_id=space_id)
        return stored or draft

    async def member_create(self, page: Page) -> Page:
        """A page created here under a v_48 host: a draft (``seq`` 0,
        ``pending_base_seq`` 0) until the host sequences it."""
        draft = replace(page, seq=0, pending_base_seq=0)
        await self._pages.save(draft, space_id=page.space_id)
        await self._pages.set_draft_base(
            page.id,
            space_id=page.space_id or "",
            base=DraftBase(title="", content="", seq=0, by=page.created_by),
        )
        stored = await self._pages.get_space_page(page.id, space_id=page.space_id or "")
        return stored or draft

    async def proposal_for(self, space_id: str, page_id: str) -> Proposal | None:
        """The proposal a member's pending draft stands for (``None``: no
        draft)."""
        page = await self._pages.get_space_page(page_id, space_id=space_id)
        if page is None or page.pending_base_seq is None:
            return None
        base = await self._pages.get_draft_base(page_id, space_id=space_id)
        base_hash = None
        resolves: tuple[str, ...] = ()
        if base is not None:
            resolves = base.resolves
            if page.pending_base_seq > 0:
                base_hash = version_hash(base.title, base.content, base.cover_image_url)
        return Proposal(
            title=page.title,
            content=page.content,
            cover_image_url=page.cover_image_url,
            actor_user_id=page.last_editor_user_id or page.created_by,
            base_seq=page.pending_base_seq,
            base_hash=base_hash,
            resolves=resolves,
            created_by=page.created_by if page.pending_base_seq == 0 else "",
        )

    async def rebase_draft(
        self, space_id: str, page_id: str, *, sent: Proposal, seq: int
    ) -> None:
        """The host sequenced an earlier proposal of ours (``sent``) at
        ``seq`` while we kept editing: the newer draft continues from what we
        sent, so that becomes its base — not the original one, against which
        our own two edits would look like a conflict."""
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None or page.pending_base_seq is None:
                return
            if seq < (page.pending_base_seq or 0):
                return
            base = await self._pages.get_draft_base(page_id, space_id=space_id)
            await self._pages.set_draft_base(
                page_id,
                space_id=space_id,
                base=DraftBase(
                    title=sent.title,
                    content=sent.content,
                    cover_image_url=sent.cover_image_url,
                    seq=seq,
                    by=sent.actor_user_id,
                    resolves=base.resolves if base is not None else (),
                ),
            )
            await self._pages.save(
                replace(page, pending_base_seq=seq), space_id=space_id
            )

    # ─── Member: mirror the host ──────────────────────────────────────────

    async def mirror(
        self,
        *,
        space_id: str,
        page_id: str,
        version: CanonicalVersion,
    ) -> bool:
        """Apply the host's ``version`` of a page by ``seq``: newer →
        stored (over a draft only when it answers that draft); same →
        settles our draft if it answers it; older → ignored. ``True`` when
        anything changed here. The answer to one of our proposals is
        published after the page lock is released."""
        settled: PageProposalSettled | None = None
        changed = False
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None:
                return await self._mirror_create(space_id, page_id, version)
            draft_hash = (
                await asyncio.to_thread(_page_hash, page)
                if page.pending_base_seq is not None
                else None
            )
            seq_ = version.sequenced
            ours = seq_ is not None and seq_.proposer_instance == self._own_instance_id
            answers_draft = (
                ours and seq_ is not None and seq_.proposal_hash == draft_hash
            )
            if answers_draft and seq_ is not None and seq_.outcome == "refused":
                settled = await self._settle_refusal(space_id, page, version, seq_)
                changed = True
            elif version.seq > page.seq or (answers_draft and version.seq >= page.seq):
                # A newer version settles our draft when it answers it — or,
                # whatever order versions arrive in, when it already holds
                # the draft: as its body, or as one of its sides.
                held = draft_hash is not None and await self._holds_sent_draft(
                    space_id, page, draft_hash, version
                )
                settles = answers_draft or held
                await self._mirror_apply(
                    space_id,
                    page,
                    version,
                    keep_draft=draft_hash is not None and not settles,
                )
                changed = True
                if settles and draft_hash is not None:
                    settled = PageProposalSettled(
                        page_id=page_id,
                        space_id=space_id,
                        proposal_hash=draft_hash,
                        outcome="applied",
                        seq=version.seq,
                    )
                elif ours and seq_ is not None:
                    # An earlier proposal's answer (we kept editing): the
                    # forwarder rebases the newer draft on what it sent and
                    # proposes it.
                    settled = PageProposalSettled(
                        page_id=page_id,
                        space_id=space_id,
                        proposal_hash=seq_.proposal_hash,
                        outcome=seq_.outcome,
                        reason=seq_.reason,
                        seq=version.seq,
                    )
            elif answers_draft and seq_ is not None:
                # Our draft's answer arriving AFTER a newer version (out of
                # order): the draft is sequenced, and the newer version —
                # already here, kept in history under the draft — is current.
                await self._settle_late(space_id, page)
                changed = True
                settled = PageProposalSettled(
                    page_id=page_id,
                    space_id=space_id,
                    proposal_hash=seq_.proposal_hash,
                    outcome="applied",
                    seq=version.seq,
                )
            elif ours and seq_ is not None:
                # An answer about an older proposal, nothing new to apply.
                settled = PageProposalSettled(
                    page_id=page_id,
                    space_id=space_id,
                    proposal_hash=seq_.proposal_hash,
                    outcome=seq_.outcome,
                    reason=seq_.reason,
                    seq=version.seq,
                )
        if settled is not None:
            await self._emit(settled)
        return changed

    async def _holds_sent_draft(
        self, space_id: str, page: Page, draft_hash: str, version: CanonicalVersion
    ) -> bool:
        """May ``version`` settle our draft by its content, without the
        host's answer to it? Only a draft that was SENT, unchanged since,
        and that resolves nothing (a resolution hashes like the version it
        keeps — its ``resolves`` are not in the hash), and then only when
        the version's BODY is the draft, or a side of it is ours (our
        actor, our base). Anything else stays pending and goes to the host,
        which acknowledges a duplicate and applies ``resolves``."""
        base = await self._pages.get_draft_base(page.id, space_id=space_id)
        if base is None or base.sent != draft_hash or base.resolves:
            return False
        body = await asyncio.to_thread(
            version_hash, version.title, version.content, version.cover_image_url
        )
        if body == draft_hash:
            return True
        actor = page.last_editor_user_id or page.created_by
        return any(
            s.hash == draft_hash
            and s.by == actor
            and s.base_seq == page.pending_base_seq
            for s in version.conflict
        )

    async def mark_sent(self, space_id: str, page_id: str, proposal_hash: str) -> None:
        """The forwarder sent ``proposal_hash``: remember it on the draft's
        base (it survives a restart) — if the draft is still that one."""
        async with self.lock_for(space_id, page_id):
            page = await self._pages.get_space_page(page_id, space_id=space_id)
            if page is None or page.pending_base_seq is None:
                return
            if await asyncio.to_thread(_page_hash, page) != proposal_hash:
                return
            base = await self._pages.get_draft_base(page_id, space_id=space_id)
            if base is not None and base.sent != proposal_hash:
                await self._pages.set_draft_base(
                    page_id, space_id=space_id, base=replace(base, sent=proposal_hash)
                )

    async def _settle_late(self, space_id: str, page: Page) -> None:
        """Drop a draft the host already sequenced, showing the newest host
        version we mirrored while it was pending (the newest history row:
        a member writes history only when it mirrors a host version)."""
        versions = await self._pages.list_versions(page.id, space_id=space_id)
        newest = max(versions, key=lambda v: v.version, default=None)
        settled = replace(page, pending_base_seq=None)
        if newest is not None:
            settled = replace(
                settled,
                title=newest.title,
                content=newest.content,
                cover_image_url=newest.cover_image_url,
                last_editor_user_id=newest.edited_by or page.last_editor_user_id,
            )
        await self._pages.save(settled, space_id=space_id)
        await self._pages.clear_draft_base(page.id, space_id=space_id)

    async def _mirror_create(
        self, space_id: str, page_id: str, version: CanonicalVersion
    ) -> bool:
        if not version.has_state or not version.created_by:
            return False
        now = _now()
        page = Page(
            id=page_id,
            title=version.title,
            content=version.content,
            created_by=version.created_by,
            created_at=version.created_at or now,
            updated_at=version.updated_at or now,
            space_id=space_id,
            cover_image_url=version.cover_image_url,
            last_editor_user_id=version.last_editor_user_id or version.created_by,
            last_edited_at=version.updated_at or now,
            seq=version.seq,
        )
        if not await self._pages.save(page, space_id=space_id):
            return False
        await self._pages.set_conflict_sides(
            page_id, space_id=space_id, sides=version.conflict
        )
        return True

    async def _mirror_apply(
        self, space_id: str, page: Page, version: CanonicalVersion, *, keep_draft: bool
    ) -> None:
        if not version.has_state:
            return
        before = {s.hash for s in await self.sides(space_id, page.id)}
        if keep_draft:
            # The draft stays on top; the host's version is kept in history.
            await self._remember(
                page,
                title=version.title,
                content=version.content,
                cover_image_url=version.cover_image_url,
                edited_by=version.last_editor_user_id,
                space_id=space_id,
            )
            updated = replace(page, seq=version.seq)
        else:
            if page.pending_base_seq is None and (
                page.title,
                page.content,
                page.cover_image_url,
            ) != (version.title, version.content, version.cover_image_url):
                await self._remember_page(page, space_id)
            updated = replace(
                page,
                title=version.title,
                content=version.content,
                cover_image_url=version.cover_image_url,
                updated_at=version.updated_at or _now(),
                last_editor_user_id=version.last_editor_user_id
                or page.last_editor_user_id,
                last_edited_at=version.updated_at or _now(),
                seq=version.seq,
                pending_base_seq=None,
            )
            await self._pages.clear_draft_base(page.id, space_id=space_id)
        await self._pages.save(updated, space_id=space_id)
        await self._pages.set_conflict_sides(
            page.id, space_id=space_id, sides=version.conflict
        )
        added = [s for s in version.conflict if s.hash not in before]
        if added:
            await self._emit(
                PageConflictEmitted(
                    page_id=page.id,
                    space_id=space_id,
                    theirs=added[-1].content,
                    theirs_by=added[-1].by,
                    federated=True,
                )
            )

    async def _settle_refusal(
        self, space_id: str, page: Page, version: CanonicalVersion, seq_: Sequenced
    ) -> PageProposalSettled:
        """The host refused our draft: ``rate_limited`` and ``bad_base``
        keep it for a retry (a restored host raises its seq floor instead
        of refusing, so the draft is never lost to its regression), ``gone``
        keeps the words (to save as a new page) but stops proposing them,
        anything else restores the host's version (or, without state, our
        own draft base)."""
        reason = seq_.reason or "access"
        if reason == "gone":
            await self._pages.save(
                replace(page, pending_base_seq=None), space_id=space_id
            )
            await self._pages.clear_draft_base(page.id, space_id=space_id)
        elif reason not in ("rate_limited", "bad_base"):
            if version.has_state and version.seq >= page.seq:
                await self._mirror_apply(space_id, page, version, keep_draft=False)
            else:
                base = await self._pages.get_draft_base(page.id, space_id=space_id)
                restored = replace(page, pending_base_seq=None)
                if base is not None and base.title:
                    restored = replace(
                        restored,
                        title=base.title,
                        content=base.content,
                        cover_image_url=base.cover_image_url,
                    )
                await self._pages.save(restored, space_id=space_id)
                await self._pages.clear_draft_base(page.id, space_id=space_id)
        log.info(
            "page %s in space %s: the host refused our edit (%s)",
            page.id,
            space_id,
            reason,
        )
        return PageProposalSettled(
            page_id=page.id,
            space_id=space_id,
            proposal_hash=seq_.proposal_hash,
            outcome="refused",
            reason=reason,
            seq=version.seq,
        )

    # ─── Legacy (a host below v_48) ───────────────────────────────────────

    async def legacy_apply(self, page: Page, incoming: Page, *, space_id: str) -> None:
        """Last write wins, the replaced body kept in history."""
        async with self.lock_for(space_id, page.id):
            if (page.title, page.content, page.cover_image_url) != (
                incoming.title,
                incoming.content,
                incoming.cover_image_url,
            ):
                await self._remember_page(page, space_id)
            await self._pages.save(
                replace(
                    page,
                    title=incoming.title,
                    content=incoming.content,
                    cover_image_url=incoming.cover_image_url,
                    updated_at=incoming.updated_at or _now(),
                    last_editor_user_id=incoming.last_editor_user_id
                    or page.last_editor_user_id,
                ),
                space_id=space_id,
            )

    # ─── Resolve ──────────────────────────────────────────────────────────

    async def resolution_body(
        self,
        page: Page,
        open_sides: list[PageConflictSide],
        *,
        resolution: str,
        side: str | None,
        merged_content: str | None,
    ) -> tuple[str, str, str | None]:
        """The body a resolution keeps (raises :class:`PageConflictStaleError`
        for a side that is no longer open, :class:`ValueError` for a
        missing merged body)."""
        current = await asyncio.to_thread(_page_hash, page)
        match resolution:
            case "side":
                pick = next((s for s in open_sides if s.hash == side), None)
                if pick is None:
                    if side == current:
                        return page.title, page.content, page.cover_image_url
                    raise PageConflictStaleError([s.hash for s in open_sides])
                return pick.title, pick.content, pick.cover_image_url
            case "mine":
                return page.title, page.content, page.cover_image_url
            case "theirs":
                others = [s for s in open_sides if s.hash != current]
                if not others:
                    return page.title, page.content, page.cover_image_url
                newest = max(others, key=lambda s: s.at)
                return newest.title, newest.content, newest.cover_image_url
        if not merged_content:
            raise ValueError("merged_content required for 'merged_content'")
        return page.title, merged_content, page.cover_image_url

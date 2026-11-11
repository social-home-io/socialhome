"""Which files of a space row this household ships, and how many (v_86, v_92)."""

from __future__ import annotations

from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.space import SpaceMember
from socialhome.federation.sync.space import file_gate as gate_mod
from socialhome.federation.sync.space.file_gate import (
    BUDGET_MAX_BYTES,
    BUDGET_MAX_FILES,
    BUDGET_WINDOW_S,
    CatchUpFileGates,
    SpaceFileBudget,
    local_file_size,
)

SP = "sp-g"
SC = f"api/media/{space_content_media_name(SP, 'a' * 64, 'x.webp')}"
LEGACY = "api/media/sd-0123456789abcdef.webp"


class _Spaces:
    def __init__(self, members: list[SpaceMember], local: list[str]) -> None:
        self._members = members
        self._local = local

    async def list_members(self, space_id: str) -> list[SpaceMember]:
        return list(self._members) if space_id == SP else []

    async def list_local_member_user_ids(self, space_id: str) -> list[str]:
        return list(self._local) if space_id == SP else []


def _member(user_id: str, role: str) -> SpaceMember:
    return SpaceMember(space_id=SP, user_id=user_id, role=role, joined_at="")


def _gates(*, host: bool = False, role: str = "member") -> CatchUpFileGates:
    async def _authority(space_id: str) -> bool:
        return host

    return CatchUpFileGates(
        space_repo=_Spaces([_member("u-me", role), _member("u-x", "member")], ["u-me"]),
        roster_authority=_authority,
    )


async def test_the_host_ships_every_file_of_every_row():
    for fills in (False, True):
        gate = await _gates(host=True).for_space(SP, fills=fills)
        assert gate.ship("u-x", [SC, LEGACY]) == [SC, LEGACY]
        assert gate.ship(None, [LEGACY]) == [LEGACY]


async def test_a_catch_up_ships_only_our_own_peoples_files():
    """A full catch-up (no fills) is the host's job for other people's
    files: even a moderator household ships only its own people's."""
    for role in ("member", "moderator", "admin"):
        gate = await _gates(role=role).for_space(SP)
        assert gate.ship("u-me", [SC, LEGACY]) == [SC, LEGACY]
        assert gate.ship("u-x", [SC, LEGACY]) == []
        assert gate.ship(None, [SC]) == []


async def test_a_trusted_household_fills_other_peoples_files_on_request():
    """``fills`` (a SPACE_MEDIA_WANTED match that proved the bytes' content
    name) lets an admin / moderator household ship another person's file —
    a plain member household never."""
    for role in ("moderator", "admin"):
        gate = await _gates(role=role).for_space(SP, fills=True)
        assert gate.ship("u-x", [LEGACY]) == [LEGACY]
    plain = await _gates(role="member").for_space(SP, fills=True)
    assert plain.ship("u-x", [SC]) == []
    assert plain.ship("u-me", [SC]) == [SC]


async def test_local_file_size_reads_the_media_dir_only(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"12345")
    assert await local_file_size(tmp_path, "api/media/a.webp") == 5
    assert await local_file_size(tmp_path, "api/media/missing.webp") is None
    assert await local_file_size(tmp_path, "api/media/..") is None


# ── One budget per (requester, space) ────────────────────────────────────


def test_the_budget_is_50_files_and_32_mib_per_requester_and_space_window():
    now = [1000.0]
    budget = SpaceFileBudget(clock=lambda: now[0])
    assert (BUDGET_MAX_FILES, BUDGET_MAX_BYTES, BUDGET_WINDOW_S) == (
        50,
        32 * 1024 * 1024,
        30 * 60.0,
    )
    assert not budget.exhausted("req", SP)
    assert all(budget.take("req", SP, 1) for _ in range(BUDGET_MAX_FILES))
    assert not budget.take("req", SP, 1)
    assert budget.exhausted("req", SP)
    # Another requester or space has its own window.
    assert budget.take("other", SP, 1)
    assert budget.take("req", "sp-other", 1)
    now[0] += BUDGET_WINDOW_S
    assert not budget.exhausted("req", SP)
    assert budget.take("req", SP, 1)


def test_a_file_that_would_pass_the_bytes_is_refused_without_a_charge():
    budget = SpaceFileBudget(clock=lambda: 0.0)
    assert budget.take("req", SP, BUDGET_MAX_BYTES - 10)
    assert not budget.take("req", SP, 11)
    assert budget.take("req", SP, 10)
    assert budget.exhausted("req", SP)


def test_the_budget_remembers_a_bounded_number_of_windows(monkeypatch):
    monkeypatch.setattr(gate_mod, "BUDGET_TRACKED", 2)
    monkeypatch.setattr(gate_mod, "BUDGET_MAX_FILES", 1)
    budget = SpaceFileBudget(clock=lambda: 0.0)
    for requester in ("a", "b", "c"):
        assert budget.take(requester, SP, 1)
    # "a" was dropped (oldest): a fresh window.
    assert not budget.exhausted("a", SP)
    assert budget.exhausted("c", SP)


async def test_own_only_ships_this_households_people_even_for_the_authority():
    """``own_only`` (sender-bound files): only our own people's rows, also
    when we hold the roster authority (which ships every file otherwise)."""

    class _Spaces:
        async def list_local_member_user_ids(self, space_id):
            return ["u-me"]

        async def list_members(self, space_id):
            return []

    async def _authority(space_id):
        return True

    gate = await CatchUpFileGates(
        space_repo=_Spaces(), roster_authority=_authority
    ).for_space("sp", fills=True)
    assert gate.ship("u-other", ["a"]) == ["a"]
    assert gate.own_only("u-other", ["a"]) == []
    assert gate.own_only("u-me", ["a"]) == ["a"]

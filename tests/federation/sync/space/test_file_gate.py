"""v_86 — which files of a space row this household ships on catch-up."""

from __future__ import annotations

import pytest

from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.space import SpaceMember
from socialhome.federation.sync.space import file_gate as gate_mod
from socialhome.federation.sync.space.file_gate import CatchUpFileGates, local_file_size

SP = "sp-g"
SC = f"api/media/{space_content_media_name(SP, 'a' * 64, 'x.webp')}"
SC2 = f"api/media/{space_content_media_name(SP, 'b' * 64, 'y.webp')}"
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


def _gates(
    *,
    host: bool = False,
    role: str = "member",
    version: int = FederationCapability.MIN_FOR_TRUSTED_FILE_FILLS,
    sizes: dict[str, int] | None = None,
) -> CatchUpFileGates:
    async def _authority(space_id: str) -> bool:
        return host

    async def _supports(instance_id: str, *, min_version: int) -> bool:
        return version >= min_version

    async def _size(url: str) -> int | None:
        return (sizes or {}).get(url, 1)

    return CatchUpFileGates(
        space_repo=_Spaces([_member("u-me", role), _member("u-x", "member")], ["u-me"]),
        roster_authority=_authority,
        supports=_supports,
        size=_size,
    )


async def test_the_host_ships_every_file_of_every_row():
    gate = await _gates(host=True).for_requester(SP, "peer-new")
    assert await gate.ship("u-x", [SC, LEGACY]) == [SC, LEGACY]
    assert await gate.ship(None, [LEGACY]) == [LEGACY]


@pytest.mark.parametrize("role", ["member", "moderator", "admin"])
async def test_our_own_peoples_files_always_ship(role):
    gate = await _gates(role=role).for_requester(SP, "peer-new")
    assert await gate.ship("u-me", [SC, LEGACY]) == [SC, LEGACY]


@pytest.mark.parametrize("role", ["moderator", "admin"])
async def test_a_trusted_household_ships_only_others_hashed_files(role):
    gate = await _gates(role=role).for_requester(SP, "peer-new")
    assert await gate.ship("u-x", [SC, LEGACY]) == [SC]
    assert await gate.ship(None, [SC2]) == [SC2]


async def test_a_plain_member_household_ships_no_one_elses_file():
    gate = await _gates(role="member").for_requester(SP, "peer-new")
    assert await gate.ship("u-x", [SC, LEGACY]) == []
    assert await gate.ship(None, [SC]) == []


async def test_a_requester_below_v86_gets_no_helper_fills():
    gate = await _gates(
        role="moderator",
        version=FederationCapability.MIN_FOR_TRUSTED_FILE_FILLS - 1,
    ).for_requester(SP, "peer-old")
    assert await gate.ship("u-x", [SC]) == []
    assert await gate.ship("u-me", [SC]) == [SC]


async def test_helper_fills_are_bounded_per_stream(monkeypatch):
    monkeypatch.setattr(gate_mod, "FILL_MAX_FILES", 1)
    gate = await _gates(role="moderator").for_requester(SP, "peer-new")
    assert await gate.ship("u-x", [SC, SC2]) == [SC]
    assert await gate.ship("u-x", [SC2]) == []
    # Our own people's files are not part of the helper budget.
    assert await gate.ship("u-me", [SC2]) == [SC2]


async def test_helper_fills_respect_the_byte_budget_and_skip_missing(monkeypatch):
    monkeypatch.setattr(gate_mod, "FILL_MAX_BYTES", 10)
    sizes = {SC: 8, SC2: 8}
    gate = await _gates(role="moderator", sizes=sizes).for_requester(SP, "peer-new")
    assert await gate.ship("u-x", [SC, SC2]) == [SC]
    missing = await _gates(role="moderator", sizes={SC: None}).for_requester(  # type: ignore[dict-item]
        SP, "peer-new"
    )
    assert await missing.ship("u-x", [SC]) == []


async def test_local_file_size_reads_the_media_dir_only(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"12345")
    assert await local_file_size(tmp_path, "api/media/a.webp") == 5
    assert await local_file_size(tmp_path, "api/media/missing.webp") is None
    assert await local_file_size(tmp_path, "api/media/..") is None

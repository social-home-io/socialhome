"""A global space's place pin in ``SpaceService`` — create, settings, tier
changes (owner decision 2026-10-19: for sorting only; never the home)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from socialhome.domain.space import (
    LocalAreaUnavailableError,
    PlacePinGlobalOnlyError,
    SpacePermissionError,
    SpaceType,
)
from socialhome.domain.space_place import PlacePin

from . import test_space_service_local_community as _base

#: The service stack fixture, shared with the local-community suite.
stack = _base.stack


async def _global(s, **kw):
    return await s.svc.create_space(
        owner_username="anna", name="Chess Zürich", space_type=SpaceType.GLOBAL, **kw
    )


async def test_create_stores_the_pin_at_4dp_with_no_radius(stack):
    space = await _global(stack, place={"lat": 47.376_912_3, "lon": 8.541_749_9})
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (47.3769, 8.5417, None)


async def test_create_without_a_pin_has_none(stack):
    """Never defaulted — not even to this household's home."""
    space = await _global(stack)
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon) == (None, None)


@pytest.mark.parametrize("tier", [SpaceType.PUBLIC, SpaceType.PRIVATE])
async def test_only_a_global_space_takes_a_pin(stack, tier):
    with pytest.raises(PlacePinGlobalOnlyError) as exc:
        await stack.svc.create_space(
            owner_username="anna", name="X", space_type=tier, place={"lat": 1, "lon": 2}
        )
    assert exc.value.status == 422


async def test_a_malformed_pin_is_a_value_error(stack):
    with pytest.raises(ValueError):
        await _global(stack, place={"lat": 100, "lon": 2})


async def test_set_moves_and_clears_and_republishes(stack):
    gfs = AsyncMock()
    stack.svc._gfs = gfs
    space = await _global(stack)
    gfs.reset_mock()
    pin = await stack.svc.set_place_pin(
        space.id, actor_username="anna", place={"lat": 46.9481, "lon": 7.4474}
    )
    assert pin == PlacePin(46.9481, 7.4474)
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (46.9481, 7.4474, None)
    gfs.republish_space.assert_awaited_once_with(space.id)
    assert (
        await stack.svc.set_place_pin(space.id, actor_username="anna", place=None)
        is None
    )
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon) == (None, None)


async def test_set_needs_an_admin(stack):
    space = await _global(stack)
    with pytest.raises(SpacePermissionError):
        await stack.svc.set_place_pin(
            space.id, actor_username="ben", place={"lat": 1, "lon": 2}
        )


async def test_set_on_a_public_space_is_refused(stack):
    space = await stack.svc.create_space(
        owner_username="anna", name="Q", space_type=SpaceType.PUBLIC
    )
    with pytest.raises(PlacePinGlobalOnlyError):
        await stack.svc.set_place_pin(
            space.id, actor_username="anna", place={"lat": 1, "lon": 2}
        )


async def test_only_the_host_sets_it(stack):
    space = await _global(stack)
    stack.svc._own_instance_id = "someone-else"
    with pytest.raises(LocalAreaUnavailableError):
        await stack.svc.set_place_pin(
            space.id, actor_username="anna", place={"lat": 1, "lon": 2}
        )


async def test_leaving_the_global_tier_drops_the_pin(stack):
    space = await _global(stack, place={"lat": 47.3769, "lon": 8.5417})
    await stack.svc.update_config(
        space.id, actor_username="anna", space_type=SpaceType.PUBLIC
    )
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (None, None, None)

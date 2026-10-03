"""§27.9 release blocker: a member-published ``space_item`` is authorized by
the cert the space authority ISSUED, end to end (Phase B review P1–P4).

The real issuer (:class:`SpaceWriterCertService`, seat roster + the space's
``posts`` access level) mints the author household's cert; the real
receiver (:class:`SpacePublicInbound`) judges the item. Each case is one of
the review's repros:

* P1 — ``ADMIN_ONLY``: a plain member's household gets no ``write`` cert,
  so its member-published post is refused;
* P2 — ``MODERATED``: likewise, so the post can only reach the feed through
  the host's review queue;
* P3 — a user of a writer household who holds no seat (kicked, banned,
  never seated) is not in the cert's user binding;
* P4 — an inner crediting another household as its origin is refused.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from socialhome.domain.space import SpaceFeatureAccess
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.services.space_writer_cert_service import SpaceWriterCertService

from tests.services.test_space_public_inbound import (  # noqa: F401
    _item_frame,
    _signed_inner,
    env,
    item_env,
)

pytestmark = pytest.mark.security


class _IssuerSpaces:
    """The issuer's view: the space row, its seed, no local seats."""

    def __init__(self, repo, seed: bytes) -> None:
        self.repo = repo
        self.seed = seed

    async def get(self, space_id):
        return await self.repo.get(space_id)

    async def get_space_seed(self, space_id):
        return self.seed

    async def list_members(self, space_id):
        return []


class _Seats:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [r for r in self.rows if r.instance_id == instance_id]


class _Keys:
    def __init__(self, pks) -> None:
        self.pks = pks

    async def peer_identity_public_key(self, instance_id):
        return self.pks.get(instance_id)


async def _issued_cert(item_env, *, role: str, user_id: str | None = None) -> dict:  # noqa: F811
    origin = item_env["author_origin"]
    issuer = SpaceWriterCertService(
        space_repo=_IssuerSpaces(
            item_env["space_repo"], item_env["space_kp"].private_key
        ),
        remote_member_repo=_Seats(
            [
                SimpleNamespace(
                    instance_id=origin,
                    user_id=user_id or item_env["author_user_id"],
                    role=role,
                )
            ]
        ),
        space_key_repo=SqliteSpaceKeyRepo(item_env["db"]),
        own_instance_id="issuer.home",
        own_identity_pk=b"\0" * 32,
    )
    issuer.attach_federation(_Keys({origin: item_env["author_kp"].public_key}))
    cert = await issuer.issue_for_instance("sp-1", origin)
    assert cert is not None
    return cert.to_wire()


async def _set_posts(item_env, level: SpaceFeatureAccess) -> None:  # noqa: F811
    repo = item_env["space_repo"]
    space = await repo.get("sp-1")
    await repo.save(
        replace(space, features=replace(space.features, posts_access=level))
    )


async def test_an_open_space_member_publish_is_accepted(item_env):  # noqa: F811
    cert = await _issued_cert(item_env, role="member")
    await item_env["inbound"].handle(await _item_frame(item_env, cert=cert))
    assert await item_env["post_repo"].get("post-1") is not None


@pytest.mark.parametrize(
    "level", [SpaceFeatureAccess.ADMIN_ONLY, SpaceFeatureAccess.MODERATED]
)
async def test_p1_p2_a_plain_member_cannot_member_publish_past_the_level(
    item_env,  # noqa: F811
    level,
):
    await _set_posts(item_env, level)
    cert = await _issued_cert(item_env, role="member")
    assert cert["scope"] == "comment"
    await item_env["inbound"].handle(await _item_frame(item_env, cert=cert))
    assert await item_env["post_repo"].get("post-1") is None


async def test_p1_an_admin_may_still_member_publish_under_admin_only(item_env):  # noqa: F811
    await _set_posts(item_env, SpaceFeatureAccess.ADMIN_ONLY)
    cert = await _issued_cert(item_env, role="admin")
    await item_env["inbound"].handle(await _item_frame(item_env, cert=cert))
    assert await item_env["post_repo"].get("post-1") is not None


async def test_p3_a_seatless_user_of_a_writer_household_is_refused(item_env):  # noqa: F811
    """The household's cert binds its seated writer (``u-seated``); the
    author holds no seat, so its post is refused even though the household
    may write."""
    cert = await _issued_cert(item_env, role="member", user_id="u-seated")
    await item_env["inbound"].handle(await _item_frame(item_env, cert=cert))
    assert await item_env["post_repo"].get("post-1") is None


async def test_p4_an_origin_not_derived_from_the_author_key_is_refused(item_env):  # noqa: F811
    cert = await _issued_cert(item_env, role="member")
    inner = _signed_inner(item_env, origin="victim.home")
    await item_env["inbound"].handle(
        await _item_frame(item_env, inner=inner, cert=cert)
    )
    assert item_env["events"] == []

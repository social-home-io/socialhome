"""SpaceRelayCoverage (step 12): where a seed holder's copy of a remote
member's post is redundant — every condition must hold, anything unknown
keeps the copy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    verify_authority_event,
)
from socialhome.crypto import generate_space_keypair
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.services import space_relay_coverage as coverage_mod
from socialhome.services.space_relay_coverage import SpaceRelayCoverage

_LISTED_KEY = "ab" * 32


class _Resp:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, status: int = 200, without: object = 0) -> None:
        self.status = status
        self.without = without
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, *, params=None, allow_redirects=True, timeout=None):
        assert allow_redirects is False
        self.gets.append((url, dict(params or {})))
        return _Resp(
            self.status, {"subscribers": 9, "without_space_item": self.without}
        )


class _Gfs:
    def __init__(self, session: _Session, *, caps: bool = True) -> None:
        self.session = session
        self.caps = caps

    def publish_client(self):
        return self.session

    async def member_publish_trusted_supported(self, conn) -> bool:
        return self.caps

    async def member_publish_strict_supported(self, conn) -> bool:
        return self.caps

    async def space_item_followers_supported(self, conn) -> bool:
        return self.caps


class _Spaces:
    def __init__(self, seed: bytes, *, listed: bool = True) -> None:
        self.seed = seed
        servers = (
            SimpleNamespace(public_keys=lambda: (_LISTED_KEY.upper(),))
            if listed
            else None
        )
        self.space = SimpleNamespace(id="sp", gfs_servers=servers)

    async def get(self, space_id):
        return self.space

    async def get_space_seed(self, space_id):
        return self.seed


def _conn(key: str = _LISTED_KEY, cid: str = "g1"):
    return SimpleNamespace(id=cid, public_key=key, inbox_url=f"https://{cid}.test")


def _coverage(*, version_ok=True, caps=True, listed=True, session=None):
    skp = generate_space_keypair()
    asked: list[tuple[str, int]] = []

    async def _peer_supports(instance_id, *, min_version):
        asked.append((instance_id, min_version))
        return version_ok

    session = session or _Session()
    cov = SpaceRelayCoverage(
        space_repo=_Spaces(skp.private_key, listed=listed),  # type: ignore[arg-type]
        gfs_service=_Gfs(session, caps=caps),  # type: ignore[arg-type]
        peer_supports=_peer_supports,
    )
    return cov, session, skp, asked


async def test_a_v57_author_on_a_listed_server_whose_followers_all_read_items_is_skipped():
    cov, session, skp, asked = _coverage()
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None
    assert await skip(_conn()) is True
    assert asked == [("author.home", FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH)]
    # The question is the authority-signed summary query, nothing else.
    [(url, params)] = session.gets
    assert url == "https://g1.test/gfs/spaces/sp/subscribers"
    assert params["summary"] == "1"
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
        space_id="sp",
        payload={"space_id": "sp", "ts": params["ts"]},
        authority_sig=params["authority_sig"],
        authority_sig_suite=params["authority_sig_suite"],
        space_public_key=skp.public_key,
    )


@pytest.mark.parametrize("without", [1, None, -1, True, "0"])
async def test_one_follower_that_lacks_items_or_an_odd_answer_keeps_the_copy(without):
    cov, _session, _skp, _ = _coverage(session=_Session(without=without))
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None
    assert await skip(_conn()) is False


async def test_an_older_author_keeps_the_copy_everywhere():
    cov, session, _skp, _ = _coverage(version_ok=False)
    assert await cov.redundant_on("sp", "author.home") is None
    assert await cov.redundant_on("sp", None) is None
    assert session.gets == []


async def test_without_an_owner_list_the_copy_goes_everywhere():
    cov, _session, _skp, _ = _coverage(listed=False)
    assert await cov.redundant_on("sp", "author.home") is None


async def test_a_server_not_on_the_list_keeps_the_copy():
    """The author publishes only to the listed servers."""
    cov, session, _skp, _ = _coverage()
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None
    assert await skip(_conn(key="cd" * 32)) is False
    assert session.gets == []


async def test_a_server_that_proved_nothing_keeps_the_copy():
    cov, session, _skp, _ = _coverage(caps=False)
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None
    assert await skip(_conn()) is False
    assert session.gets == []


async def test_a_refused_query_keeps_the_copy_and_asks_again():
    cov, session, _skp, _ = _coverage(session=_Session(status=403))
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None
    assert await skip(_conn()) is False
    assert await skip(_conn()) is False
    assert len(session.gets) == 2


async def test_a_burst_of_posts_asks_once(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(coverage_mod, "monotonic", lambda: clock[0])
    cov, session, _skp, _ = _coverage()
    for _ in range(3):
        skip = await cov.redundant_on("sp", "author.home")
        assert skip is not None and await skip(_conn()) is True
    assert len(session.gets) == 1
    clock[0] += coverage_mod.SUMMARY_TTL_S + 1
    skip = await cov.redundant_on("sp", "author.home")
    assert skip is not None and await skip(_conn()) is True
    assert len(session.gets) == 2


async def test_without_the_federation_wired_nothing_is_skipped():
    skp = generate_space_keypair()
    cov = SpaceRelayCoverage(
        space_repo=_Spaces(skp.private_key),  # type: ignore[arg-type]
        gfs_service=_Gfs(_Session()),  # type: ignore[arg-type]
    )
    assert await cov.redundant_on("sp", "author.home") is None

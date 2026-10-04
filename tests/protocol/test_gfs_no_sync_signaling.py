"""§27.9 release blocker: a space sync tells the GFS nothing.

The old provider path called ``POST /cluster/signaling-session`` on the
paired GFS before every direct (WebRTC) space sync, and
``/cluster/signaling-session/release`` when the DataChannel opened or
failed. Both bodies carried ``from_instance`` plus a household Ed25519
signature, so the GFS logged *which household* started a direct sync,
*when*, and *how long* the ICE phase lasted — per sync. The returned
``signaling_node`` URL was then shipped in ``SPACE_SYNC_OFFER``, but no
receiver ever read it: the ANSWER and every ICE candidate ride the signed
household-to-household federation path, never a GFS node. The GFS round
trip bought nothing and leaked activity metadata.

These tests pin the fix:

* no household-side module can address the signaling endpoint at all;
* the provider's OFFER carries no ``signaling_node``;
* none of the sync handlers (BEGIN, OFFER, DIRECT_READY, DIRECT_FAILED)
  makes any HTTP request — the direct path works with no GFS involvement;
* a legacy OFFER from an older provider that still carries
  ``signaling_node`` is answered over the federation path, and the URL is
  never contacted.

Every test in the first group FAILS against the pre-change code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.sync_rtc import SyncSessionRecord
from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security

_PKG = Path(__file__).resolve().parents[2] / "socialhome"
_LEGACY_NODE = "https://node-b.gfs.test"


# ─── Static tripwires ────────────────────────────────────────────────────


def _household_modules() -> list[Path]:
    """Every production module that runs inside a household.

    ``socialhome/global_server/`` is the GFS itself — it keeps accepting
    the legacy endpoint from older households, so it is the only place
    the path may still appear.
    """
    gfs_dir = _PKG / "global_server"
    return [p for p in _PKG.rglob("*.py") if gfs_dir not in p.parents]


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_no_household_module_addresses_the_signaling_endpoint():
    """Old: ``GfsConnectionService`` built ``…/cluster/signaling-session``."""
    offenders = [
        str(p.relative_to(_PKG))
        for p in _household_modules()
        if any("signaling-session" in s for s in _string_literals(p))
    ]
    assert offenders == []


def test_no_household_module_puts_signaling_node_on_the_wire():
    """Old: the provider set ``offer_payload["signaling_node"]``."""
    offenders = [
        str(p.relative_to(_PKG))
        for p in _household_modules()
        if "signaling_node" in _string_literals(p)
    ]
    assert offenders == []


def test_gfs_connection_service_has_no_signaling_calls():
    """Old: ``request_signaling_node`` / ``release_signaling_node``."""
    assert not hasattr(GfsConnectionService, "request_signaling_node")
    assert not hasattr(GfsConnectionService, "release_signaling_node")


def test_federation_service_holds_no_gfs_connection():
    """Old: ``attach_gfs_connection_service`` wired the GFS client into the
    sync handlers. The sync path must have no way to reach the GFS."""
    assert not hasattr(FederationService, "attach_gfs_connection_service")
    assert "_gfs_connection_service" not in FederationService.__slots__
    assert "signaling_node" not in SyncSessionRecord.__dataclass_fields__


# ─── Behaviour: the sync handlers make no HTTP request ──────────────────


def _event(event_type, payload, *, from_instance="peer-1", space_id="sp"):
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=space_id,
    )


@pytest.fixture
def svc():
    s = FederationService.__new__(FederationService)
    s._bus = MagicMock()
    s._bus.publish = AsyncMock()
    s._transport = None
    s._sync_manager = MagicMock()
    s._space_sync_service = MagicMock()
    s._space_sync_service.stream_initial = AsyncMock()
    s._space_sync_receiver = None
    s._route_service = None
    s._routed_handler = None
    s._last_mesh_begin_at = {}
    s._own_instance_id = "self-iid"
    s._own_identity_seed = b"\x00" * 32
    s._ice_servers = []
    s._federation_repo = MagicMock()
    s._federation_repo.get_instance = AsyncMock(
        return_value=SimpleNamespace(
            status=PairingStatus.CONFIRMED, source=InstanceSource.MANUAL
        ),
    )
    return s


@pytest.fixture
def no_http():
    """Fail on ANY outbound aiohttp request made while the handlers run."""
    calls: list[str] = []

    async def _refuse(self, method, url, *args, **kwargs):
        calls.append(f"{method} {url}")
        raise AssertionError(f"sync handler made an HTTP request: {method} {url}")

    with patch.object(aiohttp.ClientSession, "_request", _refuse):
        yield calls


async def test_provider_offer_carries_no_signaling_node(svc, no_http):
    record = SimpleNamespace(
        rtc=SimpleNamespace(create_offer=AsyncMock(return_value="sdp-x")),
    )
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_begin(
            _event(
                FederationEventType.SPACE_SYNC_BEGIN,
                {
                    "sync_id": "s1",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
            ),
        )
    # The direct path still works with no GFS: the OFFER goes to the
    # requester over the household-to-household federation path.
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["event_type"] == FederationEventType.SPACE_SYNC_OFFER
    assert kwargs["to_instance_id"] == "peer-1"
    assert set(kwargs["payload"]) == {"sync_id", "sdp_offer", "ice_servers"}
    assert no_http == []


async def test_direct_ready_and_failed_contact_nothing(svc, no_http):
    session = SyncSessionRecord(
        sync_id="s1",
        space_id="sp",
        requester_instance_id="peer-1",
        provider_instance_id="self-iid",
        sync_mode="initial",
    )
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._sync_manager.close_session = MagicMock()
    await svc._handle_space_sync_direct_ready(
        _event(FederationEventType.SPACE_SYNC_DIRECT_READY, {"sync_id": "s1"}),
    )
    await svc._handle_space_sync_direct_failed(
        _event(FederationEventType.SPACE_SYNC_DIRECT_FAILED, {"sync_id": "s1"}),
    )
    svc._sync_manager.close_session.assert_called_once_with("s1")
    assert no_http == []


async def test_legacy_offer_with_signaling_node_is_answered_over_federation(
    svc,
    no_http,
):
    """An older provider may still ship ``signaling_node``. The requester
    answers the provider over federation and never dials the URL."""
    svc._sync_manager.pending_sync_request = MagicMock(
        return_value=SimpleNamespace(
            provider_instance_id="peer-1",
            space_id="sp",
        ),
    )
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-answer")
    svc._sync_manager.get_session = MagicMock(return_value=None)
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_offer(
            _event(
                FederationEventType.SPACE_SYNC_OFFER,
                {
                    "sync_id": "s1",
                    "sdp_offer": "sdp-x",
                    "ice_servers": [],
                    "signaling_node": _LEGACY_NODE,
                },
            ),
        )
    kwargs = send_mock.await_args.kwargs
    assert kwargs["event_type"] == FederationEventType.SPACE_SYNC_ANSWER
    assert kwargs["to_instance_id"] == "peer-1"
    assert _LEGACY_NODE not in repr(kwargs)
    assert no_http == []

"""Tripwire: every non-space inbound federation event has a binding rule.

Marked ``@pytest.mark.security``.

Space content is bound by the §24.11 space-scope / authorship families
(``test_space_content_scope.py`` and friends). Every OTHER event type the
real application registers a handler for must appear below, either with
the protocol test that proves its handler binds the rows it touches to the
signing household, or with a recorded reason why there is nothing to bind.
Registering a new inbound handler without adding it here fails this test,
so the question "whose row does this change?" is asked before it ships.
"""

from __future__ import annotations

import pathlib
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType
HERE = pathlib.Path(__file__).parent

#: Space-scoped families, bound by the space-scope / authorship rules.
_SPACE_PREFIXES = ("space_", "bazaar_")

_DM = "test_dm_scope.py"
_DM_GROUP = "test_dm_group_scope.py"
_USERS = "test_user_sync_scope.py"
_CAL_EVENT = "test_personal_calendar_event_scope.py"
_CAL_RSVP = "test_personal_calendar_rsvp_scope.py"
_HIGHLIGHT = "test_highlight_scope.py"
_MOMENT = "test_moment_scope.py"
_CALL = "test_call_signal_scope.py"
_APP = "test_app_session_scope.py"
_PAIRING = "test_pairing_auth_boundary.py"
_UNPAIR = "test_unpair_authority.py"
_MEDIA = "test_media_blob_scope.py"
_GFS_ROUTES = "test_gfs_relay_route_discovery.py"

#: Event type → the protocol test proving its binding rule.
BOUND: dict[FederationEventType, str] = {
    FET.DM_MESSAGE: _DM,
    FET.DM_MESSAGE_DELETED: _DM,
    FET.DM_MESSAGE_REACTION: _DM,
    FET.DM_USER_TYPING: _DM,
    FET.DM_HISTORY_REQUEST: _DM,
    FET.DM_HISTORY_CHUNK: _DM,
    FET.DM_HISTORY_COMPLETE: _DM,
    FET.DM_MEDIA_BLOB: _MEDIA,
    FET.DM_GROUP_ROSTER: _DM_GROUP,
    FET.DM_GROUP_LEAVE: _DM_GROUP,
    FET.DM_CONTACT_REQUEST: _USERS,
    FET.USERS_SYNC: _USERS,
    FET.USER_UPDATED: _USERS,
    FET.USER_REMOVED: _USERS,
    FET.USER_STATUS_UPDATED: _USERS,
    FET.USER_ONLINE: _USERS,
    FET.USER_IDLE: _USERS,
    FET.USER_OFFLINE: _USERS,
    FET.PERSONAL_CALENDAR_EVENT_CREATED: _CAL_EVENT,
    FET.PERSONAL_CALENDAR_EVENT_UPDATED: _CAL_EVENT,
    FET.PERSONAL_CALENDAR_EVENT_DELETED: _CAL_EVENT,
    FET.PERSONAL_CALENDAR_RSVP_UPDATED: _CAL_RSVP,
    FET.PERSONAL_CALENDAR_RSVP_DELETED: _CAL_RSVP,
    FET.HIGHLIGHT_CREATED: _HIGHLIGHT,
    FET.HIGHLIGHT_FRAME_APPENDED: _HIGHLIGHT,
    FET.HIGHLIGHT_FRAME_DELETED: _HIGHLIGHT,
    FET.HIGHLIGHT_DELETED: _HIGHLIGHT,
    FET.HIGHLIGHT_FRAME_VIEWED: _HIGHLIGHT,
    FET.HIGHLIGHT_FRAME_REACTED: _HIGHLIGHT,
    FET.HIGHLIGHT_FRAME_REACTION_REMOVED: _HIGHLIGHT,
    FET.MOMENT_CREATED: _MOMENT,
    FET.MOMENT_DELETED: _MOMENT,
    FET.MOMENT_REACTED: _MOMENT,
    FET.MOMENT_REACTION_REMOVED: _MOMENT,
    FET.CALL_HANGUP: _CALL,
    FET.CALL_END: _CALL,
    FET.CALL_DECLINE: _CALL,
    FET.CALL_BUSY: _CALL,
    FET.CALL_QUALITY: _CALL,
    FET.APP_SESSION: _APP,
    FET.APP_MESSAGE: _APP,
    FET.PAIRING_CONFIRM: _PAIRING,
    FET.PAIRING_ABORT: _PAIRING,
    FET.UNPAIR: _UNPAIR,
    FET.GFS_RELAY_PROBE: _GFS_ROUTES,
    FET.GFS_RELAY_PROBE_ACK: _GFS_ROUTES,
}

#: Event type → why its handler needs no row binding beyond ``from_instance``.
REASONED: dict[FederationEventType, str] = {
    FET.PRESENCE_UPDATED: "rows keyed on (from_instance, username) by construction",
    FET.LOCAL_HOME_LOCATION_CHANGED: "writes only the sender's own peer row",
    FET.URL_UPDATED: "writes only the sender's own peer row (URL validated)",
    FET.INSTANCE_CAPABILITIES_UPDATED: "writes only the sender's own peer row",
    FET.INSTANCE_SYNC_STATUS: "confirmed-peer check, then a log line only",
    FET.INSTANCE_RESYNC_REQUEST: "replays only spaces the sender is a member of",
    FET.PAIRING_INTRO: "no row written; surfaces a prompt for the admin",
    FET.PAIRING_ACCEPT: "no row written; surfaces the code for the admin",
    FET.PAIRING_INTRO_RELAY: "capped pending request awaiting admin approval",
    FET.PAIRING_INTRO_AUTO: "token-bound auto-pair flow, admin approves",
    FET.PAIRING_INTRO_AUTO_ACK: "token-bound auto-pair flow",
    FET.PAIRING_INTRO_AUTO_ACK_VIA: "relay step of the token-bound auto-pair flow",
    FET.FEDERATION_RTC_OFFER: "transport state keyed on the sender's own peer",
    FET.FEDERATION_RTC_ANSWER: "transport state keyed on the sender's own peer",
    FET.FEDERATION_RTC_ICE: "transport state keyed on the sender's own peer",
    FET.CALL_OFFER: "caller bound to the sender, callee to the conversation",
    FET.CALL_ANSWER: "requires the sender to host a participant of the call",
    FET.CALL_ICE: "sender participant bound (from_user); see calls receiver rules",
    FET.CALL_ICE_CANDIDATE: "same as CALL_ICE",
    FET.DM_RELAY: "opaque forward only; nothing is stored or delivered locally",
    FET.DM_HISTORY_CHUNK_ACK: "in-memory ack counter keyed on the sender",
    FET.USER_MOVED: "verified against the user's own pinned key (move link)",
    FET.USER_IDENTITY_RESOLVE: "read-only reply to a confirmed peer",
}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "tripwire.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _registered(aiohttp_client, tmp_dir) -> set[FederationEventType]:
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    registry = app[federation_service_key]._event_registry
    return {
        event_type
        for event_type in FederationEventType
        if registry.handlers_for(event_type)
    }


async def test_every_non_space_inbound_event_has_a_binding_rule(
    aiohttp_client, tmp_dir
):
    registered = await _registered(aiohttp_client, tmp_dir)
    non_space = {e for e in registered if not e.value.startswith(_SPACE_PREFIXES)}
    missing = sorted(e.value for e in non_space - BOUND.keys() - REASONED.keys())
    assert not missing, (
        "inbound handlers without a binding rule — add a protocol test that "
        f"binds the rows they touch to from_instance, or a reason: {missing}"
    )


async def test_the_lists_name_only_registered_events_and_real_tests(
    aiohttp_client, tmp_dir
):
    registered = await _registered(aiohttp_client, tmp_dir)
    assert not BOUND.keys() & REASONED.keys()
    stale = sorted(e.value for e in (BOUND.keys() | REASONED.keys()) - registered)
    assert not stale, f"no inbound handler is registered for: {stale}"
    for test_file in set(BOUND.values()):
        assert (HERE / test_file).is_file(), test_file

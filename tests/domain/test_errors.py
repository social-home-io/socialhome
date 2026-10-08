"""Coded refusals — ``socialhome/domain/errors.py`` and its subclasses."""

from __future__ import annotations

import pytest

from socialhome.domain.calendar import RsvpPastError
from socialhome.domain.conversation import (
    DmSelfError,
    DmTooLongError,
    GroupTooSmallError,
)
from socialhome.domain.federation import (
    GfsNotConnectedError,
    GfsNotSharedError,
    PairingKeywrapInvalidError,
    PairingReachInvalidError,
)
from socialhome.domain.errors import (
    CodedError,
    ImageTooLargeError,
    ImageUnreadableError,
    PayloadTooLargeError,
)
from socialhome.domain.space import (
    AgeRestrictedError,
    AlreadyMemberError,
    BannedFromSpaceError,
    HostNotPairedError,
    InviteExpiredError,
    InviteOnlyError,
    SpaceArchivedError,
    SpacePermissionError,
    SubscribeNotAllowedError,
    SubscriberReadOnlyError,
    UserAlreadyMemberError,
    UserBannedError,
)
from socialhome.services.bazaar_service import (
    BidTooLowError,
    ListingNotActiveError,
    OwnListingError,
)
from socialhome.services.dm_group_service import GroupMemberUnsupportedError
from socialhome.services.dm_service import (
    GUARDIAN_BLOCK_DETAIL,
    GUARDIAN_BLOCK_GROUP_DETAIL,
    RECIPIENT_BLOCKED_DETAIL,
    YOU_BLOCKED_DETAIL,
    RecipientBlockedError,
)
from socialhome.services.poll_service import PollClosedError


# ─── CodedError ──────────────────────────────────────────────────────────


def test_coded_error_defaults():
    exc = CodedError()
    assert exc.status == 422
    assert exc.code == "UNPROCESSABLE"
    assert exc.detail == "Request could not be processed."
    assert exc.params == {}
    assert str(exc) == exc.detail


def test_coded_error_overrides_are_per_instance():
    exc = CodedError("nope", status=409, code="X", params={"n": 1})
    assert (exc.status, exc.code, exc.detail, exc.params) == (
        409,
        "X",
        "nope",
        {"n": 1},
    )
    # The class defaults stay untouched.
    assert CodedError.status == 422
    assert CodedError.code == "UNPROCESSABLE"


def test_coded_error_copies_params():
    src = {"n": 1}
    exc = CodedError(params=src)
    src["n"] = 2
    assert exc.params == {"n": 1}


def test_image_too_large_reports_whole_megabytes():
    exc = ImageTooLargeError(10 * 1024 * 1024)
    assert (exc.status, exc.code) == (422, "IMAGE_TOO_LARGE")
    assert exc.params == {"max_mb": 10}
    assert not isinstance(exc, ValueError)
    # Never rounds down to zero.
    assert ImageTooLargeError(1000).params == {"max_mb": 1}


def test_payload_too_large_is_a_413_with_whole_megabytes():
    """A body/part over a route's cap (gallery items, backup import) —
    413, not 422: the whole request is refused before it is buffered."""
    exc = PayloadTooLargeError(100 * 1024 * 1024)
    assert isinstance(exc, CodedError)
    assert (exc.status, exc.code) == (413, "PAYLOAD_TOO_LARGE")
    assert exc.detail == "Upload exceeds size limit."
    assert exc.params == {"max_mb": 100}
    assert not isinstance(exc, ValueError)
    # Never rounds down to zero.
    assert PayloadTooLargeError(1000).params == {"max_mb": 1}


def test_image_unreadable_is_a_value_error_with_a_fixed_detail():
    exc = ImageUnreadableError()
    assert isinstance(exc, ValueError)
    assert (exc.status, exc.code) == (422, "IMAGE_UNREADABLE")
    assert exc.detail == "This image couldn't be opened."
    assert exc.params == {}


# ─── Spaces ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "status", "code", "base"),
    [
        (AlreadyMemberError(), 422, "ALREADY_MEMBER", ValueError),
        (UserAlreadyMemberError(), 403, "USER_ALREADY_MEMBER", SpacePermissionError),
        (InviteOnlyError(), 403, "INVITE_ONLY", SpacePermissionError),
        (
            SubscribeNotAllowedError(),
            403,
            "SUBSCRIBE_NOT_ALLOWED",
            SpacePermissionError,
        ),
        (SpaceArchivedError(), 403, "SPACE_ARCHIVED", SpacePermissionError),
        (HostNotPairedError(), 403, "NOT_PAIRED", SpacePermissionError),
        (InviteExpiredError(), 404, "INVITE_EXPIRED", KeyError),
    ],
)
def test_space_errors_keep_their_old_type(exc, status, code, base):
    assert isinstance(exc, CodedError)
    assert isinstance(exc, base)
    assert (exc.status, exc.code) == (status, code)


@pytest.mark.parametrize("cls", [BannedFromSpaceError, UserBannedError])
def test_ban_errors_are_banned_permission_errors_without_ids(cls):
    exc = cls()
    assert isinstance(exc, SpacePermissionError)
    assert exc.banned is True
    assert exc.status == 403
    assert exc.params == {}
    assert "'" not in exc.detail


def test_ban_codes_differ_for_self_and_other():
    assert BannedFromSpaceError().code == "BANNED"
    assert UserBannedError().code == "USER_BANNED"


def test_subscriber_read_only_carries_the_action():
    exc = SubscriberReadOnlyError("comment")
    assert exc.code == "SUBSCRIBER_READ_ONLY"
    assert exc.params == {"action": "comment"}
    assert "comment" in exc.detail


def test_age_restricted_carries_the_minimum_age():
    exc = AgeRestrictedError(16)
    assert (exc.status, exc.code) == (403, "AGE_RESTRICTED")
    assert exc.params == {"min_age": 16}
    assert isinstance(exc, SpacePermissionError)


def test_invite_expired_str_is_not_quoted():
    exc = InviteExpiredError()
    assert str(exc) == exc.detail
    assert not str(exc).startswith("'")


def test_subscribe_not_allowed_accepts_a_specific_detail():
    exc = SubscribeNotAllowedError("only public / global spaces can be subscribed to")
    assert exc.detail.startswith("only public")
    assert exc.code == "SUBSCRIBE_NOT_ALLOWED"


# ─── Conversations ───────────────────────────────────────────────────────


def test_dm_errors():
    assert (DmSelfError().status, DmSelfError().code) == (422, "DM_SELF")
    assert isinstance(DmSelfError(), ValueError)
    small = GroupTooSmallError(3)
    assert (small.code, small.params) == ("GROUP_TOO_SMALL", {"min": 3})
    assert isinstance(small, ValueError)
    long = DmTooLongError(1000)
    assert (long.code, long.params) == ("DM_TOO_LONG", {"max": 1000})
    assert isinstance(long, ValueError)


@pytest.mark.parametrize(
    ("detail", "code"),
    [
        (RECIPIENT_BLOCKED_DETAIL, "DM_BLOCKED"),
        (YOU_BLOCKED_DETAIL, "DM_YOU_BLOCKED"),
        (GUARDIAN_BLOCK_DETAIL, "DM_NOT_ALLOWED"),
        (GUARDIAN_BLOCK_GROUP_DETAIL, "DM_GROUP_NOT_ALLOWED"),
        ("something else", "FORBIDDEN"),
    ],
)
def test_recipient_blocked_code_follows_the_words(detail, code):
    exc = RecipientBlockedError(detail)
    assert isinstance(exc, PermissionError)
    assert exc.status == 403
    assert exc.code == code
    assert exc.detail == detail


def test_guardian_block_reaches_the_blocked_person_as_a_personal_block():
    """§CP.F2: the blocked person hears ``RECIPIENT_BLOCKED_DETAIL`` for a
    guardian block too — same words, so the same code. Nothing in the
    answer tells a guardian is involved."""
    personal = RecipientBlockedError(RECIPIENT_BLOCKED_DETAIL)
    guardian_seen_by_blocked = RecipientBlockedError(RECIPIENT_BLOCKED_DETAIL)
    assert personal.code == guardian_seen_by_blocked.code == "DM_BLOCKED"
    assert personal.params == guardian_seen_by_blocked.params == {}


def test_group_member_unsupported_params():
    exc = GroupMemberUnsupportedError("x", reason="too_old", name="Olaf")
    assert isinstance(exc, ValueError)
    assert (exc.status, exc.code) == (422, "GROUP_MEMBER_UNSUPPORTED")
    assert exc.params == {"reason": "too_old", "name": "Olaf"}
    legacy = GroupMemberUnsupportedError("y", reason="legacy_group")
    assert legacy.params == {"reason": "legacy_group", "name": ""}


# ─── Calendar, polls, bazaar ─────────────────────────────────────────────


def test_rsvp_past():
    exc = RsvpPastError()
    assert (exc.status, exc.code) == (422, "RSVP_PAST")
    assert not isinstance(exc, ValueError)


def test_poll_closed_detail_never_names_the_post():
    exc = PollClosedError("post-secret-123")
    assert (exc.status, exc.code) == (409, "POLL_CLOSED")
    assert exc.post_id == "post-secret-123"
    assert "post-secret-123" not in exc.detail
    assert "post-secret-123" not in str(exc)


def test_bid_too_low_carries_floor_and_currency():
    exc = BidTooLowError(1500, "EUR")
    assert (exc.status, exc.code) == (422, "BID_TOO_LOW")
    assert exc.params == {"floor_amount": 1500, "currency": "EUR"}


def test_own_listing_and_listing_not_active_status_per_site():
    assert OwnListingError().status == 422
    assert OwnListingError(status=403).status == 403
    assert OwnListingError().code == "OWN_LISTING"
    assert ListingNotActiveError(status=409).status == 409
    assert ListingNotActiveError().code == "LISTING_NOT_ACTIVE"


# ─── Pairing reach (§11 through a GFS) ───────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PairingReachInvalidError(), "INVALID_REACH"),
        (GfsNotConnectedError(), "GFS_NOT_CONNECTED"),
        (GfsNotSharedError(), "GFS_NOT_SHARED"),
        (PairingKeywrapInvalidError(), "KEYWRAP_INVALID"),
    ],
)
def test_pairing_reach_errors_are_coded_value_errors(exc, code):
    assert isinstance(exc, CodedError)
    assert isinstance(exc, ValueError)
    assert (exc.status, exc.code) == (422, code)
    assert exc.params == {}

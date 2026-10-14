"""Tests for DashboardDisplayService — wall-panel pairing + device tokens.

In-memory: a fake ``instance_config`` repo and a settable clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
from dataclasses import replace

import pytest

from socialhome.dashboard.auth import (
    PAIR_CODE_MAX_FAILURES,
    THROTTLE_MAX_FAILURES,
    DashboardAuth,
)
from socialhome.domain.household_dashboard import (
    CLAIM_TTL_SECONDS,
    MAX_DISPLAYS,
    MAX_PENDING_CLAIMS,
    MAX_PENDING_PAIRINGS,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    DashboardDisabledError,
    DashboardDisplayLimitError,
    DashboardDisplayNotFoundError,
    DashboardPairingCodeInvalidError,
    DashboardDisplayPinThrottledError,
    DashboardDisplayPinWrongError,
    DashboardDisplaySignedOutError,
    DashboardPairingThrottledError,
    DashboardPinRequiredError,
    DashboardSettingsInvalidError,
    DisplayDiscoveryFailedError,
    DisplayDiscoveryUnavailableError,
    DisplayPanelNeedsUpdateError,
    DisplayPanelNotFoundError,
    DisplayPanelOfflineError,
    ExternalDisplayPanel,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.dashboard_display_service import (
    SEEN_INTERVAL_S,
    DashboardDisplayService,
)
from socialhome.services.dashboard_settings_service import DashboardSettingsService

MAIN_PORT = 8080
_CODE = re.compile(rf"[{PAIRING_CODE_ALPHABET}]{{{PAIRING_CODE_LENGTH}}}")


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.rows.get(key)

    async def set(self, key: str, value: str) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> None:
        self.rows.pop(key, None)

    async def set_many(self, items) -> None:
        self.rows.update(items)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def repo() -> _Repo:
    return _Repo()


@pytest.fixture
async def settings(repo) -> DashboardSettingsService:
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    svc.attach_key_manager(KeyManager(os.urandom(32)))
    await svc.set_password(actor_is_admin=True, password="correct horse")
    await svc.update(actor_is_admin=True, patch={"enabled": True})
    return svc


@pytest.fixture
def auth(settings, clock) -> DashboardAuth:
    return DashboardAuth(settings, clock=clock)


@pytest.fixture
def svc(settings, auth, clock) -> DashboardDisplayService:
    return DashboardDisplayService(settings, auth, clock=clock)


async def _pair(svc: DashboardDisplayService, name: str = "Kitchen"):
    """Start, claim and collect: ``(display, token)``."""
    pairing = await svc.start_pairing(model="esp32-s3-box", firmware="0.1.0")
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name=name)
    status = await svc.poll(pairing.pair_id)
    assert status is not None and status.paired
    assert status.token is not None
    return display, status.token


# ── start / poll ────────────────────────────────────────────────────────


async def test_start_pairing_shape(svc):
    pairing = await svc.start_pairing()
    assert _CODE.fullmatch(pairing.code)
    assert pairing.expires_in == PAIRING_TTL_SECONDS
    # 128 random bits, url-safe.
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", pairing.pair_id)


async def test_codes_and_ids_are_unique(svc):
    pairings = [await svc.start_pairing() for _ in range(MAX_PENDING_PAIRINGS)]
    assert len({p.code for p in pairings}) == MAX_PENDING_PAIRINGS
    assert len({p.pair_id for p in pairings}) == MAX_PENDING_PAIRINGS


async def test_poll_pending(svc):
    pairing = await svc.start_pairing()
    status = await svc.poll(pairing.pair_id)
    assert status is not None
    assert status.paired is False
    assert status.token is None


async def test_poll_unknown_is_none(svc):
    assert await svc.poll("nope") is None


async def test_pending_pairing_expires(svc, clock):
    pairing = await svc.start_pairing()
    clock.now += PAIRING_TTL_SECONDS + 1
    assert await svc.poll(pairing.pair_id) is None
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="Kitchen")


async def test_oldest_pending_is_evicted(svc):
    first = await svc.start_pairing()
    for _ in range(MAX_PENDING_PAIRINGS):
        await svc.start_pairing()
    assert await svc.poll(first.pair_id) is None
    assert svc.pending_count == MAX_PENDING_PAIRINGS


async def test_start_refused_while_dashboard_is_off(svc, settings):
    await settings.update(actor_is_admin=True, patch={"enabled": False})
    with pytest.raises(DashboardDisabledError):
        await svc.start_pairing()


async def test_model_and_firmware_are_cleaned(svc):
    pairing = await svc.start_pairing(model="  box\x00\x1b[31m  ", firmware="x" * 200)
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    assert display.model == "box[31m"
    assert display.firmware == "x" * 64


@pytest.mark.parametrize("bad", [None, 3, ["a"], "", "   ", "\x00"])
async def test_missing_or_bad_model_is_none(svc, bad):
    pairing = await svc.start_pairing(model=bad, firmware=bad)
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    assert display.model is None
    assert display.firmware is None


# ── claim ───────────────────────────────────────────────────────────────


async def test_claim_pairs_and_token_is_collected_once(svc, settings):
    pairing = await svc.start_pairing(model="m", firmware="f")
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name="Kitchen")
    assert display.name == "Kitchen"
    assert (display.model, display.firmware) == ("m", "f")
    assert display.last_seen_at is None
    assert display.created_at.endswith("+00:00")
    status = await svc.poll(pairing.pair_id)
    assert status is not None and status.paired
    assert status.display_id == display.id
    assert status.token is not None and len(status.token) >= 43
    # Exactly once: the entry is gone after the token was handed out.
    assert await svc.poll(pairing.pair_id) is None
    # Only the SHA-256 is stored.
    stored = (await settings.displays())[0]
    assert stored.token_hash == hashlib.sha256(status.token.encode()).hexdigest()


async def test_claim_code_is_case_and_space_insensitive(svc):
    pairing = await svc.start_pairing()
    typed = f" {pairing.code[:3].lower()}-{pairing.code[3:].lower()} "
    display = await svc.claim(actor_is_admin=True, code=typed, name="Hall")
    assert display.name == "Hall"


async def test_claim_requires_admin(svc):
    pairing = await svc.start_pairing()
    with pytest.raises(PermissionError):
        await svc.claim(actor_is_admin=False, code=pairing.code, name="K")


async def test_claim_wrong_code(svc):
    await svc.start_pairing()
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")


@pytest.mark.parametrize("code", [None, 7, "", "ABC", "ABCDEFG", "ABCDE0", "ABCDÉ2"])
async def test_claim_malformed_code(svc, code):
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code=code, name="K")


async def test_claim_a_code_twice(svc):
    pairing = await svc.start_pairing()
    await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="K2")


@pytest.mark.parametrize("name", [None, 3, "", "   ", "x" * 41, "a\nb", "a\x00"])
async def test_claim_invalid_name(svc, name):
    pairing = await svc.start_pairing()
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.claim(actor_is_admin=True, code=pairing.code, name=name)
    assert exc.value.params == {"field": "name"}


async def test_claim_name_allows_emoji_sequences(svc):
    pairing = await svc.start_pairing()
    family = "Kids \U0001f468\u200d\U0001f469\u200d\U0001f467 room"
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name=family)
    assert display.name == family


@pytest.mark.parametrize("name", ["evil\u202eexe.txt", "a\u2066b", "a\ufeffb"])
async def test_claim_name_refuses_bidi_and_invisible_format_chars(svc, name):
    pairing = await svc.start_pairing()
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name=name)


async def test_claim_name_is_trimmed(svc):
    pairing = await svc.start_pairing()
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name="  Hall ")
    assert display.name == "Hall"


async def test_claim_refused_while_dashboard_is_off(svc, settings):
    pairing = await svc.start_pairing()
    await settings.update(actor_is_admin=True, patch={"enabled": False})
    with pytest.raises(DashboardDisabledError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="K")


async def test_wrong_codes_are_throttled_globally(svc, auth):
    pairing = await svc.start_pairing()
    for _ in range(PAIR_CODE_MAX_FAILURES):
        with pytest.raises(DashboardPairingCodeInvalidError):
            await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")
    # Even the right code waits now — guesses can't race the limit.
    with pytest.raises(DashboardPairingThrottledError) as exc:
        await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    assert exc.value.params["retry_after"] > 0
    # Never the login brake nor the login table: tablets still sign in.
    assert auth.global_failure_count == 0
    assert auth.throttle_key_count == 0
    assert auth.begin_attempt("10.0.0.1") == 0


async def test_locked_out_logins_never_throttle_pairing(settings, clock):
    """A login table full of locked-out clients (and a tripped global
    brake) must not turn an admin's code entry into a 429."""
    auth = DashboardAuth(settings, clock=clock, max_throttle_keys=2)
    svc = DashboardDisplayService(settings, auth, clock=clock)
    for client in ("10.0.0.1", "10.0.0.2"):
        for _ in range(THROTTLE_MAX_FAILURES):
            auth.begin_attempt(client)
    for _ in range(40):
        auth.begin_attempt("10.0.0.1")
    assert auth.begin_attempt("10.0.0.3") > 0
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")
    await _pair(svc)


async def test_right_code_clears_the_failures(svc, auth):
    for _ in range(PAIR_CODE_MAX_FAILURES - 1):
        with pytest.raises(DashboardPairingCodeInvalidError):
            await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")
    await _pair(svc)
    assert auth.pair_code_failure_count == 0


async def test_display_limit(svc):
    for i in range(MAX_DISPLAYS):
        await _pair(svc, name=f"P{i}")
    pairing = await svc.start_pairing()
    with pytest.raises(DashboardDisplayLimitError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="One more")
    # Nothing to collect for the refused panel.
    status = await svc.poll(pairing.pair_id)
    assert status is not None and not status.paired


async def test_paired_token_pickup_gets_a_fresh_window(svc, clock):
    pairing = await svc.start_pairing()
    clock.now += PAIRING_TTL_SECONDS - 5
    await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    clock.now += 60
    status = await svc.poll(pairing.pair_id)
    assert status is not None and status.paired


async def test_token_is_never_logged(svc, caplog):
    caplog.set_level(logging.DEBUG)
    _, token = await _pair(svc)
    await svc.authenticate(token)
    assert token not in caplog.text


# ── authenticate / seen ─────────────────────────────────────────────────


async def test_authenticate(svc):
    display, token = await _pair(svc)
    assert await svc.authenticate(token) == display


@pytest.mark.parametrize("bad", [None, "", "x", "a" * 43, "é" * 43, "a" * 500])
async def test_authenticate_refuses_garbage(svc, bad):
    await _pair(svc)
    assert await svc.authenticate(bad) is None


async def test_authenticate_picks_the_right_display(svc):
    _, t1 = await _pair(svc, "One")
    d2, t2 = await _pair(svc, "Two")
    found = await svc.authenticate(t2)
    assert found is not None and found.id == d2.id
    assert t1 != t2


async def test_removed_display_is_revoked(svc):
    display, token = await _pair(svc)
    await svc.remove(actor_is_admin=True, display_id=display.id)
    assert await svc.authenticate(token) is None


async def test_sign_out_all_tablets_revokes_displays(svc, settings):
    _, token = await _pair(svc)
    await settings.revoke_sessions(actor_is_admin=True)
    assert await svc.authenticate(token) is None
    assert await svc.list(actor_is_admin=True) == ()


async def test_seen_is_written_at_most_once_a_minute(svc, settings, clock):
    display, _ = await _pair(svc)
    await svc.seen(display, firmware="0.2.0", model=None)
    first = (await settings.displays())[0]
    assert first.last_seen_at is not None
    assert first.firmware == "0.2.0"
    clock.now += SEEN_INTERVAL_S - 1
    await svc.seen(display, firmware="0.3.0", model=None)
    assert (await settings.displays())[0] == first
    clock.now += 2
    await svc.seen(display, firmware="0.3.0\x07", model="box")
    later = (await settings.displays())[0]
    assert later.last_seen_at != first.last_seen_at
    assert (later.firmware, later.model) == ("0.3.0", "box")


async def test_seen_memory_stays_bounded(svc, clock):
    display, _ = await _pair(svc)
    for i in range(10 * MAX_DISPLAYS):
        stub = type(display)(
            id=f"x{i}", name="n", token_hash="h", created_at=display.created_at
        )
        await svc.seen(stub, firmware=None, model=None)
        clock.now += SEEN_INTERVAL_S + 1
    assert svc.seen_count <= 4 * MAX_DISPLAYS


# ── admin list / rename / remove ────────────────────────────────────────


async def test_list_requires_admin(svc):
    with pytest.raises(PermissionError):
        await svc.list(actor_is_admin=False)


async def test_list(svc):
    d, _ = await _pair(svc)
    assert await svc.list(actor_is_admin=True) == (d,)


async def test_rename(svc):
    d, _ = await _pair(svc)
    renamed = await svc.update(
        actor_is_admin=True, display_id=d.id, patch={"name": " Hall "}
    )
    assert renamed.name == "Hall"
    with pytest.raises(PermissionError):
        await svc.update(actor_is_admin=False, display_id=d.id, patch={"name": "x"})
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.update(actor_is_admin=True, display_id=d.id, patch={"name": ""})
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.update(actor_is_admin=True, display_id="nope", patch={"name": "x"})


async def test_remove(svc):
    d, _ = await _pair(svc)
    with pytest.raises(PermissionError):
        await svc.remove(actor_is_admin=False, display_id=d.id)
    await svc.remove(actor_is_admin=True, display_id=d.id)
    assert await svc.list(actor_is_admin=True) == ()
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.remove(actor_is_admin=True, display_id=d.id)


# ── review regressions ──────────────────────────────────────────────────


async def test_parked_token_is_dropped_when_the_display_is_removed(svc):
    pairing = await svc.start_pairing()
    display = await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    await svc.remove(actor_is_admin=True, display_id=display.id)
    # The panel never gets a token that can't work.
    assert await svc.poll(pairing.pair_id) is None


async def test_parked_token_is_dropped_after_sign_out_all(svc, settings):
    pairing = await svc.start_pairing()
    await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    await settings.revoke_sessions(actor_is_admin=True)
    assert await svc.poll(pairing.pair_id) is None


async def test_eviction_spares_claimed_pairings(svc):
    first = await svc.start_pairing()
    await svc.claim(actor_is_admin=True, code=first.code, name="K")
    second = await svc.start_pairing()
    for _ in range(MAX_PENDING_PAIRINGS):
        await svc.start_pairing()
    status = await svc.poll(first.pair_id)
    assert status is not None and status.paired
    assert await svc.poll(second.pair_id) is None


async def test_seen_racing_remove_never_resurrects(svc, settings):
    display, _ = await _pair(svc)
    await asyncio.gather(
        svc.seen(display, firmware="9", model=None),
        svc.remove(actor_is_admin=True, display_id=display.id),
    )
    assert await settings.displays() == ()


@pytest.mark.parametrize("name", ["a\u2028b", "a\u2029b"])
async def test_claim_name_refuses_line_separators(svc, name):
    pairing = await svc.start_pairing()
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name=name)


# ── privacy lock ────────────────────────────────────────────────────────

PIN = "4321"


async def _locked(svc, settings):
    await settings.set_pin(actor_is_admin=True, pin=PIN)
    display, token = await _pair(svc)
    await svc.lock(display)
    stored = await svc.authenticate(token)
    assert stored is not None and stored.locked
    return stored, token


async def test_lock_needs_a_pin(svc, settings):
    display, _ = await _pair(svc)
    with pytest.raises(DashboardPinRequiredError) as exc:
        await svc.lock(display)
    assert exc.value.status == 409
    assert not (await settings.displays())[0].locked


async def test_lock_is_stored_and_survives_a_restart(svc, settings, repo):
    display, token = await _locked(svc, settings)
    fresh = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    again = DashboardDisplayService(fresh, DashboardAuth(fresh))
    found = await again.authenticate(token)
    assert found is not None and found.locked
    assert await again.is_locked(found)


async def test_is_locked_needs_a_pin(svc, settings):
    display, _ = await _locked(svc, settings)
    assert await svc.is_locked(display)
    await settings.delete_pin(actor_is_admin=True)
    # Even a stale flag (a crash mid-delete) counts as unlocked with no PIN.
    assert not await svc.is_locked(replace(display, locked=True))


async def test_unlock_with_the_pin(svc, settings, auth):
    display, token = await _locked(svc, settings)
    await svc.unlock(display, PIN)
    found = await svc.authenticate(token)
    assert found is not None and not found.locked
    assert auth.failure_count(DashboardAuth.display_pin_throttle_key(display.id)) == 0


async def test_unlock_an_unlocked_panel_checks_nothing(svc, settings, auth):
    await settings.set_pin(actor_is_admin=True, pin=PIN)
    display, _ = await _pair(svc)
    # No PIN oracle and no throttle entry for a panel that isn't locked.
    await svc.unlock(display, "0000")
    assert auth.failure_count(DashboardAuth.display_pin_throttle_key(display.id)) == 0


@pytest.mark.parametrize("pin", ["0000", "12", "abcd", None, 1234, ""])
async def test_wrong_pin_counts_and_says_how_many_are_left(svc, settings, pin):
    display, token = await _locked(svc, settings)
    with pytest.raises(DashboardDisplayPinWrongError) as exc:
        await svc.unlock(display, pin)
    assert exc.value.status == 403
    assert exc.value.params == {"remaining": THROTTLE_MAX_FAILURES - 1}
    found = await svc.authenticate(token)
    assert found is not None and found.locked


async def test_sixth_wrong_try_unpairs_the_panel(svc, settings, auth, caplog):
    display, token = await _locked(svc, settings)
    for left in range(THROTTLE_MAX_FAILURES - 1, -1, -1):
        with pytest.raises(DashboardDisplayPinWrongError) as exc:
            await svc.unlock(display, "0000")
        assert exc.value.params == {"remaining": left}
    caplog.set_level(logging.WARNING)
    # Even the right PIN: the budget is spent, the panel is signed out.
    with pytest.raises(DashboardDisplaySignedOutError) as signed_out:
        await svc.unlock(display, PIN)
    assert signed_out.value.status == 401
    assert await svc.authenticate(token) is None
    assert await settings.displays() == ()
    assert "too many wrong PINs" in caplog.text
    assert token not in caplog.text


async def test_wrong_pins_age_out(svc, settings, clock):
    display, token = await _locked(svc, settings)
    for _ in range(THROTTLE_MAX_FAILURES):
        with pytest.raises(DashboardDisplayPinWrongError):
            await svc.unlock(display, "0000")
    clock.now += 15 * 60
    await svc.unlock(display, PIN)
    assert await svc.authenticate(token) is not None


async def test_full_throttle_table_only_asks_to_wait(settings, clock):
    auth = DashboardAuth(settings, clock=clock, max_throttle_keys=1)
    svc = DashboardDisplayService(settings, auth, clock=clock)
    # Someone else's locked-out bucket fills the only slot.
    for _ in range(THROTTLE_MAX_FAILURES):
        auth.begin_attempt("pin:other", count_global=False)
    display, token = await _locked(svc, settings)
    with pytest.raises(DashboardDisplayPinThrottledError) as exc:
        await svc.unlock(display, PIN)
    assert exc.value.status == 429 and exc.value.params["retry_after"] >= 1
    assert await svc.authenticate(token) is not None


async def test_unlock_of_a_removed_panel_is_signed_out(svc, settings):
    display, _ = await _locked(svc, settings)
    await svc.remove(actor_is_admin=True, display_id=display.id)
    with pytest.raises(DashboardDisplaySignedOutError):
        await svc.unlock(display, PIN)


async def test_lock_of_a_removed_panel_is_signed_out(svc, settings):
    await settings.set_pin(actor_is_admin=True, pin=PIN)
    display, _ = await _pair(svc)
    await svc.remove(actor_is_admin=True, display_id=display.id)
    with pytest.raises(DashboardDisplaySignedOutError):
        await svc.lock(display)


async def test_admin_unlock(svc, settings):
    display, token = await _locked(svc, settings)
    with pytest.raises(PermissionError):
        await svc.update(
            actor_is_admin=False, display_id=display.id, patch={"locked": True}
        )
    unlocked = await svc.update(
        actor_is_admin=True, display_id=display.id, patch={"locked": False}
    )
    assert unlocked.locked is False and unlocked.name == "Kitchen"
    found = await svc.authenticate(token)
    assert found is not None and not found.locked
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.update(
            actor_is_admin=True, display_id="nope", patch={"locked": False}
        )


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"locked": True}, "locked"),
        ({"locked": 0}, "locked"),
        ({"name": "Hall", "locked": True}, "locked"),
        ({"name": "", "locked": False}, "name"),
        ({}, "name"),
    ],
)
async def test_update_validates_before_writing(svc, settings, patch, field):
    display, _ = await _locked(svc, settings)
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, display_id=display.id, patch=patch)
    assert exc.value.params == {"field": field}
    stored = (await settings.displays())[0]
    assert (stored.name, stored.locked) == ("Kitchen", True)


async def test_update_renames_and_unlocks(svc, settings):
    display, _ = await _locked(svc, settings)
    updated = await svc.update(
        actor_is_admin=True,
        display_id=display.id,
        patch={"name": " Hall ", "locked": False},
    )
    assert (updated.name, updated.locked) == ("Hall", False)


async def test_admin_unlock_gives_the_panel_fresh_pin_tries(svc, settings, auth):
    display, token = await _locked(svc, settings)
    for _ in range(THROTTLE_MAX_FAILURES):
        with pytest.raises(DashboardDisplayPinWrongError):
            await svc.unlock(display, "0000")
    await svc.update(
        actor_is_admin=True, display_id=display.id, patch={"locked": False}
    )
    assert auth.failure_count(DashboardAuth.display_pin_throttle_key(display.id)) == 0
    # Locked again, one wrong PIN no longer unpairs it.
    locked = await svc.authenticate(token)
    assert locked is not None
    await svc.lock(locked)
    with pytest.raises(DashboardDisplayPinWrongError):
        await svc.unlock(locked, "0000")


async def test_unlock_reads_the_stored_lock_not_the_request_snapshot(
    svc, settings, auth
):
    await settings.set_pin(actor_is_admin=True, pin=PIN)
    display, token = await _pair(svc)
    # Authenticated before a concurrent /lock landed: still checked.
    await svc.lock(display)
    with pytest.raises(DashboardDisplayPinWrongError):
        await svc.unlock(display, "0000")
    found = await svc.authenticate(token)
    assert found is not None and found.locked


async def test_rename_and_unlock_is_one_write(svc, settings, repo):
    display, _ = await _locked(svc, settings)
    writes: list[str] = []
    real = repo.set

    async def spy(key, value):
        writes.append(key)
        await real(key, value)

    repo.set = spy
    await svc.update(
        actor_is_admin=True,
        display_id=display.id,
        patch={"name": "Hall", "locked": False},
    )
    assert writes == ["dashboard.displays"]


# ── Panels found through the platform (Home Assistant): connect claims ──


KITCHEN_MAC = "aa:bb:cc:dd:ee:01"
HALL_MAC = "aa:bb:cc:dd:ee:02"


def _panel(
    panel_id: str = "dev-kitchen",
    *,
    name: str = "Kitchen panel",
    mac: str | None = KITCHEN_MAC,
    action: str | None = "sh_dash_kitchen_connect_social_home",
    online: bool = True,
) -> ExternalDisplayPanel:
    return ExternalDisplayPanel(
        id=panel_id,
        name=name,
        mac=mac,
        sw_version="2026.10.13 (ESPHome 2026.9.1)",
        connect_action=action,
        online=online,
    )


class _Directory:
    """A :class:`DisplayDirectory` test double: lists ``panels`` and
    records every connect request it is asked to deliver."""

    def __init__(self, *panels: ExternalDisplayPanel) -> None:
        self.panels = list(panels)
        self.sent: list[tuple[str, str, str]] = []
        self.fail_list = False
        self.fail_connect = False

    async def list_panels(self) -> list[ExternalDisplayPanel]:
        if self.fail_list:
            raise DisplayDiscoveryFailedError()
        return list(self.panels)

    async def connect(self, panel, *, url: str, claim: str) -> None:
        if self.fail_connect:
            raise DisplayDiscoveryFailedError()
        self.sent.append((panel.id, url, claim))


URL = "http://192.168.1.20:8100"


@pytest.fixture
def directory() -> _Directory:
    return _Directory(_panel(), _panel("dev-hall", name="Hall", mac=HALL_MAC))


@pytest.fixture
def hsvc(settings, auth, clock, directory) -> DashboardDisplayService:
    return DashboardDisplayService(settings, auth, directory=directory, clock=clock)


async def _connect(svc, directory, panel_id: str = "dev-kitchen") -> str:
    """Send ``panel_id`` a connect request; the claim it was sent."""
    await svc.connect_platform_panel(actor_is_admin=True, panel_id=panel_id, url=URL)
    sent_id, url, claim = directory.sent[-1]
    assert (sent_id, url) == (panel_id, URL)
    return claim


def test_platform_discovery_follows_the_directory(svc, hsvc):
    assert svc.can_discover is False
    assert hsvc.can_discover is True


async def test_without_a_directory_discovery_is_unavailable(svc):
    with pytest.raises(DisplayDiscoveryUnavailableError):
        await svc.platform_panels(actor_is_admin=True)
    with pytest.raises(DisplayDiscoveryUnavailableError):
        await svc.connect_platform_panel(actor_is_admin=True, panel_id="x", url=URL)


async def test_platform_panels_are_admin_only(hsvc):
    with pytest.raises(PermissionError):
        await hsvc.platform_panels(actor_is_admin=False)
    with pytest.raises(PermissionError):
        await hsvc.connect_platform_panel(
            actor_is_admin=False, panel_id="dev-kitchen", url=URL
        )


async def test_platform_panels_match_paired_displays_by_mac(hsvc, directory):
    claim = await _connect(hsvc, directory)
    status = await hsvc.spend_claim(claim, mac=KITCHEN_MAC.upper())
    assert status is not None
    found = await hsvc.platform_panels(actor_is_admin=True)
    assert [(p.id, d.id if d else None) for p, d in found] == [
        ("dev-kitchen", status.display_id),
        ("dev-hall", None),
    ]


async def test_a_panel_without_a_mac_never_matches(hsvc, directory, settings):
    directory.panels = [_panel(mac=None)]
    found = await hsvc.platform_panels(actor_is_admin=True)
    assert found == [(directory.panels[0], None)]


async def test_claim_pairs_the_panel_once(hsvc, directory, settings):
    expires = await hsvc.connect_platform_panel(
        actor_is_admin=True, panel_id="dev-kitchen", url=URL
    )
    assert expires == CLAIM_TTL_SECONDS
    claim = directory.sent[-1][2]
    # 256 random bits, url-safe — what the panel's NVS / HA action carry.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", claim)
    status = await hsvc.spend_claim(
        claim, mac=KITCHEN_MAC, model="guition-jc8012p4a1", firmware="2026.10.13"
    )
    assert status is not None and status.paired and status.token
    (display,) = await settings.displays()
    assert display.id == status.display_id
    assert display.name == "Kitchen panel"
    assert display.mac == KITCHEN_MAC
    assert (display.model, display.firmware) == ("guition-jc8012p4a1", "2026.10.13")
    assert await hsvc.authenticate(status.token) == display
    # Single use: the same claim again is unknown.
    assert await hsvc.spend_claim(claim, mac=KITCHEN_MAC) is None
    assert hsvc.claim_count == 0


async def test_only_the_claims_hash_is_kept(hsvc, directory):
    claim = await _connect(hsvc, directory)
    assert claim not in repr(hsvc._claims)
    assert all(claim.encode() not in c.digest for c in hsvc._claims.values())


async def test_claim_expires(hsvc, directory, clock):
    claim = await _connect(hsvc, directory)
    clock.now += CLAIM_TTL_SECONDS
    assert await hsvc.spend_claim(claim, mac=KITCHEN_MAC) is None
    assert hsvc.claim_count == 0


async def test_claim_for_one_panel_is_refused_from_another_and_burnt(
    hsvc, directory, settings
):
    claim = await _connect(hsvc, directory)
    assert await hsvc.spend_claim(claim, mac=HALL_MAC) is None
    assert await hsvc.spend_claim(claim, mac=None) is None
    # Spent by the wrong-MAC try: the right panel can't use it any more.
    assert await hsvc.spend_claim(claim, mac=KITCHEN_MAC) is None
    assert await settings.displays() == ()


async def test_malformed_claims_are_unknown(hsvc, directory):
    await _connect(hsvc, directory)
    for bad in (None, 7, "", "short", "x" * 43 + "!", "é" * 43):
        assert await hsvc.spend_claim(bad, mac=KITCHEN_MAC) is None
    assert hsvc.claim_count == 1


async def test_claims_are_compared_in_constant_time(hsvc, directory, monkeypatch):
    await _connect(hsvc, directory, "dev-kitchen")
    await _connect(hsvc, directory, "dev-hall")
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(
        "socialhome.services.dashboard_display_service.hmac.compare_digest", spy
    )
    assert await hsvc.spend_claim("A" * 43, mac=KITCHEN_MAC) is None
    # Every pending claim compared, digest against digest — no early exit.
    assert len(calls) == 2
    assert all(len(a) == len(b) == 32 for a, b in calls)


async def test_a_new_connect_replaces_the_panels_old_claim(hsvc, directory):
    first = await _connect(hsvc, directory)
    second = await _connect(hsvc, directory)
    assert first != second and hsvc.claim_count == 1
    assert await hsvc.spend_claim(first, mac=KITCHEN_MAC) is None
    assert await hsvc.spend_claim(second, mac=KITCHEN_MAC) is not None


async def test_pending_claims_are_bounded(hsvc, directory):
    directory.panels = [
        _panel(f"dev-{i}", mac=f"aa:bb:cc:00:00:{i:02x}")
        for i in range(MAX_PENDING_CLAIMS + 3)
    ]
    claims = [
        await _connect(hsvc, directory, f"dev-{i}")
        for i in range(MAX_PENDING_CLAIMS + 3)
    ]
    assert hsvc.claim_count == MAX_PENDING_CLAIMS
    # The oldest went first.
    assert await hsvc.spend_claim(claims[0], mac="aa:bb:cc:00:00:00") is None
    last = MAX_PENDING_CLAIMS + 2
    assert (
        await hsvc.spend_claim(claims[last], mac=f"aa:bb:cc:00:00:{last:02x}")
        is not None
    )


async def test_an_undelivered_connect_takes_its_claim_along(hsvc, directory):
    directory.fail_connect = True
    with pytest.raises(DisplayDiscoveryFailedError):
        await hsvc.connect_platform_panel(
            actor_is_admin=True, panel_id="dev-kitchen", url=URL
        )
    assert hsvc.claim_count == 0


async def test_connect_refuses_unknown_offline_and_old_panels(hsvc, directory):
    directory.panels = [
        _panel("dev-off", online=False),
        _panel("dev-old", action=None),
        _panel("dev-nomac", mac=None),
    ]
    cases = {
        "dev-gone": DisplayPanelNotFoundError,
        "dev-off": DisplayPanelOfflineError,
        "dev-old": DisplayPanelNeedsUpdateError,
        "dev-nomac": DisplayPanelNeedsUpdateError,
    }
    for panel_id, error in cases.items():
        with pytest.raises(error):
            await hsvc.connect_platform_panel(
                actor_is_admin=True, panel_id=panel_id, url=URL
            )
    assert directory.sent == [] and hsvc.claim_count == 0


async def test_connect_and_spend_need_the_dashboard_on(hsvc, directory, settings):
    claim = await _connect(hsvc, directory)
    await settings.update(actor_is_admin=True, patch={"enabled": False})
    with pytest.raises(DashboardDisabledError):
        await hsvc.connect_platform_panel(
            actor_is_admin=True, panel_id="dev-kitchen", url=URL
        )
    with pytest.raises(DashboardDisabledError):
        await hsvc.spend_claim(claim, mac=KITCHEN_MAC)


async def test_reconnecting_replaces_the_panels_old_pairing(hsvc, directory, settings):
    first = await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
    hall = await hsvc.spend_claim(
        await _connect(hsvc, directory, "dev-hall"), mac=HALL_MAC
    )
    again = await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
    assert first and hall and again
    ids = [d.id for d in await settings.displays()]
    assert ids == [hall.display_id, again.display_id]
    # The old token died with its record.
    assert await hsvc.authenticate(first.token) is None


async def test_reconnecting_at_the_limit_still_works(hsvc, directory, settings):
    await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
    for i in range(MAX_DISPLAYS - 1):
        await _pair(hsvc, f"Panel {i}")
    assert len(await settings.displays()) == MAX_DISPLAYS
    assert (
        await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
        is not None
    )
    with pytest.raises(DashboardDisplayLimitError):
        await hsvc.spend_claim(
            await _connect(hsvc, directory, "dev-hall"), mac=HALL_MAC
        )


@pytest.mark.parametrize(
    ("ha_name", "expected"),
    [
        ("  Kitchen panel ", "Kitchen panel"),
        ("A" * 60, "A" * 40),
        ("bad‮name", "Wall panel"),
    ],
)
async def test_the_display_is_named_after_the_panel(
    hsvc, directory, settings, ha_name, expected
):
    directory.panels = [_panel(name=ha_name)]
    await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
    (display,) = await settings.displays()
    assert display.name == expected


@pytest.mark.parametrize("revoke", ["sessions", "password"])
async def test_sign_out_revokes_pending_claims(hsvc, directory, settings, revoke):
    """A claim minted before "sign out all tablets" (or a password change)
    can't pair anything after it — it is bound to the session epoch."""
    claim = await _connect(hsvc, directory)
    if revoke == "sessions":
        await settings.revoke_sessions(actor_is_admin=True)
    else:
        await settings.set_password(actor_is_admin=True, password="new horse battery")
    assert await hsvc.spend_claim(claim, mac=KITCHEN_MAC) is None
    assert await settings.displays() == ()
    # A fresh Connect after the sign-out works.
    assert (
        await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
        is not None
    )


async def test_removing_a_display_drops_its_pending_claim(hsvc, directory, settings):
    first = await hsvc.spend_claim(await _connect(hsvc, directory), mac=KITCHEN_MAC)
    hall_claim = await _connect(hsvc, directory, "dev-hall")
    again = await _connect(hsvc, directory)
    await hsvc.remove(actor_is_admin=True, display_id=first.display_id)
    assert await hsvc.spend_claim(again, mac=KITCHEN_MAC) is None
    # Another panel's claim is untouched.
    assert await hsvc.spend_claim(hall_claim, mac=HALL_MAC) is not None


async def test_code_pairing_keeps_the_panels_mac(hsvc, settings):
    pairing = await hsvc.start_pairing(mac="AA-BB-CC-DD-EE-01")
    await hsvc.claim(actor_is_admin=True, code=pairing.code, name="Kitchen")
    (display,) = await settings.displays()
    assert display.mac == KITCHEN_MAC


async def test_seen_records_a_reported_mac_once(hsvc, settings, clock):
    display, _ = await _pair(hsvc)
    assert display.mac is None
    await hsvc.seen(display, firmware=None, model=None, mac="aabbccddee01")
    (stored,) = await settings.displays()
    assert stored.mac == KITCHEN_MAC
    # Write-once: a paired panel can't later pose as another one.
    clock.now += SEEN_INTERVAL_S
    await hsvc.seen(stored, firmware=None, model=None, mac=HALL_MAC)
    (stored,) = await settings.displays()
    assert stored.mac == KITCHEN_MAC


async def test_claim_logs_never_carry_the_claim_or_token(hsvc, directory, caplog):
    caplog.set_level(logging.DEBUG)
    claim = await _connect(hsvc, directory)
    await hsvc.spend_claim(claim, mac=HALL_MAC)
    claim2 = await _connect(hsvc, directory)
    status = await hsvc.spend_claim(claim2, mac=KITCHEN_MAC)
    assert status is not None and status.token is not None
    for secret in (claim, claim2, status.token):
        assert secret not in caplog.text

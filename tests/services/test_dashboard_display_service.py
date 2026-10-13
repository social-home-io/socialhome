"""Tests for DashboardDisplayService — wall-panel pairing + device tokens.

In-memory: a fake ``instance_config`` repo and a settable clock.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

import pytest

from socialhome.dashboard.auth import THROTTLE_MAX_FAILURES, DashboardAuth
from socialhome.domain.household_dashboard import (
    MAX_DISPLAYS,
    MAX_PENDING_PAIRINGS,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    DashboardDisabledError,
    DashboardDisplayLimitError,
    DashboardDisplayNotFoundError,
    DashboardPairingCodeInvalidError,
    DashboardPairingThrottledError,
    DashboardSettingsInvalidError,
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
    status = svc.poll(pairing.pair_id)
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
    status = svc.poll(pairing.pair_id)
    assert status is not None
    assert status.paired is False
    assert status.token is None


async def test_poll_unknown_is_none(svc):
    assert svc.poll("nope") is None


async def test_pending_pairing_expires(svc, clock):
    pairing = await svc.start_pairing()
    clock.now += PAIRING_TTL_SECONDS + 1
    assert svc.poll(pairing.pair_id) is None
    with pytest.raises(DashboardPairingCodeInvalidError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="Kitchen")


async def test_oldest_pending_is_evicted(svc):
    first = await svc.start_pairing()
    for _ in range(MAX_PENDING_PAIRINGS):
        await svc.start_pairing()
    assert svc.poll(first.pair_id) is None
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
    status = svc.poll(pairing.pair_id)
    assert status is not None and status.paired
    assert status.display_id == display.id
    assert status.token is not None and len(status.token) >= 43
    # Exactly once: the entry is gone after the token was handed out.
    assert svc.poll(pairing.pair_id) is None
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
    for _ in range(THROTTLE_MAX_FAILURES):
        with pytest.raises(DashboardPairingCodeInvalidError):
            await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")
    # Even the right code waits now — guesses can't race the limit.
    with pytest.raises(DashboardPairingThrottledError) as exc:
        await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    assert exc.value.params["retry_after"] > 0
    # Never the login brake: tablets still sign in.
    assert auth.global_failure_count == 0


async def test_right_code_clears_the_failures(svc, auth):
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        with pytest.raises(DashboardPairingCodeInvalidError):
            await svc.claim(actor_is_admin=True, code="ZZZZZZ", name="K")
    await _pair(svc)
    assert auth.throttle_key_count == 0


async def test_display_limit(svc):
    for i in range(MAX_DISPLAYS):
        await _pair(svc, name=f"P{i}")
    pairing = await svc.start_pairing()
    with pytest.raises(DashboardDisplayLimitError):
        await svc.claim(actor_is_admin=True, code=pairing.code, name="One more")
    # Nothing to collect for the refused panel.
    status = svc.poll(pairing.pair_id)
    assert status is not None and not status.paired


async def test_paired_token_pickup_gets_a_fresh_window(svc, clock):
    pairing = await svc.start_pairing()
    clock.now += PAIRING_TTL_SECONDS - 5
    await svc.claim(actor_is_admin=True, code=pairing.code, name="K")
    clock.now += 60
    status = svc.poll(pairing.pair_id)
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
    renamed = await svc.rename(actor_is_admin=True, display_id=d.id, name=" Hall ")
    assert renamed.name == "Hall"
    with pytest.raises(PermissionError):
        await svc.rename(actor_is_admin=False, display_id=d.id, name="x")
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.rename(actor_is_admin=True, display_id=d.id, name="")
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.rename(actor_is_admin=True, display_id="nope", name="x")


async def test_remove(svc):
    d, _ = await _pair(svc)
    with pytest.raises(PermissionError):
        await svc.remove(actor_is_admin=False, display_id=d.id)
    await svc.remove(actor_is_admin=True, display_id=d.id)
    assert await svc.list(actor_is_admin=True) == ()
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.remove(actor_is_admin=True, display_id=d.id)

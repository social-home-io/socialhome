"""Per-peer memory of the binary media leg on the HTTPS inbox (v_81).

A peer whose inbox keeps refusing binary bodies (a proxy that blocks them,
a bug, the shared 600/min bucket behind Home Assistant's relay) must not
cost a wasted ~513 KiB POST before every JSON chunk forever.
"""

from __future__ import annotations

import pytest

from socialhome.federation.media_https_backoff import (
    MEDIA_HTTPS_DISABLE_S,
    MEDIA_HTTPS_MAX_PEERS,
    MEDIA_HTTPS_RETRY_AFTER_DEFAULT_S,
    MEDIA_HTTPS_RETRY_AFTER_MAX_S,
    MEDIA_HTTPS_STRIKES,
    MediaHttpsBackoff,
    MediaPostResult,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _backoff() -> tuple[MediaHttpsBackoff, _Clock]:
    clock = _Clock()
    return MediaHttpsBackoff(clock=clock), clock


def test_a_fresh_peer_gets_the_leg():
    b, _ = _backoff()
    assert b.allows("p")


@pytest.mark.parametrize("status", [None, 500, 502, 503])
def test_transient_failures_disable_the_leg_after_n_strikes(status):
    b, clock = _backoff()
    for _ in range(MEDIA_HTTPS_STRIKES - 1):
        b.record("p", MediaPostResult(ok=False, status_code=status))
        assert b.allows("p")
    b.record("p", MediaPostResult(ok=False, status_code=status))
    assert not b.allows("p")
    clock.now += MEDIA_HTTPS_DISABLE_S - 1
    assert not b.allows("p")
    clock.now += 1
    assert b.allows("p")  # probe again after the window


@pytest.mark.parametrize("status", [400, 403, 404, 413, 415])
def test_a_refusing_4xx_disables_the_leg_at_once(status):
    b, clock = _backoff()
    b.record("p", MediaPostResult(ok=False, status_code=status))
    assert not b.allows("p")
    clock.now += MEDIA_HTTPS_DISABLE_S
    assert b.allows("p")


def test_a_failed_probe_disables_again_at_once():
    """After the window one probe is allowed; if it fails too, the leg is
    off for another window — not N more wasted chunks."""
    b, clock = _backoff()
    for _ in range(MEDIA_HTTPS_STRIKES):
        b.record("p", MediaPostResult(ok=False, status_code=503))
    clock.now += MEDIA_HTTPS_DISABLE_S
    assert b.allows("p")
    b.record("p", MediaPostResult(ok=False, status_code=503))
    assert not b.allows("p")


def test_a_success_forgets_the_strikes():
    b, _ = _backoff()
    for _ in range(MEDIA_HTTPS_STRIKES - 1):
        b.record("p", MediaPostResult(ok=False, status_code=503))
    b.record("p", MediaPostResult(ok=True, status_code=200))
    for _ in range(MEDIA_HTTPS_STRIKES - 1):
        b.record("p", MediaPostResult(ok=False, status_code=503))
    assert b.allows("p")


def test_a_429_backs_off_for_its_retry_after_without_a_strike():
    b, clock = _backoff()
    b.record("p", MediaPostResult(ok=False, status_code=429, retry_after_s=7))
    assert not b.allows("p")
    clock.now += 7
    assert b.allows("p")
    # Not a strike: N-1 transient failures later the leg is still on.
    for _ in range(MEDIA_HTTPS_STRIKES - 1):
        b.record("p", MediaPostResult(ok=False, status_code=503))
    assert b.allows("p")


def test_a_429_without_retry_after_backs_off_briefly():
    b, clock = _backoff()
    b.record("p", MediaPostResult(ok=False, status_code=429))
    assert not b.allows("p")
    clock.now += MEDIA_HTTPS_RETRY_AFTER_DEFAULT_S
    assert b.allows("p")


def test_a_429_retry_after_is_capped():
    b, clock = _backoff()
    b.record("p", MediaPostResult(ok=False, status_code=429, retry_after_s=10**9))
    clock.now += MEDIA_HTTPS_RETRY_AFTER_MAX_S
    assert b.allows("p")


def test_peers_are_independent():
    b, _ = _backoff()
    b.record("p", MediaPostResult(ok=False, status_code=413))
    assert not b.allows("p")
    assert b.allows("q")


def test_memory_is_bounded():
    b, _ = _backoff()
    for i in range(MEDIA_HTTPS_MAX_PEERS + 10):
        b.record(f"p{i}", MediaPostResult(ok=False, status_code=413))
    assert len(b) == MEDIA_HTTPS_MAX_PEERS
    assert b.allows("p0")  # the oldest entry was dropped
    assert not b.allows(f"p{MEDIA_HTTPS_MAX_PEERS + 9}")

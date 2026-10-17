"""Tests for the Hybrid Logical Clock (HLC) primitive."""

from __future__ import annotations

from socialhome.infrastructure.hlc import HLC, HLC_MAX_DRIFT_MS

# ─── Ordering ──────────────────────────────────────────────────────────────


def test_total_order_counter_then_physical():
    assert HLC(5, 0) < HLC(5, 1)
    assert HLC(5, 1) < HLC(6, 0)
    assert HLC(5, 0) < HLC(6, 0)


def test_equal_compares_equal():
    assert HLC(5, 2) == HLC(5, 2)
    assert HLC(5, 2) <= HLC(5, 2)
    assert HLC(5, 2) >= HLC(5, 2)
    assert not (HLC(5, 2) < HLC(5, 2))


def test_str_round_trip():
    h = HLC(123, 4)
    assert str(h) == "123-4"
    assert HLC.parse(str(h)) == h


def test_parse_fail_soft():
    zero = HLC(0, 0)
    assert HLC.parse("") == zero
    assert HLC.parse("x") == zero
    assert HLC.parse(None) == zero
    assert HLC.parse("5-") == zero
    assert HLC.parse(42) == zero
    assert HLC.parse(object()) == zero


def test_parse_passthrough_hlc():
    h = HLC(7, 3)
    assert HLC.parse(h) is h


# ─── tick: monotonic ─────────────────────────────────────────────────────


def test_tick_strictly_greater():
    h = HLC(100, 0)
    assert h.tick(200) > h
    assert h.tick(100) > h
    assert h.tick(50) > h  # clock went backwards, still advances


def test_tick_clock_backwards_advances_counter():
    # now_ms below current physical → physical held, counter bumped.
    h = HLC(100, 5)
    advanced = h.tick(50)
    assert advanced == HLC(100, 6)
    assert advanced > h


def test_tick_counter_bump_same_now():
    assert HLC(0, 0).tick(100).tick(100) == HLC(100, 1)


def test_tick_advances_physical_resets_counter():
    assert HLC(100, 5).tick(200) == HLC(200, 0)


# ─── merge: causality ───────────────────────────────────────────────────


def test_merge_remote_ahead_adopts_remote():
    local = HLC(100, 0)
    remote = HLC(200, 3)
    merged = local.merge(remote, now_ms=150)
    # remote physical 200 wins (above local + now); counter+1.
    assert merged == HLC(200, 4)
    assert merged > local
    assert merged > remote


def test_merge_remote_behind_keeps_local_advances():
    local = HLC(200, 2)
    remote = HLC(100, 9)
    merged = local.merge(remote, now_ms=150)
    # local physical 200 wins; counter advances.
    assert merged == HLC(200, 3)
    assert merged > local
    assert merged > remote


def test_merge_all_equal_physical_max_counter():
    local = HLC(200, 4)
    remote = HLC(200, 9)
    merged = local.merge(remote, now_ms=200)
    assert merged == HLC(200, 10)
    assert merged > local
    assert merged > remote


def test_merge_wall_clock_ahead_resets_counter():
    local = HLC(100, 4)
    remote = HLC(120, 7)
    merged = local.merge(remote, now_ms=300)
    # now_ms is strictly greatest → fresh physical, counter 0.
    assert merged == HLC(300, 0)
    assert merged > local
    assert merged > remote


# ─── merge: drift clamp ──────────────────────────────────────────────────


def test_merge_clamps_far_future_remote():
    now = 1_000
    drift = 50
    remote = HLC(now + 10**9, 0)
    merged = HLC(0, 0).merge(remote, now_ms=now, max_drift_ms=drift)
    assert merged.physical_ms == now + drift
    assert merged.physical_ms != remote.physical_ms


def test_default_max_drift_constant():
    assert HLC_MAX_DRIFT_MS == 300_000


# ─── determinism / convergence ───────────────────────────────────────────


def test_two_node_causal_convergence():
    # Node A and Node B apply the same causal sequence and stay comparable.
    a = HLC(0, 0)
    b = HLC(0, 0)

    # A ticks twice locally.
    a = a.tick(10)
    a = a.tick(20)
    # B observes A's latest, then ticks.
    b = b.merge(a, now_ms=20)
    b = b.tick(25)
    # A observes B.
    a2 = a.merge(b, now_ms=25)

    # Causal order preserved: every observed-before event is strictly less.
    assert a < b  # A's send precedes B's later state
    assert b < a2  # B's send precedes A's merge
    # Determinism: replaying the identical sequence yields identical HLCs.
    a_r = HLC(0, 0).tick(10).tick(20)
    b_r = HLC(0, 0).merge(a_r, now_ms=20).tick(25)
    assert a_r == a
    assert b_r == b

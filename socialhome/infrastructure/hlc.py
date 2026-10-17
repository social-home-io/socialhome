"""Hybrid Logical Clock (HLC) — a (physical_ms, counter) timestamp that is
monotonic per node and causally consistent across nodes. Used to tie-break
config edits that collide at the same config_sequence (see
federation_inbound_service config LWW). Pure + clock-injected for testability."""

from __future__ import annotations

from dataclasses import dataclass

#: Max future drift accepted from a remote HLC on merge (ms). Mirrors the
#: §24.11 inbound timestamp-skew bound so a remote clock can't run away.
HLC_MAX_DRIFT_MS = 300_000


@dataclass(slots=True, frozen=True, order=True)
class HLC:
    physical_ms: int = 0
    counter: int = 0

    def tick(self, now_ms: int) -> HLC:
        """Advance for a LOCAL event at wall-clock ``now_ms``."""
        p = max(self.physical_ms, now_ms)
        c = self.counter + 1 if p == self.physical_ms else 0
        return HLC(p, c)

    def merge(
        self, remote: HLC, now_ms: int, *, max_drift_ms: int = HLC_MAX_DRIFT_MS
    ) -> HLC:
        """Advance on RECEIVING ``remote``, clamping its physical to ``now+max_drift``."""
        rp = min(remote.physical_ms, now_ms + max_drift_ms)
        p = max(self.physical_ms, rp, now_ms)
        if p == self.physical_ms and p == rp:
            c = max(self.counter, remote.counter) + 1
        elif p == self.physical_ms:
            c = self.counter + 1
        elif p == rp:
            c = remote.counter + 1
        else:
            c = 0
        return HLC(p, c)

    def __str__(self) -> str:
        return f"{self.physical_ms}-{self.counter}"

    @classmethod
    def parse(cls, raw: object) -> HLC:
        """Fail-soft parse of ``"<physical>-<counter>"``; garbage / None → zero."""
        if isinstance(raw, HLC):
            return raw
        if not isinstance(raw, str):
            return cls()
        try:
            p_str, _, c_str = raw.partition("-")
            return cls(int(p_str), int(c_str))
        except ValueError, TypeError:
            return cls()

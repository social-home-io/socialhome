"""Jittered exponential backoff shared by the retry outboxes.

Three outbox drains reschedule a transiently-failed row with the same
math — :mod:`socialhome.services.media_transcode_service`,
:mod:`socialhome.services.dm_media_sync_service` and
:mod:`socialhome.services.space_media_sync_service`. They all persist
``next_attempt_at`` at SQLite's one-second granularity
(``strftime("%Y-%m-%d %H:%M:%S")``) and their repos pick rows with
``datetime(next_attempt_at) <= datetime('now')``.

That combination makes *full* jitter (``random.uniform(0, base)``)
wrong: any sample below one second truncates to the current second, so
the "backed-off" row is due again on the very next scheduler tick — no
backoff at all. For the first retry (``base`` 30 s) that is ~1 run in
30, and the retried work is expensive (a PyAV video transcode, a
multi-chunk blob push), which is exactly what backoff exists to avoid.

:func:`jittered_backoff_seconds` uses *equal* jitter instead — half the
window fixed, half random — so the delay is never below ``base / 2``,
and clamps to :data:`MIN_BACKOFF_SECONDS` as a belt-and-braces floor for
callers with a small base. This mirrors the floor the federation outbox
processor already keeps (``max(1.0, ...)`` in
:meth:`socialhome.infrastructure.outbox_processor.OutboxProcessor._delay_for`).
Spread across a swarm of simultaneously-failing rows is preserved — the
random half still de-synchronises their retries.
"""

from __future__ import annotations

import random


#: Hard floor on any computed backoff, in seconds. ``next_attempt_at``
#: is stored at one-second granularity, so a sub-second delay rounds to
#: "due now" and the row retries immediately.
MIN_BACKOFF_SECONDS: float = 1.0


def jittered_backoff_seconds(
    *,
    attempts: int,
    base_seconds: float,
    cap_seconds: float,
    min_seconds: float = MIN_BACKOFF_SECONDS,
) -> float:
    """Equal-jitter exponential backoff for retry attempt ``attempts``.

    ``attempts`` is 1-based (the delay before the *second* try is
    ``attempts=1``). The un-jittered window doubles per attempt from
    ``base_seconds``, clamped at ``cap_seconds``; the returned delay is
    uniform in ``[window / 2, window]`` and never below ``min_seconds``.
    """
    window = min(base_seconds * (2 ** (attempts - 1)), cap_seconds)
    half = window / 2
    return max(min_seconds, half + random.uniform(0, half))

"""ISO-8601 parsing helpers.

Three flavours, all tolerant of a trailing ``Z`` (UTC) since
``datetime.fromisoformat`` only accepts ``+00:00`` suffix form before
Python 3.11's broader tolerance.

* :func:`parse_iso8601_strict` — raises ``ValueError`` on malformed input.
  Use when a federated peer must have sent a well-formed timestamp
  (envelope timestamp, user-identity assertion issued_at, pairing
  expiry).
* :func:`parse_iso8601_optional` — returns ``None`` for empty / malformed
  input. Use when reading a nullable DB column.
* :func:`parse_iso8601_lenient` — returns ``datetime.now(UTC)`` for
  anything unparseable. Use only in the inbound federation pipeline,
  where a malformed peer-supplied timestamp should land locally as "now"
  rather than raising — the signature has already been verified by the
  time we reach the timestamp fallback.

And :func:`as_utc` — the one "a naive datetime means UTC" rule for
values already parsed.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso8601_strict(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting trailing ``Z`` for UTC.

    Raises :class:`ValueError` on malformed input.
    """
    return datetime.fromisoformat(value)


def parse_iso8601_optional(value: str | None) -> datetime | None:
    """Like :func:`parse_iso8601_strict` but returns ``None`` for empty
    or malformed input rather than raising.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_iso8601_lenient(value: object) -> datetime:
    """Parse an ISO-8601 string, returning ``datetime.now(UTC)`` on failure.

    Accepts ``object`` so callers can pass arbitrary JSON-decoded values
    (strings, ``None``, numbers) without a pre-check. The fallback is
    intentional for the inbound federation pipeline — a peer-sent
    timestamp that round-trips badly is a soft error, not a hard one,
    because the envelope signature has already been verified.
    """
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """``value`` as a tz-aware UTC datetime; a naive one is taken as UTC
    already (the shape SQLite ``datetime('now')`` stamps parse to)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

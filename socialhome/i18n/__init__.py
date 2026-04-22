"""Backend i18n — minimal gettext-style message catalog (§30).

Only a tiny surface area is needed on the server: the
:class:`~socialhome.services.notification_service.NotificationService`
formats notification titles per recipient locale; the WebSocket layer
sometimes echoes server-generated text back to the client.

We do **not** translate API error messages — those are
machine-readable codes (e.g. ``"NOT_FOUND"``) which the client
translates locally. Only human-facing strings live in the catalog.

Catalog format: one ``messages/{locale}.json`` per supported locale,
each a flat ``{key: translation}`` map.  ``en`` is the source-of-truth
fallback.

Lookup falls back to the source language when the key (or the locale
file) is missing — never raises in production. Missing strings are
logged at DEBUG so they show up during dev but don't spam logs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Formatter

log = logging.getLogger(__name__)


_DEFAULT_LOCALE = "en"


class Catalog:
    """In-memory translation catalog.

    Construct via :meth:`from_directory` to load every
    ``{locale}.json`` file; or construct empty and call
    :meth:`load_locale` directly (useful in tests).
    """

    __slots__ = ("_messages", "_default")

    def __init__(self, default_locale: str = _DEFAULT_LOCALE) -> None:
        self._messages: dict[str, dict[str, str]] = {default_locale: {}}
        self._default = default_locale

    # ─── Loading ──────────────────────────────────────────────────────────

    @classmethod
    def from_directory(
        cls, path: str | Path, *, default_locale: str = _DEFAULT_LOCALE
    ) -> "Catalog":
        """Load every ``{locale}.json`` file under ``path``."""
        catalog = cls(default_locale=default_locale)
        d = Path(path)
        if not d.exists():
            return catalog
        for f in d.glob("*.json"):
            locale = f.stem
            try:
                catalog._messages[locale] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("i18n: failed to load %s: %s", f, exc)
        return catalog

    def load_locale(self, locale: str, messages: dict[str, str]) -> None:
        """Replace the message map for *locale*."""
        self._messages[locale] = dict(messages)

    # ─── Lookup ───────────────────────────────────────────────────────────

    def gettext(self, key: str, *, locale: str | None = None, **fmt_args) -> str:
        """Translate *key* and substitute ``{name}`` placeholders.

        Falls back to the default locale when:

        * the requested locale is not loaded, or
        * the key is missing in the requested locale.

        If the key isn't in the default locale either, returns the raw
        ``key`` unchanged so the UI still shows something useful.
        """
        chosen = locale or self._default
        msg = self._messages.get(chosen, {}).get(key)
        if msg is None and chosen != self._default:
            msg = self._messages.get(self._default, {}).get(key)
        if msg is None:
            log.debug("i18n: missing key %r (locale=%s)", key, chosen)
            msg = key

        if not fmt_args:
            return msg
        try:
            return Formatter().vformat(msg, (), fmt_args)
        except (KeyError, IndexError) as exc:
            log.debug("i18n: format substitution failed for %r: %s", key, exc)
            return msg

    @property
    def loaded_locales(self) -> list[str]:
        return sorted(self._messages.keys())

    @property
    def default_locale(self) -> str:
        return self._default

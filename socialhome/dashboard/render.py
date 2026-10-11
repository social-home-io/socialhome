"""Server-side rendering for the household dashboard (Jinja2).

Templates live in ``templates/`` next to this module and are loaded when
the renderer is built (startup, not inside a request). Autoescaping is
always on and undefined variables raise, so a typo can't silently render
an empty string. Strings come from the backend i18n catalog; templates
call ``t("dashboard.…", name=value)``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import jinja2

from ..i18n import Catalog

log = logging.getLogger(__name__)

__all__ = ["RTL_LOCALES", "TEMPLATE_DIR", "DashboardRenderer", "text_direction"]

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Locales written right-to-left. None of the shipped dashboard locales
#: are, but the ``dir`` hook is wired so adding one needs no template edit.
RTL_LOCALES = frozenset({"ar", "fa", "he", "ur"})


def text_direction(locale: str) -> str:
    """``"rtl"`` or ``"ltr"`` for the ``<html dir>`` attribute."""
    return "rtl" if locale.split("-", 1)[0] in RTL_LOCALES else "ltr"


class DashboardRenderer:
    """Renders dashboard pages in a given locale."""

    __slots__ = ("_env", "_catalog")

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Compile every template now — a broken template fails at startup,
        # and no request ever touches the filesystem for one.
        for name in self._env.list_templates(extensions=["j2"]):
            self._env.get_template(name)

    def gettext(self, key: str, locale: str, **fmt: object) -> str:
        return self._catalog.gettext(key, locale=locale, **fmt)

    def _translator(self, locale: str) -> Callable[..., str]:
        def t(key: str, **fmt: object) -> str:
            return self._catalog.gettext(key, locale=locale, **fmt)

        return t

    def render(self, template: str, *, locale: str, **context: object) -> str:
        """Render ``template`` with ``context`` plus ``lang`` / ``dir`` / ``t``."""
        return self._env.get_template(template).render(
            lang=locale,
            dir=text_direction(locale),
            t=self._translator(locale),
            **context,
        )

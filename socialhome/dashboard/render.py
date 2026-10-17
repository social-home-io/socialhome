"""Server-side rendering for the household dashboard (Jinja2) + its assets.

Templates (``templates/``) and static files (``static/``) next to this
module are read once, when the renderer is built at wiring time — never
inside a request or the server's async start path (``auto_reload`` is
off, so Jinja doesn't even stat a template again). Autoescaping is
always on and undefined variables raise, so a typo can't silently render
an empty string. Strings come from the backend i18n catalog; templates
call ``t("dashboard.…", name=value)``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import mimetypes
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import jinja2

from ..i18n import Catalog

log = logging.getLogger(__name__)

__all__ = [
    "RTL_LOCALES",
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "DashboardRenderer",
    "StaticAsset",
    "compiled_environment",
    "load_static_assets",
    "text_direction",
]

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: Locales written right-to-left. None of the shipped dashboard locales
#: are, but the ``dir`` hook is wired so adding one needs no template edit.
RTL_LOCALES = frozenset({"ar", "fa", "he", "ur"})


def text_direction(locale: str) -> str:
    """``"rtl"`` or ``"ltr"`` for the ``<html dir>`` attribute."""
    return "rtl" if locale.split("-", 1)[0] in RTL_LOCALES else "ltr"


@dataclass(slots=True, frozen=True)
class StaticAsset:
    """One preloaded ``/static/`` file."""

    body: bytes
    content_type: str
    etag: str


def load_static_assets(directory: Path) -> dict[str, StaticAsset]:
    """Read every regular, non-hidden file directly in ``directory``.

    Sync on purpose: called once at wiring time, outside the event loop's
    request path. Serving from this map also makes path traversal moot —
    a name is either a key or a 404.
    """
    assets: dict[str, StaticAsset] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        assets[path.name] = StaticAsset(body=body, content_type=content_type, etag=etag)
    return assets


@functools.cache
def compiled_environment() -> jinja2.Environment:
    """The template environment, every template compiled — once per process.

    The templates are package data and ``auto_reload`` is off, so a
    compiled environment never goes stale; renderers share it (the locale
    and translator are per-render arguments, not environment state).
    Compiling all of them here keeps the fail-fast: a broken template fails
    the first renderer built, at startup, and no request ever touches the
    filesystem for one. Compiling is ~20 templates' worth of Jinja codegen,
    which every ``create_app`` used to pay again.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )
    for name in env.list_templates(extensions=["j2"]):
        env.get_template(name)
    return env


class DashboardRenderer:
    """Renders dashboard pages in a given locale."""

    __slots__ = ("_catalog", "_env", "_static")

    def __init__(self, catalog: Catalog, *, static_dir: Path = STATIC_DIR) -> None:
        self._catalog = catalog
        self._env = compiled_environment()
        self._static: Mapping[str, StaticAsset] = load_static_assets(static_dir)

    def static_asset(self, name: str) -> StaticAsset | None:
        """The preloaded ``/static/<name>`` file, or ``None``."""
        return self._static.get(name)

    def gettext(self, key: str, locale: str, **fmt: object) -> str:
        return self._catalog.gettext(key, locale=locale, **fmt)

    def translator(self, locale: str) -> Callable[..., str]:
        """``t(key, **fmt)`` bound to ``locale`` (what templates call)."""

        def t(key: str, **fmt: object) -> str:
            return self._catalog.gettext(key, locale=locale, **fmt)

        return t

    def render(self, template: str, *, locale: str, **context: object) -> str:
        """Render ``template`` with ``context`` plus ``lang`` / ``dir`` / ``t``."""
        return self._env.get_template(template).render(
            lang=locale,
            dir=text_direction(locale),
            t=self.translator(locale),
            **context,
        )

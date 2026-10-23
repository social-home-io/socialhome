"""orjson as a ``str`` — for aiohttp hooks that want text.

aiohttp's ``ClientSession(json_serialize=…)`` and
``web.json_response(dumps=…)`` default to the stdlib :mod:`json`; Social Home
hands them :func:`dumps_str` instead (compact, UTF-8, no ``ensure_ascii``
escaping). Non-``str`` dict keys (an ``int`` count bucket, say) become
strings as the stdlib does, rather than raising. Never for bytes a
signature or a hash covers — those go through :mod:`socialhome.canonical_json`.
"""

from __future__ import annotations

from typing import Any

import orjson


def dumps_str(obj: Any) -> str:
    """``obj`` as compact orjson text."""
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")

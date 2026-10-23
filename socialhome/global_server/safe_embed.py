"""Safe embedding of JSON into an HTML ``<script>`` block.

A JSON encoder does not escape ``<`` (or ``/``), so a value containing
``</script>`` breaks out of a ``<script type="application/json">`` block
and injects markup. The GFS public pages embed values a *paired* (but
not fully trusted) instance controls — a registration's ``user_id`` /
``instance_id``, a publish-supplied ``highlight_id`` — so embedding them
with bare JSON is a stored-XSS vector on the anonymous
landing pages.

Escape the HTML-significant characters (and the U+2028/U+2029 line
separators that are valid in JSON strings but break JS parsing) as JSON
unicode escapes: still valid JSON for ``JSON.parse``, inert as HTML.
"""

from __future__ import annotations

from typing import Any

import orjson

_SCRIPT_UNSAFE = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def script_json(obj: Any) -> str:
    """``obj`` as JSON (orjson: compact, UTF-8) hardened for inlining in a ``<script>`` block."""
    out = orjson.dumps(obj).decode()
    for ch, esc in _SCRIPT_UNSAFE.items():
        out = out.replace(ch, esc)
    return out

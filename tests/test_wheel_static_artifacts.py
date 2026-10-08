"""Guard the wheel-packaging allow-list for GFS vite build outputs.

The GFS Preact surfaces (``admin``, ``highlight_public_viewer``,
``moment_public_viewer``) are bundled by vite into
``socialhome/global_server/static/``. Those entry files are gitignored,
so they only reach the published wheel (and therefore the GFS Docker
image) via ``[tool.hatch.build].artifacts`` in ``pyproject.toml``.

vite factors the shared Preact runtime into a *separate* chunk that every
entry bundle imports with a top-level ``import ... from "./<name>.module-<hash>.js"``.
If that chunk is not in the artifacts allow-list it is silently dropped
from the wheel; the browser then 404s on the import and the whole SPA
fails to initialise (blank page).

This bit production once: the allow-list hardcoded ``hooks.module-*.js``
(the chunk's old name) while a vite/preset upgrade had renamed it to
``jsxRuntime.module-*.js`` — so a clean rebuild shipped ``admin.js``
without the chunk it imports. The fix globs the chunk name; this test
keeps any future chunk-name drift from re-breaking it.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Representative shared-chunk filenames vite has emitted across versions.
#: The allow-list must cover *any* ``<name>.module-<hash>.js`` so a future
#: rename (hooks → jsxRuntime → preact → …) can't drop the chunk again.
GFS_SHARED_CHUNK_SAMPLES = [
    "socialhome/global_server/static/jsxRuntime.module-DdiM6-lp.js",
    "socialhome/global_server/static/hooks.module-abc12345.js",
    "socialhome/global_server/static/preact.module-0Z9aA1.js",
    # The public viewers' shared error copy (``client/gfs/viewer_errors.ts``),
    # named into this family by ``manualChunks`` in ``vite.gfs.config.ts``.
    "socialhome/global_server/static/viewer.module-BjUB5M97.js",
]


def _build_artifacts() -> list[str]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["hatch"]["build"]["artifacts"]


def test_gfs_vite_shared_chunks_are_in_wheel_artifacts() -> None:
    """Every vite shared chunk must match an artifacts glob.

    fnmatch's ``*`` spans ``/``, matching hatchling's recursive-glob
    semantics closely enough for these flat ``static/`` paths.
    """
    artifacts = _build_artifacts()
    for chunk in GFS_SHARED_CHUNK_SAMPLES:
        assert any(fnmatch.fnmatch(chunk, pat) for pat in artifacts), (
            f"{chunk!r} is not covered by [tool.hatch.build].artifacts and "
            f"would be excluded from the wheel (→ 404 at runtime, blank SPA). "
            f"artifacts={artifacts}"
        )


def _gfs_vite_entries() -> list[str]:
    """The ``rollupOptions.input`` entry names in ``client/vite.gfs.config.ts``."""
    text = (REPO_ROOT / "client" / "vite.gfs.config.ts").read_text(encoding="utf-8")
    block = re.search(r"input:\s*\{(.*?)\}", text, re.S)
    assert block is not None, "no rollupOptions.input block in vite.gfs.config.ts"
    return re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M)


def test_gfs_vite_entry_bundles_are_ignored_and_in_wheel_artifacts() -> None:
    """Each GFS entry bundle is a build output: gitignored (so a stale copy
    can't be committed and shadow the build) and in the wheel artifacts (so
    the release build — ``pnpm run build`` then ``python -m build`` — ships
    it to the GFS image)."""
    entries = _gfs_vite_entries()
    assert {"highlight_public_viewer", "moment_public_viewer", "admin"} <= set(entries)
    artifacts = _build_artifacts()
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for name in entries:
        path = f"socialhome/global_server/static/{name}.js"
        assert any(fnmatch.fnmatch(path, pat) for pat in artifacts), path
        assert path in ignored, f"{path} must be gitignored (build output)"

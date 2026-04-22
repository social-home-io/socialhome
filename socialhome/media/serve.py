"""aiohttp media serving + upload handler.

The View classes live in :mod:`socialhome.routes.media` (to avoid
circular imports). This module re-exports them for backwards
compatibility with existing test imports.
"""

from __future__ import annotations

# Re-export for backwards compatibility with tests that import from here.
from ..routes.media import MediaServeView, MediaUploadView

__all__ = ["MediaServeView", "MediaUploadView"]

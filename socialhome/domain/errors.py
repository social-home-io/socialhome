"""Coded refusals — domain errors the API answers with a stable code.

A :class:`CodedError` carries everything :meth:`BaseView._iter` needs to
answer it, so a new refusal is a subclass and never a new ``except``
clause or handler branch:

* ``status`` — the HTTP status (kept per raise site, so turning a
  catch-all ``ValueError`` into a coded error never changes the status a
  client sees);
* ``code`` — the machine-readable code the SPA translates
  (``error.<code>`` in ``client/src/apiErrors.ts``);
* ``detail`` — a fixed English sentence for API clients. Never raw ids,
  library error text or other internals;
* ``params`` — flat, JSON-safe values the translated message needs
  (a price floor, an age, a length limit). Same rule: nothing internal.

Subclasses also inherit the exception type the raise site used before
(``SpacePermissionError``, ``PermissionError``…) so existing ``except``
clauses and callers keep working.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "CodedError",
    "GfsSelectionInvalidError",
    "GfsSelectionRequiredError",
    "ImageTooLargeError",
    "ImageUnreadableError",
    "InvalidMediaRefError",
    "PayloadTooLargeError",
    "SpaceNotPublishableError",
    "TierChangeRequiredError",
    "TranscodeQueueFullError",
    "VideoTooLongError",
    "VideoUnreadableError",
    "WrongTypeError",
]

ParamValue = str | int | float | bool


class CodedError(Exception):
    """A refusal with a stable code, a fixed English detail and safe params."""

    status: int = 422
    code: str = "UNPROCESSABLE"
    detail: str = "Request could not be processed."

    def __init__(
        self,
        detail: str | None = None,
        *,
        status: int | None = None,
        code: str | None = None,
        params: Mapping[str, ParamValue] | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.params: dict[str, ParamValue] = dict(params or {})
        super().__init__(self.detail)


class ImageTooLargeError(CodedError):
    """An uploaded picture (avatar, cover, icon) is over the size limit."""

    status = 422
    code = "IMAGE_TOO_LARGE"
    detail = "Upload exceeds size limit."

    def __init__(self, max_bytes: int) -> None:
        super().__init__(params={"max_mb": max(1, max_bytes // (1024 * 1024))})


class PayloadTooLargeError(CodedError):
    """A request body (or one multipart part) is over the route's cap.

    Raised by the streaming readers in :mod:`socialhome.hardening`
    (``read_body_capped`` / ``read_part_capped``) the moment the bytes
    read cross the cap — the rest of the body is never buffered. 413 like
    aiohttp's own ``client_max_size`` refusal, but with the canonical
    coded envelope so the SPA can tell the user the limit.
    """

    status = 413
    code = "PAYLOAD_TOO_LARGE"
    detail = "Upload exceeds size limit."

    def __init__(self, max_bytes: int) -> None:
        super().__init__(params={"max_mb": max(1, max_bytes // (1024 * 1024))})


class SpaceItemTooLargeError(CodedError, ValueError):
    """A page or a description too large to reach every member household.

    A household that reaches a space only through a GFS gets a large item
    as one relay frame, which carries about 495 KiB of text. Saving more
    into a space with such a household is refused up front (413) — it
    would show as saved here and never arrive there.
    """

    status = 413
    code = "SPACE_ITEM_TOO_LARGE"
    detail = (
        "This is too large for a space with households that reach it "
        "through a GFS. Shorten it or split it."
    )

    def __init__(self, max_bytes: int) -> None:
        super().__init__(params={"max_kb": max_bytes // 1024})


class DownloadTooLargeError(CodedError):
    """A gallery download asks for more photos or bytes than one ZIP holds.
    The SPA says how many fit (``max_items``) and how big (``max_mb``)."""

    status = 413
    code = "DOWNLOAD_TOO_LARGE"
    detail = "Too many photos for one download."

    def __init__(self, max_items: int, max_bytes: int) -> None:
        super().__init__(
            params={
                "max_items": max_items,
                "max_mb": max(1, max_bytes // (1024 * 1024)),
            }
        )


class ImageUnreadableError(CodedError, ValueError):
    """The image library could not open an uploaded picture. The library's
    own error text stays in the server log — it can name files and
    internals, so it never reaches the client."""

    status = 422
    code = "IMAGE_UNREADABLE"
    detail = "This image couldn't be opened."


class VideoTooLongError(CodedError, ValueError):
    """A video is longer than this place keeps (household gallery: 5 min).

    Refused, never cut: ``params`` carry the video's length and the limit
    (whole seconds) so the SPA can say "7:12 long — the gallery keeps
    videos up to 5 minutes".
    """

    status = 422
    code = "VIDEO_TOO_LONG"
    detail = "This video is longer than the gallery keeps."

    def __init__(self, duration_s: float, max_s: int) -> None:
        super().__init__(params={"duration_s": int(duration_s), "max_s": int(max_s)})


class VideoUnreadableError(CodedError, ValueError):
    """The video library could not open an uploaded video (no video
    stream, or not one frame decodes). Its own error text stays in the
    server log."""

    status = 422
    code = "VIDEO_UNREADABLE"
    detail = "This video couldn't be opened."


class TranscodeQueueFullError(CodedError):
    """Too many videos are waiting to be converted; try again later."""

    status = 503
    code = "TRANSCODE_QUEUE_FULL"
    detail = "Too many videos are waiting to be converted. Try again later."


class PageFilesNotShareableError(CodedError, ValueError):
    """A page edit adds pictures this household can't hand to the space's
    host (422 ``PAGE_FILES_NOT_SHAREABLE``): the host reaches us only through
    a GFS relay (we were seated from an invite link), and we hold no upload
    authorization for the space's GFS media cache (no writer key, writer
    cert or channel grant). The words would land and the pictures would
    stay broken for every member — the edit is refused instead (v_82)."""

    status = 422
    code = "PAGE_FILES_NOT_SHAREABLE"
    detail = "this household can't share pictures in this space yet"

    def __init__(self) -> None:
        super().__init__(self.detail)


class InvalidMediaRefError(CodedError, ValueError):
    """A media field that is not a local upload (422 ``INVALID_MEDIA_URL``).

    Only ``api/media/<name>`` — the shape ``POST /api/media/upload``
    returns — is stored for a field the SPA renders as an ``<img>`` /
    ``<video>`` source or a link target: a remote URL would leak every
    viewer's IP to its host (docs/principles.md "No third-party fetches
    from user content") and would be dropped by every receiving household
    anyway, a ``javascript:`` URL would be stored XSS. The detail names
    the field and the rule (``params.field`` carries the field name), never the
    submitted value.
    """

    status = 422
    code = "INVALID_MEDIA_URL"
    detail = "media_url must be a file uploaded via /api/media/upload"

    def __init__(self, field: str = "media_url") -> None:
        super().__init__(
            f"{field} must be a file uploaded via /api/media/upload",
            params={"field": field},
        )


class SpaceNotPublishableError(CodedError):
    """Only a global space may be listed on a GFS.

    A private or household space never leaves its member households, so
    publishing one would put its name, description and cover in a GFS
    directory. Raised before anything is sent to the GFS.
    """

    status = 409
    code = "SPACE_NOT_PUBLIC"
    detail = "Only global spaces can be listed on a GFS."


class TierChangeRequiredError(SpaceNotPublishableError):
    """A PUBLIC space is never listed on a GFS.

    Public means "discoverable by the households we are connected with";
    listing it on a GFS *is* making it global, which is a tier change the
    space's admins approve (the quorum proposal), never a side effect of a
    publish. Raised before anything is sent to the GFS. A subclass of
    :class:`SpaceNotPublishableError` so every caller that skips an
    unpublishable space skips this one too.
    """

    status = 409
    code = "TIER_CHANGE_REQUIRED"
    detail = (
        "A public space is only visible to connected households. "
        "Make it global to list it on a GFS."
    )


class GfsSelectionRequiredError(CodedError, ValueError):
    """A new global space must name at least one GFS to be listed on, as
    long as the household has an active GFS connection to choose from."""

    status = 422
    code = "GFS_SELECTION_REQUIRED"
    detail = "Choose at least one GFS to list this global space on."


class GfsSelectionInvalidError(CodedError, ValueError):
    """A GFS choice that can't be used. ``params.reason``:

    * ``unknown`` — not one of this household's ACTIVE GFS connections;
    * ``too_many`` — more than ``params.max`` servers;
    * ``not_global`` — servers were chosen for a space that isn't global;
    * ``malformed`` — ``gfs_ids`` is not a list of connection ids;
    * ``unsupported`` — a private space's channel server that does not
      prove ``private_channels``.
    """

    status = 422
    code = "GFS_SELECTION_INVALID"
    detail = "That GFS choice can't be used."

    def __init__(self, reason: str, *, max_servers: int | None = None) -> None:
        params: dict[str, ParamValue] = {"reason": reason}
        if max_servers is not None:
            params["max"] = max_servers
        super().__init__(params=params)


class WrongTypeError(TypeError, ValueError):
    """Malformed input: a field arrived with the wrong type.

    A ``TypeError`` by meaning, and still a ``ValueError`` so every caller
    that already catches malformed input as ``ValueError`` — and
    ``BaseView._iter``'s ``ValueError`` clause — answers it exactly as
    before (same status, same message).
    """

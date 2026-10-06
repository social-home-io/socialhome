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
    "ImageTooLargeError",
    "ImageUnreadableError",
    "PayloadTooLargeError",
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


class ImageUnreadableError(CodedError, ValueError):
    """The image library could not open an uploaded picture. The library's
    own error text stays in the server log — it can name files and
    internals, so it never reaches the client."""

    status = 422
    code = "IMAGE_UNREADABLE"
    detail = "This image couldn't be opened."

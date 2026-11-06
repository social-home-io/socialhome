"""Media routes — /api/media/* (file serving + upload)."""

from __future__ import annotations

import logging
import pathlib
import uuid

import aiofiles
import aiofiles.os
from aiohttp import web
from aiohttp.multipart import BodyPartReader

from ..app_keys import (
    config_key,
    gallery_service_key,
    media_signer_key,
    media_transcode_repo_key,
    media_transcode_service_key,
    space_media_gfs_fetcher_key,
    storage_quota_service_key,
)
from ..auth import SIGNED_URL_PRINCIPAL
from ..csp import media_response_headers, media_type_for
from ..domain.errors import CodedError
from ..domain.media_constraints import (
    AUDIO_ACCEPTED_MIMES,
    FILE_DENIED_EXTENSIONS,
    FILE_MAX_UPLOAD_BYTES,
    VIDEO_MAX_UPLOAD_BYTES,
)
from ..media.audio_processor import AudioProcessor
from ..media.image_processor import ImageProcessor, sniff_image_mime
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)

# Max raw upload size checked *before* processing (separate from the
# video processor's own max_input_bytes which gates the video path).
_DEFAULT_MAX_UPLOAD_BYTES = VIDEO_MAX_UPLOAD_BYTES

#: Loose detector for the "this is an image" path. Routes the upload
#: through :class:`ImageProcessor` (which then runs its own strict
#: magic-byte + Pillow check). Outside this set the upload either
#: takes the video path or — once the spec opened ``type='file'`` —
#: the passthrough path.
_IMAGE_HINT_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
    },
)
_IMAGE_HINT_EXTS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"},
)
_VIDEO_HINT_MIMES: frozenset[str] = frozenset(
    {"video/mp4", "video/webm", "video/quicktime"},
)
_VIDEO_HINT_EXTS: frozenset[str] = frozenset({".mp4", ".webm", ".mov"})
#: Audio routes the upload through :class:`AudioProcessor` — same
#: pattern as the image/video branches. Anything that hints "audio"
#: (any ``audio/*`` MIME or a recognisable extension) takes this
#: branch; the processor itself enforces OGG/Opus and rejects
#: everything else, so a mislabelled ``.mp3`` lands a clear 422
#: instead of slipping into the generic file passthrough.
_AUDIO_HINT_EXTS: frozenset[str] = frozenset(
    {".ogg", ".oga", ".opus", ".weba", ".m4a", ".aac"},
)


def _sanitise_file_ext(filename: str) -> str:
    """Pick a safe extension from ``filename`` for the stored name.

    Returns a lowercase ``".ext"`` (with the leading dot) or an
    empty string when the source carried no recognisable extension.
    Only ``a-z 0-9`` characters in the extension; up to 8 chars
    long. ``filename`` is the un-trusted upload field — never used
    on disk verbatim.
    """
    suffix = pathlib.Path(filename).suffix.lower()
    if not suffix or len(suffix) > 9:  # incl. leading dot
        return ""
    cleaned = "".join(c for c in suffix[1:] if c.isalnum())
    if not cleaned:
        return ""
    return f".{cleaned[:8]}"


class MediaServeView(BaseView):
    """``GET /api/media/{filename}`` — stream a media file.

    Auth is enforced upstream of this view by either
    :class:`SignedMediaStrategy` (browser ``<img>``/``<video>``/download
    links carrying ``?exp=&sig=``) or :class:`BearerTokenStrategy` (any
    ``fetch()`` carrying ``Authorization: Bearer …``). Either populates
    ``request['user']`` by the time the handler runs.

    A space file this household holds only as a ``media_refs`` descriptor
    (v_80 — it reached us through a GFS without its bytes) is fetched from
    the GFS media cache first (:meth:`SpaceMediaGfsFetcher.ensure_local`:
    one fetch per file however many viewers wait, bounded by a timeout);
    404 when that fails.
    """

    async def get(self) -> web.StreamResponse:
        config = self.svc(config_key)
        filename = self.match("filename")

        # Prevent path traversal
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return error_response(400, "BAD_REQUEST", "Invalid filename.")

        file_path = pathlib.Path(config.media_path) / filename
        # Access first (review H): a viewer who may not open a file never
        # makes this household fetch it from a GFS either.
        if not await self._may_open(filename):
            # A private album's photo (D7) for someone outside it: exactly
            # the unknown file's answer, so nobody learns it exists.
            return error_response(404, "NOT_FOUND", "Media file not found.")
        if not await aiofiles.os.path.isfile(file_path):
            fetcher = self.request.app.get(space_media_gfs_fetcher_key)
            if fetcher is None or not await fetcher.ensure_local(filename):
                return error_response(404, "NOT_FOUND", "Media file not found.")
        gallery = self.request.app.get(gallery_service_key)
        # A private album's file is never kept in a browser or proxy cache:
        # someone taken out of the album must not find it there (a signed
        # link they already hold still works until it expires, ≤ 7 h).
        private = gallery is not None and await gallery.is_private_file(filename)

        # The type is guessed from the stored name, so a stored ``.svg`` /
        # ``.html`` would otherwise render as a document on our origin.
        # ``media_response_headers`` downgrades every script-capable or
        # unknown type to an octet-stream attachment and sandboxes the rest.
        content_type = media_type_for(filename)
        stat_result = await aiofiles.os.stat(file_path)
        headers = {
            **media_response_headers(content_type, filename),
            "Content-Length": str(stat_result.st_size),
            "Cache-Control": "private, no-store"
            if private
            else "private, max-age=86400",
        }

        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(self.request)

        async with aiofiles.open(file_path, "rb") as fh:
            while True:
                chunk = await fh.read(64 * 1024)
                if not chunk:
                    break
                await response.write(chunk)

        await response.write_eof()
        return response

    async def _may_open(self, filename: str) -> bool:
        """A signed URL was handed out by a route that already checked who
        sees the file (it names one file, for a few hours); a bearer caller
        is checked here against the gallery's private albums."""
        user_id = self.user.user_id
        if user_id == SIGNED_URL_PRINCIPAL:
            return True
        gallery = self.request.app.get(gallery_service_key)
        if gallery is None:
            return True
        return await gallery.may_open_file(filename, user_id=user_id)


class MediaUploadView(BaseView):
    """``POST /api/media/upload`` — accept multipart upload and process."""

    async def post(self) -> web.Response:
        self.require_user()

        config = self.svc(config_key)
        quota = self.request.app.get(storage_quota_service_key)

        if not self.request.content_type.startswith("multipart/"):
            return error_response(400, "BAD_REQUEST", "Expected multipart/form-data.")

        try:
            reader = await self.request.multipart()
        except Exception as exc:
            log.warning("media upload: multipart parse error: %s", exc, exc_info=True)
            return error_response(400, "BAD_REQUEST", "Malformed multipart body.")

        field = await reader.next()
        if field is None:
            return error_response(400, "BAD_REQUEST", "No file field in upload.")
        if not isinstance(field, BodyPartReader):
            return error_response(400, "BAD_REQUEST", "Expected file part.")

        filename = field.filename or "upload"
        chunks: list[bytes] = []
        total = 0

        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _DEFAULT_MAX_UPLOAD_BYTES:
                return error_response(
                    413, "PAYLOAD_TOO_LARGE", "Upload exceeds size limit."
                )
            chunks.append(chunk)

        data = b"".join(chunks)
        # Pre-check the household storage quota before disk write (§18 /
        # §5.2). ``check_can_store`` raises ``StorageQuotaExceeded`` which
        # ``BaseView._iter`` maps to HTTP 507 STORAGE_FULL.
        if quota is not None and total > 0:
            await quota.check_can_store(total)
        content_type = field.headers.get("Content-Type", "")
        lower_ext = pathlib.Path(filename).suffix.lower()

        # Audio detection runs FIRST so a Chromium ``voice-note.webm``
        # blob (audio/webm; codecs=opus, ``.webm`` extension) takes the
        # audio branch instead of being mis-routed to the video
        # processor — the ``.webm`` extension alone matches the video
        # hint set otherwise.
        is_audio = (
            content_type in AUDIO_ACCEPTED_MIMES
            or content_type.startswith("audio/")
            or lower_ext in _AUDIO_HINT_EXTS
        )
        is_video = not is_audio and (
            content_type in _VIDEO_HINT_MIMES or lower_ext in _VIDEO_HINT_EXTS
        )
        # A picture whose type and name don't say (a phone picker's
        # content URI: ``application/octet-stream``, no extension) is
        # still a picture: its first bytes decide, and it is converted
        # like any other — never stored as the original.
        is_image = not is_audio and (
            content_type in _IMAGE_HINT_MIMES
            or lower_ext in _IMAGE_HINT_EXTS
            or (not is_video and sniff_image_mime(data) is not None)
        )

        # ── Async video path ────────────────────────────────────────────
        # Video transcode is the slowest, most CPU-heavy branch — running
        # it inline blocks the request for seconds. Instead we stash the
        # raw source bytes in the (non-served) ``transcode_src`` temp dir,
        # enqueue one ``media_transcode_jobs`` row keyed by the eventual
        # ``.webm`` output filename, and return 201 with
        # ``media_status='processing'`` immediately. The background
        # :class:`MediaTranscodeService` decodes the source, writes the
        # ``.webm`` + ``.webp`` poster, and clears the row. Size + MIME
        # were already validated above (``_DEFAULT_MAX_UPLOAD_BYTES`` /
        # quota), so the enqueue can't be abused as an unbounded sink.
        if is_video:
            return await self._enqueue_video(config, data)

        try:
            out_bytes: bytes
            out_name: str
            if is_audio:
                a_proc = AudioProcessor()
                out_bytes, out_name = await a_proc.process(data, filename)
            elif is_image:
                processor = ImageProcessor()
                out_bytes, out_name = await processor.process(data, filename)
            else:
                # Generic file passthrough — DMs accept ``type='file'``
                # for PDFs / docs / archives that don't transcode to a
                # smaller form. ``ImageProcessor`` / ``VideoProcessor``
                # would 422 on these; instead we apply a separate
                # (smaller) size cap, deny the obvious executable
                # extensions, and write the bytes as-is.
                if total > FILE_MAX_UPLOAD_BYTES:
                    return error_response(
                        413,
                        "PAYLOAD_TOO_LARGE",
                        f"File exceeds the {FILE_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB cap.",
                    )
                if lower_ext in FILE_DENIED_EXTENSIONS:
                    return error_response(
                        422,
                        "UNPROCESSABLE",
                        f"File type {lower_ext!r} isn't allowed.",
                    )
                out_name = f"{uuid.uuid4().hex}{_sanitise_file_ext(filename)}"
                out_bytes = data
        except CodedError:
            # ``ImageUnreadableError`` is also a ``ValueError`` — let
            # ``BaseView._iter`` answer it with its stable code.
            raise
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except RuntimeError as exc:
            log.error("media upload: processor runtime error: %s", exc)
            return error_response(503, "SERVICE_UNAVAILABLE", str(exc))

        media_dir = pathlib.Path(config.media_path)
        await aiofiles.os.makedirs(media_dir, exist_ok=True)
        dest = media_dir / out_name
        async with aiofiles.open(dest, "wb") as f:
            await f.write(out_bytes)

        url = f"api/media/{out_name}"
        # The composer needs a URL it can drop straight into ``<img src>``
        # for the local preview — that requires the short-lived
        # signature. We return both forms: ``url`` is the canonical one
        # the client sends back when creating the post (server signs
        # again at every read), ``signed_url`` is for immediate display.
        signer = self.request.app.get(media_signer_key)
        signed_url = signer.sign(url) if signer is not None else url
        return web.json_response(
            {"url": url, "filename": out_name, "signed_url": signed_url},
            status=201,
        )

    async def _enqueue_video(self, config, data: bytes) -> web.Response:
        """Stash source bytes + enqueue a background transcode job.

        Returns 201 with the future ``.webm`` URL and
        ``media_status='processing'`` — the files don't exist yet, so
        signing the (not-yet-present) path is fine; every read re-signs.
        The source bytes go under ``media_dir/transcode_src`` which the
        serve route can never reach (it rejects any filename containing
        ``/``), so the raw upload is never publicly fetchable.
        """
        media_dir = pathlib.Path(config.media_path)
        # ONE shared UUID stem for the transcoded ``.webm`` and its
        # ``.webp`` poster so the poster path is derivable from the
        # media URL server-side (feed/DM/moment rows store only the
        # ``.webm`` and have no thumbnail column).
        stem = uuid.uuid4().hex
        output_filename = f"{stem}.webm"
        thumbnail_filename = f"{stem}.webp"

        temp_dir = media_dir / "transcode_src"
        await aiofiles.os.makedirs(temp_dir, exist_ok=True)
        temp_path = temp_dir / f"{uuid.uuid4().hex}.bin"
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(data)

        repo = self.svc(media_transcode_repo_key)
        await repo.enqueue(
            output_filename=output_filename,
            source_path=str(temp_path),
            thumbnail_filename=thumbnail_filename,
            owner_user_id=self.user.user_id,
        )
        self.svc(media_transcode_service_key).nudge()

        url = f"api/media/{output_filename}"
        thumbnail_url = f"api/media/{thumbnail_filename}"
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            url_signed = signer.sign(url)
            thumb_signed = signer.sign(thumbnail_url)
        else:
            url_signed = url
            thumb_signed = thumbnail_url
        return web.json_response(
            {
                "url": url,
                "thumbnail_url": thumbnail_url,
                "filename": output_filename,
                "media_status": "processing",
                "signed_url": url_signed,
                "signed_thumbnail_url": thumb_signed,
            },
            status=201,
        )

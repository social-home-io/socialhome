"""Media routes — /api/media/* (file serving + upload)."""

from __future__ import annotations

import logging
import mimetypes
import pathlib

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from ..app_keys import config_key, storage_quota_service_key
from ..domain.media_constraints import VIDEO_MAX_UPLOAD_BYTES
from ..media.image_processor import ImageProcessor
from ..media.video_processor import VideoProcessor
from ..security import error_response
from .base import BaseView

log = logging.getLogger(__name__)

# Max raw upload size checked *before* processing (separate from the
# video processor's own max_input_bytes which gates the video path).
_DEFAULT_MAX_UPLOAD_BYTES = VIDEO_MAX_UPLOAD_BYTES


class MediaServeView(BaseView):
    """``GET /api/media/{filename}`` — stream a media file."""

    async def get(self) -> web.StreamResponse:
        self.user  # auth check

        config = self.svc(config_key)
        filename = self.match("filename")

        # Prevent path traversal
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return error_response(400, "BAD_REQUEST", "Invalid filename.")

        file_path = pathlib.Path(config.media_path) / filename
        if not file_path.exists() or not file_path.is_file():
            return error_response(404, "NOT_FOUND", "Media file not found.")

        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"

        headers = {
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(file_path.stat().st_size),
            "Cache-Control": "private, max-age=86400",
        }

        response = web.StreamResponse(
            status=200,
            headers={**headers, "Content-Type": content_type},
        )
        await response.prepare(self.request)

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                await response.write(chunk)

        await response.write_eof()
        return response


class MediaUploadView(BaseView):
    """``POST /api/media/upload`` — accept multipart upload and process."""

    async def post(self) -> web.Response:
        self.user  # auth check

        config = self.svc(config_key)
        quota = self.request.app.get(storage_quota_service_key)

        if not self.request.content_type.startswith("multipart/"):
            return error_response(400, "BAD_REQUEST", "Expected multipart/form-data.")

        try:
            reader = await self.request.multipart()
        except Exception as exc:
            log.warning("media upload: multipart parse error: %s", exc)
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

        is_video = content_type in (
            "video/mp4",
            "video/webm",
            "video/quicktime",
        ) or filename.lower().endswith((".mp4", ".webm", ".mov"))

        try:
            out_bytes: bytes
            out_name: str
            if is_video:
                v_proc = VideoProcessor()
                out_bytes, out_name = await v_proc.process(data, filename)
            else:
                processor = ImageProcessor()
                out_bytes, out_name = await processor.process(data, filename)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except RuntimeError as exc:
            log.error("media upload: processor runtime error: %s", exc)
            return error_response(503, "SERVICE_UNAVAILABLE", str(exc))

        media_dir = pathlib.Path(config.media_path)
        media_dir.mkdir(parents=True, exist_ok=True)
        dest = media_dir / out_name
        dest.write_bytes(out_bytes)

        url = f"/api/media/{out_name}"
        return web.json_response({"url": url, "filename": out_name}, status=201)

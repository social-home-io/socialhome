"""Video processing module — PyAV-based transcoding (spec §5.2).

Accepts MP4, WebM, and QuickTime video. Transcodes to VP9/Opus WebM
in-process via PyAV (C bindings to libavcodec/libavformat). No system
``ffmpeg`` binary required — PyAV bundles its own libav*.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid

import av
from PIL import Image

from ..domain.media_constraints import (
    THUMBNAIL_PX,
    THUMBNAIL_WEBP_QUALITY,
    VIDEO_ACCEPTED_MIMES,
    VIDEO_AUDIO_BITRATE_KBPS,
    VIDEO_CRF,
    VIDEO_MAX_DIMENSION,
    VIDEO_MAX_DURATION_SECONDS,
    VIDEO_MAX_UPLOAD_BYTES,
)

log = logging.getLogger(__name__)


class VideoProcessor:
    """Transcode uploaded videos to VP9/Opus WebM via PyAV.

    Processing parameters are protocol constants defined in
    :mod:`socialhome.domain.media_constraints`.
    """

    ACCEPTED_MIME_TYPES: frozenset[str] = VIDEO_ACCEPTED_MIMES

    __slots__ = (
        "_max_dimension",
        "_crf",
        "_max_duration",
        "_audio_bitrate",
        "_max_input_bytes",
    )

    def __init__(self) -> None:
        self._max_dimension = VIDEO_MAX_DIMENSION
        self._crf = VIDEO_CRF
        self._max_duration = VIDEO_MAX_DURATION_SECONDS
        self._audio_bitrate = VIDEO_AUDIO_BITRATE_KBPS
        self._max_input_bytes = VIDEO_MAX_UPLOAD_BYTES

    # ── Public API ────────────────────────────────────────────────────────

    async def process(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[bytes, str]:
        """Validate and transcode *data* to a VP9/Opus WebM.

        Parameters
        ----------
        data:
            Raw bytes of the uploaded video file.
        filename:
            Original filename (used for logging only).

        Returns
        -------
        tuple[bytes, str]
            ``(webm_bytes, new_filename)`` where *new_filename* is a
            UUID-based ``.webm`` name.

        Raises
        ------
        ValueError
            If *data* exceeds ``max_input_bytes`` or the file is not a
            valid video.
        """
        if len(data) > self._max_input_bytes:
            raise ValueError(
                f"Video upload exceeds maximum allowed size of "
                f"{self._max_input_bytes} bytes"
            )

        loop = asyncio.get_running_loop()
        webm_bytes = await loop.run_in_executor(
            None,
            self._transcode,
            data,
        )

        new_filename = f"{uuid.uuid4().hex}.webm"
        return webm_bytes, new_filename

    async def generate_thumbnail(self, data: bytes) -> bytes:
        """Extract the first frame of *data* as a WebP image.

        Returns raw bytes of the WebP thumbnail.

        Raises
        ------
        ValueError
            If the video has no decodable frames.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._extract_thumbnail,
            data,
        )

    # ── Internal (run in executor) ────────────────────────────────────────

    def _transcode(self, data: bytes) -> bytes:
        """Synchronous VP9/Opus WebM transcode via PyAV."""
        input_buf = io.BytesIO(data)
        output_buf = io.BytesIO()

        try:
            input_container = av.open(input_buf, format=None)
        except av.error.InvalidDataError as exc:
            raise ValueError(f"Invalid or unsupported video file: {exc}") from exc

        try:
            output_container = av.open(output_buf, mode="w", format="webm")

            # Probe input streams.
            in_video = None
            in_audio = None
            for stream in input_container.streams:
                if stream.type == "video" and in_video is None:
                    in_video = stream
                elif stream.type == "audio" and in_audio is None:
                    in_audio = stream

            if in_video is None:
                raise ValueError("No video stream found in uploaded file")

            # Compute output dimensions: scale longest edge to max_dimension,
            # keep aspect ratio, ensure both dimensions are even.
            src_w = in_video.codec_context.width
            src_h = in_video.codec_context.height
            out_w, out_h = self._scale_dimensions(src_w, src_h)

            # Configure VP9 output stream.
            fps = in_video.average_rate or 30
            out_video = output_container.add_stream("libvpx-vp9", rate=fps)
            out_video.width = out_w
            out_video.height = out_h
            out_video.pix_fmt = "yuv420p"
            # VP9 CRF mode: set bit_rate to 0, use crf option.
            out_video.bit_rate = 0
            out_video.options = {"crf": str(self._crf)}

            # Configure Opus output stream (if input has audio).
            out_audio = None
            if in_audio is not None:
                out_audio = output_container.add_stream("libopus", rate=48000)
                out_audio.bit_rate = self._audio_bitrate * 1000

            # Transcode frame by frame.
            max_pts = None
            if in_video.time_base is not None:
                max_pts = int(self._max_duration / float(in_video.time_base))

            for packet in (
                input_container.demux(in_video, in_audio)
                if in_audio
                else input_container.demux(in_video)
            ):
                if packet.dts is None:
                    continue

                # Duration cap.
                if (
                    max_pts is not None
                    and packet.stream.type == "video"
                    and packet.pts is not None
                    and packet.pts > max_pts
                ):
                    break

                for frame in packet.decode():
                    if frame is None:
                        continue

                    if packet.stream.type == "video":
                        # Rescale if needed.
                        if frame.width != out_w or frame.height != out_h:
                            frame = frame.reformat(
                                width=out_w,
                                height=out_h,
                                format="yuv420p",
                            )
                        for out_packet in out_video.encode(frame):
                            output_container.mux(out_packet)

                    elif packet.stream.type == "audio" and out_audio is not None:
                        # Resample audio to 48kHz for Opus.
                        frame.pts = None  # let encoder assign PTS
                        for out_packet in out_audio.encode(frame):
                            output_container.mux(out_packet)

            # Flush encoders.
            for out_packet in out_video.encode():
                output_container.mux(out_packet)
            if out_audio is not None:
                for out_packet in out_audio.encode():
                    output_container.mux(out_packet)

        finally:
            output_container.close()
            input_container.close()

        return output_buf.getvalue()

    def _extract_thumbnail(self, data: bytes) -> bytes:
        """Synchronous first-frame extraction → WebP bytes."""
        input_buf = io.BytesIO(data)

        try:
            container = av.open(input_buf, format=None)
        except av.error.InvalidDataError as exc:
            raise ValueError(f"Invalid video for thumbnail: {exc}") from exc

        try:
            video_stream = next(
                (s for s in container.streams if s.type == "video"),
                None,
            )
            if video_stream is None:
                raise ValueError("No video stream found for thumbnail")

            for frame in container.decode(video_stream):
                pil_image = frame.to_image().convert("RGB")
                pil_image.thumbnail(
                    (THUMBNAIL_PX, THUMBNAIL_PX),
                    Image.Resampling.LANCZOS,
                )
                buf = io.BytesIO()
                pil_image.save(buf, "WEBP", quality=THUMBNAIL_WEBP_QUALITY)
                return buf.getvalue()
        finally:
            container.close()

        raise ValueError("Video has no decodable frames")

    # ── Helpers ───────────────────────────────────────────────────────────

    def _scale_dimensions(self, src_w: int, src_h: int) -> tuple[int, int]:
        """Compute output (w, h) keeping aspect ratio, longest side ≤ max.

        Both dimensions are made even (required by VP9).
        """
        max_dim = self._max_dimension
        if src_w >= src_h:
            if src_w <= max_dim:
                out_w, out_h = src_w, src_h
            else:
                out_w = max_dim
                out_h = int(src_h * max_dim / src_w)
        else:
            if src_h <= max_dim:
                out_w, out_h = src_w, src_h
            else:
                out_h = max_dim
                out_w = int(src_w * max_dim / src_h)

        # Ensure even dimensions.
        out_w = out_w if out_w % 2 == 0 else out_w - 1
        out_h = out_h if out_h % 2 == 0 else out_h - 1
        return max(out_w, 2), max(out_h, 2)

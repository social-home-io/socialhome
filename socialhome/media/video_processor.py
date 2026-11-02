"""Video processing module — PyAV-based transcoding (spec §5.2).

Accepts MP4, WebM, and QuickTime video. Transcodes to VP9/Opus WebM
in-process via PyAV (C bindings to libavcodec/libavformat). No system
``ffmpeg`` binary required — PyAV bundles its own libav*.

Every stored video is the same shape whatever it came from: VP9/Opus
WebM, longest edge ``VIDEO_MAX_DIMENSION``, the picture turned the way
the phone recorded it (a portrait clip's rotation is applied — WebM
carries none). Only the *length* kept differs by use: the protocol's
``VIDEO_MAX_DURATION_SECONDS`` for the feed, spaces and DMs, the
household gallery's own limit (checked at upload) for its videos — the
caller passes it per job (:meth:`VideoProcessor.transcode_file`).

Large sources are read from and written to disk
(:meth:`~VideoProcessor.transcode_file`, :meth:`~VideoProcessor.probe_file`,
:meth:`~VideoProcessor.thumbnail_file`) so a 2 GB phone video is never
held in memory; the bytes API (:meth:`~VideoProcessor.process`) remains
for small in-memory clips. All decoding / encoding runs in a worker
thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import IO

import av
import av.filter
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
from ..domain.media_transcode import VideoProbe

log = logging.getLogger(__name__)

#: libvpx VP9 speed preset (``cpu-used``, 0 slowest … 8 fastest) for the
#: ``good`` deadline. How hard the encoder searches, not what it stores:
#: the format, size and quality target (``VIDEO_CRF``) are unchanged.
#: libvpx's default (1) took 3.5 s of CPU per second of 1080p video on a
#: desktop core; 4 takes ~2.2 s for a file of about the same size — the
#: difference between a 5-minute video being ready in ~35 min or ~55 min
#: on a Raspberry Pi 4 (measurements in the PR introducing this).
VP9_CPU_USED: int = 4

#: Encoder threads: row-based multithreading over all but one core (at
#: most 4 — libvpx gains little past that at 1080p), so a Raspberry Pi
#: running Home Assistant keeps one core for everything else.
VP9_MAX_THREADS: int = 4

#: A container date before this is a camera with no clock set (MP4's
#: epoch is 1904-01-01, many cameras write 1970-01-01) — not a day.
_EARLIEST_TAKEN_ON = date(1971, 1, 1)
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
#: QuickTime's local-time recording date (iPhone); preferred over the
#: UTC ``creation_time`` every MP4 / MOV carries, which can be the next
#: or previous day for an evening clip.
_LOCAL_DATE_KEY = "com.apple.quicktime.creationdate"
_UTC_DATE_KEY = "creation_time"

#: ``on_progress`` is called at most once per this many seconds of video.
_PROGRESS_STEP_S: float = 1.0

ProgressCallback = Callable[[float], None]


def encoder_threads() -> int:
    """Threads for the VP9 encoder on this machine (see ``VP9_MAX_THREADS``)."""
    return max(1, min(VP9_MAX_THREADS, (os.cpu_count() or 2) - 1))


class VideoProcessor:
    """Transcode uploaded videos to VP9/Opus WebM via PyAV.

    Processing parameters are protocol constants defined in
    :mod:`socialhome.domain.media_constraints`.
    """

    ACCEPTED_MIME_TYPES: frozenset[str] = VIDEO_ACCEPTED_MIMES

    __slots__ = (
        "_audio_bitrate",
        "_crf",
        "_max_dimension",
        "_max_duration",
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
        """Validate and transcode in-memory *data* to a VP9/Opus WebM.

        Keeps at most the protocol's ``VIDEO_MAX_DURATION_SECONDS``.

        Returns ``(webm_bytes, new_filename)`` where *new_filename* is a
        UUID-based ``.webm`` name. Raises :class:`ValueError` if *data*
        exceeds ``max_input_bytes`` or is not a valid video.
        """
        if len(data) > self._max_input_bytes:
            raise ValueError(
                f"Video upload exceeds maximum allowed size of "
                f"{self._max_input_bytes} bytes"
            )
        webm_bytes = await asyncio.to_thread(self._transcode_bytes, data)
        return webm_bytes, f"{uuid.uuid4().hex}.webm"

    async def probe_file(self, path: str | os.PathLike[str]) -> VideoProbe:
        """Read a video file's length, stored size and recording day.

        Decodes the first frame, so a file that only *looks* like a video
        is refused here (:class:`ValueError`), before it is queued.
        """
        return await asyncio.to_thread(self._probe, os.fspath(path))

    async def transcode_file(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        max_duration_s: float,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Transcode the video at *src* into a VP9/Opus WebM at *dst*.

        Streams file to file — neither side is held in memory. Keeps the
        first *max_duration_s* seconds (the caller's limit for this use;
        a household gallery video was checked against it at upload, so
        nothing is cut). *on_progress* is called from the worker thread
        with the seconds of video done so far — keep it cheap.
        """
        await asyncio.to_thread(
            self._transcode,
            os.fspath(src),
            os.fspath(dst),
            float(max_duration_s),
            on_progress,
        )

    async def generate_thumbnail(self, data: bytes) -> bytes:
        """The first frame of in-memory *data* as a WebP poster."""
        return await asyncio.to_thread(self._extract_thumbnail, io.BytesIO(data))

    async def thumbnail_file(self, path: str | os.PathLike[str]) -> bytes:
        """The first frame of the video at *path* as a WebP poster."""
        return await asyncio.to_thread(self._extract_thumbnail, os.fspath(path))

    # ── Internal (run in a worker thread) ─────────────────────────────────

    def _transcode_bytes(self, data: bytes) -> bytes:
        output_buf = io.BytesIO()
        self._transcode(io.BytesIO(data), output_buf, float(self._max_duration), None)
        return output_buf.getvalue()

    def _probe(self, src: str) -> VideoProbe:
        container = _open_input(src)
        try:
            in_video, first = _first_video_frame(container)
            rotation = _normalise_rotation(getattr(first, "rotation", 0))
            out_w, out_h = self._scale_dimensions(*_display_size(first, rotation))
            return VideoProbe(
                duration_s=_duration_seconds(container, in_video),
                width=out_w,
                height=out_h,
                rotation=rotation,
                taken_on=_taken_on(container, in_video),
            )
        finally:
            container.close()

    def _transcode(
        self,
        src: str | IO[bytes],
        dst: str | IO[bytes],
        max_duration_s: float,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Synchronous VP9/Opus WebM transcode via PyAV."""
        # A first look at the opening frame: the rotation the phone
        # recorded with and the picture's size, so the output streams are
        # set up before the first packet (audio included) is read.
        container = _open_input(src)
        try:
            _, first = _first_video_frame(container)
            rotation = _normalise_rotation(getattr(first, "rotation", 0))
            out_w, out_h = self._scale_dimensions(*_display_size(first, rotation))
        finally:
            container.close()
        if not isinstance(src, str):
            src.seek(0)

        input_container = _open_input(src)
        output_container = None
        try:
            output_container = av.open(dst, mode="w", format="webm")

            # Probe input streams.
            in_video = None
            in_audio = None
            for stream in input_container.streams:
                if stream.type == "video" and in_video is None:
                    in_video = stream
                elif stream.type == "audio" and in_audio is None:
                    in_audio = stream
            if in_video is None:  # pragma: no cover — the first look checked
                raise ValueError("No video stream found in uploaded file")
            # Frame-threaded decoding: a 4K HEVC phone clip decodes on all
            # cores instead of one.
            in_video.thread_type = "AUTO"

            out_video = self._add_video_stream(output_container, in_video, out_w, out_h)
            rotate = _rotation_graph(in_video, rotation)

            # Configure Opus output stream (if input has audio).
            out_audio = None
            if in_audio is not None:
                out_audio = output_container.add_stream("libopus", rate=48000)
                out_audio.bit_rate = self._audio_bitrate * 1000

            # Transcode frame by frame. The length cap is checked on
            # *decoded* frames (display order), so frames a threaded
            # decoder still holds are never lost before the cap, and none
            # past it is kept.
            start = 0.0
            if in_video.start_time is not None and in_video.time_base is not None:
                start = float(in_video.start_time * in_video.time_base)
            reported = 0.0
            past_cap = False

            for packet in (
                input_container.demux(in_video, in_audio)
                if in_audio
                else input_container.demux(in_video)
            ):
                # The empty packet at the end of each stream flushes its
                # decoder — the frames it still holds (B-frame reordering,
                # frame threading) are the video's last ones, so it is
                # decoded, not skipped.
                if packet.dts is None and packet.size:
                    continue

                for frame in packet.decode():
                    if frame is None:
                        continue
                    at = (frame.time - start) if frame.time is not None else None
                    if at is not None and at > max_duration_s:
                        if packet.stream.type == "video":
                            past_cap = True
                            break
                        continue  # audio past the cap

                    if packet.stream.type == "video":
                        if rotate is not None:
                            rotate.push(frame)
                            frame = rotate.pull()
                        # Rescale if needed.
                        if frame.width != out_w or frame.height != out_h:
                            frame = frame.reformat(
                                width=out_w,
                                height=out_h,
                                format="yuv420p",
                            )
                        for out_packet in out_video.encode(frame):
                            output_container.mux(out_packet)
                        if (
                            on_progress is not None
                            and at is not None
                            and at - reported >= _PROGRESS_STEP_S
                        ):
                            reported = at
                            on_progress(at)

                    elif packet.stream.type == "audio" and out_audio is not None:
                        # Resample audio to 48kHz for Opus.
                        frame.pts = None  # let encoder assign PTS
                        for out_packet in out_audio.encode(frame):
                            output_container.mux(out_packet)
                if past_cap:
                    break

            # Flush encoders.
            for out_packet in out_video.encode():
                output_container.mux(out_packet)
            if out_audio is not None:
                for out_packet in out_audio.encode():
                    output_container.mux(out_packet)
        finally:
            if output_container is not None:
                output_container.close()
            input_container.close()

    def _add_video_stream(self, output_container, in_video, out_w: int, out_h: int):
        fps = in_video.average_rate or 30
        out_video = output_container.add_stream("libvpx-vp9", rate=fps)
        out_video.width = out_w
        out_video.height = out_h
        out_video.pix_fmt = "yuv420p"
        # VP9 CRF mode: set bit_rate to 0, use crf option.
        out_video.bit_rate = 0
        threads = encoder_threads()
        out_video.codec_context.thread_count = threads
        out_video.options = {
            "crf": str(self._crf),
            "deadline": "good",
            "cpu-used": str(VP9_CPU_USED),
            "row-mt": "1",
            # 2^n tile columns let row-mt use the threads (a 1080p frame
            # fits 4; libvpx lowers it for smaller frames itself).
            "tile-columns": "2" if threads > 1 else "0",
        }
        return out_video

    def _extract_thumbnail(self, src: str | IO[bytes]) -> bytes:
        """Synchronous first-frame extraction → WebP bytes."""
        try:
            container = av.open(src, format=None)
        except av.error.FFmpegError as exc:
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
                rotation = _normalise_rotation(getattr(frame, "rotation", 0))
                if rotation:
                    # PIL turns counter-clockwise, like the display matrix.
                    pil_image = pil_image.rotate(rotation, expand=True)
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


# ── Module helpers (worker thread) ────────────────────────────────────────


def _open_input(src: str | IO[bytes]):
    try:
        return av.open(src, format=None)
    except av.error.FFmpegError as exc:
        raise ValueError(f"Invalid or unsupported video file: {exc}") from exc


def _first_video_frame(container):
    """``(video_stream, first_frame)`` — or :class:`ValueError` when the
    file has no video stream or not one frame decodes."""
    in_video = next((s for s in container.streams if s.type == "video"), None)
    if in_video is None:
        raise ValueError("No video stream found in uploaded file")
    try:
        first = next(iter(container.decode(in_video)), None)
    except av.error.FFmpegError as exc:
        raise ValueError(f"Invalid or unsupported video file: {exc}") from exc
    if first is None:
        raise ValueError("Video has no decodable frames")
    return in_video, first


def _display_size(frame, rotation: int) -> tuple[int, int]:
    """The picture's size as shown (a quarter turn swaps the sides)."""
    if rotation in (90, 270):
        return frame.height, frame.width
    return frame.width, frame.height


def _normalise_rotation(value: float | None) -> int:
    """A display rotation as 0 / 90 / 180 / 270 (counter-clockwise)."""
    try:
        quarter = round(float(value or 0) / 90.0)
    except TypeError, ValueError:
        return 0
    return (quarter % 4) * 90


def _rotation_graph(in_video, rotation: int):
    """A filter graph that turns frames upright, or ``None`` for 0°.

    The rotation is counter-clockwise (PyAV's ``VideoFrame.rotation``,
    from the display matrix): 90° is ``transpose=cclock``, 270° is
    ``transpose=clock``, 180° both flips.
    """
    if rotation == 0:
        return None
    graph = av.filter.Graph()
    src = graph.add_buffer(template=in_video)
    if rotation == 90:
        chain = [graph.add("transpose", "cclock")]
    elif rotation == 270:
        chain = [graph.add("transpose", "clock")]
    else:
        chain = [graph.add("hflip"), graph.add("vflip")]
    sink = graph.add("buffersink")
    node = src
    for f in chain:
        node.link_to(f)
        node = f
    node.link_to(sink)
    graph.configure()
    return graph


def _duration_seconds(container, stream) -> float | None:
    """The file's length in seconds, or ``None`` when it doesn't say."""
    if container.duration is not None and container.duration > 0:
        return container.duration / av.time_base
    if stream.duration is not None and stream.time_base is not None:
        secs = float(stream.duration * stream.time_base)
        return secs if secs > 0 else None
    return None


def _taken_on(container, stream) -> str | None:
    """The recording day the container carries, as ``YYYY-MM-DD``.

    QuickTime's local ``creationdate`` first (an iPhone's own day), else
    the UTC ``creation_time`` of the container or the video stream. Only
    the date is read — a file's location tags are never looked at.
    """
    tags = [container.metadata or {}, stream.metadata or {}]
    for key in (_LOCAL_DATE_KEY, _UTC_DATE_KEY):
        for meta in tags:
            day = _parse_day(meta.get(key))
            if day is not None:
                return day
    return None


def _parse_day(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _DAY_RE.match(raw.strip())
    if m is None:
        return None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    # A clock that was never set, or one in the future: not a real day.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    if day < _EARLIEST_TAKEN_ON or day > tomorrow:
        return None
    return day.isoformat()

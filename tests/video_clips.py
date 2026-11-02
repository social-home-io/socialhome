"""Tiny real video files for tests (PyAV, in memory).

A clip of ``seconds`` at 1 frame per second and a few pixels decodes,
probes and transcodes in well under a second, yet carries a real length
— so a 5-minute boundary can be tested without minutes of video.
"""

from __future__ import annotations

import io
import math
from array import array

import av
from PIL import Image


def make_clip(
    seconds: int,
    *,
    size: tuple[int, int] = (32, 16),
    fps: int = 1,
    rotation: int = 0,
    metadata: dict[str, str] | None = None,
    audio: bool = False,
    split_colour: bool = False,
) -> bytes:
    """An H.264 MP4 of ``seconds`` (``seconds * fps`` frames).

    ``rotation`` sets the display matrix (counter-clockwise degrees, the
    way a phone marks a portrait video). ``split_colour`` paints the left
    half red and the right half blue, so a test can tell which way the
    picture was turned. ``metadata`` lands on the container (e.g.
    ``creation_time``).
    """
    w, h = size
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp4")
    try:
        for key, value in (metadata or {}).items():
            out.metadata[key] = value
        video = out.add_stream("libx264", rate=fps)
        video.width, video.height, video.pix_fmt = w, h, "yuv420p"
        video.options = {"preset": "ultrafast"}
        if rotation:
            video.set_display_rotation(rotation)
        sound = out.add_stream("aac", rate=48000) if audio else None
        for i in range(seconds * fps):
            img = Image.new(
                "RGB", (w, h), (0, 0, 255) if split_colour else (i % 255, 90, 160)
            )
            if split_colour:
                img.paste((255, 0, 0), (0, 0, w // 2, h))
            frame = av.VideoFrame.from_image(img)
            frame.pts = i
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode():
            out.mux(packet)
        if sound is not None:
            samples, t0 = 1024, 0
            while t0 < seconds * 48000:
                tone = array(
                    "f",
                    (
                        0.2 * math.sin(2 * math.pi * 440 * (k + t0) / 48000)
                        for k in range(samples)
                    ),
                )
                af = av.AudioFrame(format="flt", layout="mono", samples=samples)
                af.planes[0].update(tone.tobytes())
                af.sample_rate = 48000
                af.pts = t0
                for packet in sound.encode(af):
                    out.mux(packet)
                t0 += samples
            for packet in sound.encode():
                out.mux(packet)
    finally:
        out.close()
    return buf.getvalue()

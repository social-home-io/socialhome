"""Protocol-level media constraints (§5.2).

These values are **not** operator-configurable — they are part of the Social
Home wire protocol.  Every instance in the federation must agree on the same
limits so that media exchanged between instances is always accepted.

Local upload processing (image_processor, video_processor) uses these
constants to produce conformant output.  Inbound federation validation
(media_validator) uses them to reject non-conforming payloads.
"""

from __future__ import annotations


# ─── Image constraints ──────────────────────────────────────────────────────

#: Longest-side cap for uploaded photos / post images. 2560 px is a
#: 1:1 fit for the native short edge of an iPad Pro 12.9 (2732×2048)
#: and a 14"/16" retina MacBook (1964/2234 short edge), so a photo
#: viewed full-bleed on those screens is no longer software-stretched.
#: The previous 2048 px cap pre-dated retina laptops as a baseline.
IMAGE_MAX_DIMENSION: int = 2560
#: WebP quality factor. 82 is the threshold where the mild Q78
#: artifacts (skin-tone blockiness, gradient banding on skies) clean
#: up visibly on typical household photos. ~+18% file size over 78
#: per image. Thumbnails stay at Q75 — at 512 px the perceived
#: difference is dominated by display scaling, so the savings are
#: free.
IMAGE_WEBP_QUALITY: int = 82
IMAGE_MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024  # 20 MiB
IMAGE_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
    }
)
IMAGE_OUTPUT_MIME: str = "image/webp"
IMAGE_WEBP_MAGIC: bytes = b"RIFF"
IMAGE_WEBP_MAGIC_8: bytes = b"WEBP"

# ─── Video constraints ──────────────────────────────────────────────────────

#: Longest-side cap. 1920 px is true 1080p — matches what every flagship
#: phone captures by default and the playback target (laptop / TV) every
#: viewer is likely on. The previous 1280 px (720p) read as low quality
#: on anything bigger than a phone screen.
VIDEO_MAX_DIMENSION: int = 1920
#: x264 CRF — log-scale quality knob (-6 ≈ 2× file size). 25 sits in
#: the "visually transparent for normal household content" band; the
#: previous 28 left visible smearing on motion and banding on
#: gradients. Cost: ~1.4× per-clip file size at the same resolution,
#: ~2.8× combined with the 720p → 1080p bump.
VIDEO_CRF: int = 25
VIDEO_MAX_DURATION_SECONDS: int = 60
VIDEO_AUDIO_BITRATE_KBPS: int = 96
VIDEO_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # 200 MiB
VIDEO_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)
VIDEO_OUTPUT_MIME: str = "video/webm"
VIDEO_WEBM_MAGIC: bytes = b"\x1a\x45\xdf\xa3"

# ─── Shared ─────────────────────────────────────────────────────────────────

#: Grid-thumbnail longest side. 512 px keeps tablet- and desktop-grid
#: thumbs sharp at 3× retina (~170 dp wide). The previous 400 px was a
#: phone-grid target and read soft on bigger screens.
THUMBNAIL_PX: int = 512
THUMBNAIL_WEBP_QUALITY: int = 75
CAPTION_MAX: int = 300

# ─── Profile / cover uploads ───────────────────────────────────────────────
#: Avatars and space cover images both ride through ImageProcessor, which
#: transcodes to WebP and caps the longest side — so the raw upload cap is
#: about accommodating high-resolution phone photos (HEIC/JPEG) rather than
#: about the on-disk size. One shared constant keeps the three upload sites
#: (profile picture, per-space member picture, space cover) in lockstep.
PROFILE_PICTURE_MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MiB

# ─── Generic file uploads (DM attachments etc.) ───────────────────────────
#: Catch-all cap for ``type='file'`` DM attachments and any future
#: passthrough uploads (PDFs, docs, archives). Smaller than the
#: video cap because files don't transcode to a smaller on-disk
#: form: 25 MiB covers most invoices, scans, and short audio clips
#: without inviting people to ship a whole movie through the DM
#: bar. The image / video paths keep their own (larger) caps.
FILE_MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MiB
#: Disallowed extensions for ``type='file'`` uploads. Anything
#: that's *executable on default OS handlers* gets rejected — the
#: bar there is "what would a click on the download link in the SPA
#: do?". The list is deliberately narrow: a malicious sender can
#: always wrap a payload inside a ``.zip``; this just prevents the
#: most obvious foot-guns where the SPA's ``download`` attribute
#: would auto-prompt to run something. Aligned with the GitHub /
#: Slack / Discord "denied attachment" sets.
FILE_DENIED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".msi",
        ".scr",
        ".vbs",
        ".vbe",
        ".jse",
        ".wsf",
        ".wsh",
        ".lnk",
        ".app",
        ".dmg",
        ".sh",
        ".bash",
        ".zsh",
        ".jar",
        ".dll",
        ".so",
    },
)

# ─── Audio (voice notes) ──────────────────────────────────────────────────
#: Voice notes use the Opus codec — free, royalty-free, speech-tuned. The
#: container is whichever Opus-bearing format the sender's browser
#: produces: Firefox emits ``audio/ogg``, Chrome / Edge emit
#: ``audio/webm`` (their MediaRecorder doesn't support the OGG
#: container). Both are accepted; only the codec inside has to be
#: Opus. The receiver-side decode pipeline is container-agnostic — PyAV
#: probes via the leading bytes either way.
AUDIO_OUTPUT_MIME: str = "audio/ogg"
AUDIO_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {"audio/ogg", "audio/webm", "audio/mp4"},
)
AUDIO_OGG_MAGIC: bytes = b"OggS"
#: WebM uses the same EBML magic as video/webm (``\x1a\x45\xdf\xa3``).
#: The processor probes all three magics — the codec sniff is the
#: real gate.
AUDIO_WEBM_MAGIC: bytes = b"\x1a\x45\xdf\xa3"
#: MP4 / M4A is what Safari's MediaRecorder produces (AAC inside an
#: ISO base-media container). The signature lives at byte 4 — ``ftyp``
#: marks the ftyp box that every well-formed MP4 starts with.
AUDIO_MP4_FTYP_OFFSET: int = 4
AUDIO_MP4_FTYP_MAGIC: bytes = b"ftyp"
#: Codecs we accept inside a voice-note container. Opus is the
#: preferred codec (Firefox + Chromium MediaRecorder both emit it);
#: AAC is the Safari path. Both decode losslessly to PCM via PyAV
#: before STT and play back natively in every modern browser.
AUDIO_ACCEPTED_CODECS: frozenset[str] = frozenset({"opus", "aac"})
#: Five minutes — matches WhatsApp's voice-note ceiling. At 24 kbps Opus
#: that is ~ 0.9 MiB so the message fits comfortably inside the 25 MiB
#: ``FILE_MAX_UPLOAD_BYTES`` cap with margin for the OGG container
#: overhead and any local transcoding losses.
AUDIO_MAX_DURATION_SECONDS: int = 300
#: Same 25 MiB ceiling as files. A 5-minute Opus@24 kbps blob is ~1 MiB,
#: so this is generous — it leaves room for higher-bitrate clients that
#: choose to ship 48 kbps (still well under cap).
AUDIO_MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MiB
#: Target encoder bitrate for voice notes. 24 kbps Opus is speech-clean
#: on every device tested; doubling to 48 kbps is audibly only on music
#: source material, which is not what a voice note ships.
AUDIO_BITRATE_KBPS: int = 24

#: Space cover image resized to this longest side; larger than the 256-px
#: profile-picture cap so a hero banner has real estate.
SPACE_COVER_MAX_DIMENSION: int = 1200

#: Space icon (avatar) resized to this longest side — small, like a
#: profile picture, since it renders as a circular chip.
SPACE_ICON_MAX_DIMENSION: int = 256

#: Largest cover / icon (raw WebP bytes) a household embeds in a GFS space
#: publish, and the largest the GFS stores. The publish ships both images as
#: base64 ``data:`` URIs so the GFS's public page renders them on its own
#: origin; a dimension-capped WebP (1200 px cover at the thumbnail quality) is
#: typically a few hundred KiB, so this only bites a pathological image (a
#: noisy RGBA cover can reach ~2.3 MB). An image over it is left OUT of the
#: listing (the space still publishes, just without that art) rather than
#: sinking the whole publish. Shared by both ends so the household never
#: sends what the GFS would refuse to keep.
SPACE_IMAGE_EMBED_MAX_BYTES: int = 1536 * 1024

#: ``data:`` URI prefix the publish embeds images under.
SPACE_IMAGE_DATA_URI_PREFIX: str = "data:image/webp;base64,"

#: Longest embedded-image ``data:`` URI: the prefix plus the base64 (4/3,
#: padded) of :data:`SPACE_IMAGE_EMBED_MAX_BYTES`.
SPACE_IMAGE_DATA_URI_MAX_CHARS: int = len(SPACE_IMAGE_DATA_URI_PREFIX) + 4 * -(
    -SPACE_IMAGE_EMBED_MAX_BYTES // 3
)

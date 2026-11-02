"""Gallery downloads — human file names and a streamed ZIP (§23.119).

Downloads hand out the stored files exactly as kept: WebP photos and WebM
videos at the household's defined size (owner decision 2026-11-02 — no
originals, no JPEG re-encode). Two pieces live here:

* :func:`download_names` — ``"Summer 2026 – 2026-07-12 – 3.webp"``: the
  album, the day the photo was taken (else the day it was added) and, when
  that album and day hold more than one picked photo, a running number.
* :func:`stream_zip` — a ZIP of stored (uncompressed) entries written
  straight to the response: WebP and WebM are compressed already, so
  deflating them again only burns a Pi's CPU. Each entry's CRC-32 is
  computed off the event loop first (``asyncio.to_thread``), then the
  file is streamed with ``aiofiles`` in chunks, so memory stays at one
  chunk whatever the selection. The archive's exact length is known up
  front (:func:`zip_size`), so the browser shows real progress.

Bounded by the caller: :data:`~socialhome.domain.gallery.DOWNLOAD_MAX_BYTES`
keeps every offset inside the classic 32-bit ZIP fields (no ZIP64), and
the item caps keep the entry count far below 65 535.
"""

from __future__ import annotations

import asyncio
import re
import struct
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import aiofiles

from ..domain.gallery import GalleryDownloadFile, GalleryItem

#: Read / write granularity while streaming one stored file.
CHUNK_BYTES: int = 256 * 1024

#: Longest album part of a file name (an album name is ≤ 80 anyway).
_LABEL_MAX = 80
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_SPACES = re.compile(r"\s+")
_EXT = re.compile(r"^[a-z0-9]{1,5}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: General-purpose flag bit 11 — the entry name is UTF-8 ("Überraschung").
_FLAG_UTF8 = 0x0800
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_CENTRAL_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
_END_RECORD = struct.Struct("<IHHHHIIH")
#: Classic ZIP fields are 32 bits; the caller's byte cap stays well below.
_ZIP32_MAX = 0xFFFFFFFF


def safe_label(label: str | None, fallback: str = "Photo") -> str:
    """An album name made safe as part of a file name on every system:
    no path separators, no reserved characters, no leading / trailing dots
    or spaces, at most 80 characters."""
    text = _SPACES.sub(" ", _UNSAFE.sub(" ", label or "")).strip(" .")
    return text[:_LABEL_MAX].strip(" .") or fallback


def _day_of(item: GalleryItem) -> str:
    """The day taken, else the day it was added (``YYYY-MM-DD``), else ``''``."""
    if item.taken_at and _DAY.match(item.taken_at):
        return item.taken_at
    added = (item.created_at or "")[:10]
    return added if _DAY.match(added) else ""


def _ext_of(item: GalleryItem) -> str:
    name = (item.url or "").split("?", 1)[0].rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext if _EXT.match(ext) else "bin"


def download_names(picked: Sequence[tuple[GalleryItem, str]]) -> list[str]:
    """One file name per ``(item, album name)``, in order:
    ``"<album> – <day>.<ext>"``, numbered ``"<album> – <day> – <n>.<ext>"``
    when more than one picked photo shares that album and day. Names are
    unique within the list (a ZIP may not hold two entries alike)."""
    stems: list[str] = []
    for item, label in picked:
        day = _day_of(item)
        stems.append(f"{safe_label(label)} – {day}" if day else safe_label(label))
    totals = Counter(s.casefold() for s in stems)
    running: Counter[str] = Counter()
    taken: set[str] = set()
    out: list[str] = []
    for (item, _label), stem in zip(picked, stems, strict=True):
        key = stem.casefold()
        ext = _ext_of(item)
        if totals[key] > 1:
            running[key] += 1
            name = f"{stem} – {running[key]}.{ext}"
        else:
            name = f"{stem}.{ext}"
        # A clash left over (an album named like "Summer – 2026-07-12 – 2")
        # gets a further number.
        base, extra = name, 1
        while base.casefold() in taken:
            extra += 1
            base = f"{stem} ({extra}).{ext}"
        taken.add(base.casefold())
        out.append(base)
    return out


def content_disposition(name: str) -> str:
    """``attachment`` with the name for every browser: an ASCII fallback
    plus the UTF-8 ``filename*`` (RFC 6266 / 5987)."""
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
    quoted = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "-._~") else _pct(ch)
        for ch in name
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _pct(ch: str) -> str:
    return "".join(f"%{b:02X}" for b in ch.encode("utf-8"))


def _dos_stamp(now: datetime) -> tuple[int, int]:
    """``(time, date)`` in MS-DOS format, the only stamp classic ZIP has."""
    year = max(1980, min(now.year, 2107))
    dos_time = (now.hour << 11) | (now.minute << 5) | (now.second // 2)
    dos_date = ((year - 1980) << 9) | (now.month << 5) | now.day
    return dos_time, dos_date


def zip_size(files: Sequence[GalleryDownloadFile]) -> int:
    """The exact length of :func:`stream_zip`'s archive for *files*."""
    total = _END_RECORD.size
    for f in files:
        n = len(f.name.encode("utf-8"))
        total += _LOCAL_HEADER.size + n + f.size + _CENTRAL_HEADER.size + n
    return total


def _crc32_sync(path: str) -> int:
    """CRC-32 of a stored file — CPU over the whole file, so it runs in a
    worker thread (``asyncio.to_thread``), never on the event loop."""
    crc = 0
    with open(path, "rb") as fh:  # sync on purpose: called via to_thread
        while chunk := fh.read(CHUNK_BYTES):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


async def stream_zip(
    files: Sequence[GalleryDownloadFile],
    write: Callable[[bytes], Awaitable[None]],
    *,
    now: datetime | None = None,
) -> int:
    """Write a ZIP of *files* (stored entries) through *write*, file by file.
    Returns the bytes written — always :func:`zip_size` of *files*.

    A file must not change between the size check and here; one that went
    missing meanwhile (purged) raises, which ends the response early — the
    browser then reports a failed download rather than a corrupt archive."""
    if zip_size(files) > _ZIP32_MAX:
        raise ValueError("archive too large for a classic ZIP")
    dos_time, dos_date = _dos_stamp(now or datetime.now(UTC))
    central: list[bytes] = []
    offset = 0
    for f in files:
        name = f.name.encode("utf-8")
        crc = await asyncio.to_thread(_crc32_sync, f.path)
        header = _LOCAL_HEADER.pack(
            0x04034B50, 20, _FLAG_UTF8, 0, dos_time, dos_date,
            crc, f.size, f.size, len(name), 0,
        )  # fmt: skip
        await write(header + name)
        written = 0
        async with aiofiles.open(f.path, "rb") as fh:
            while chunk := await fh.read(CHUNK_BYTES):
                written += len(chunk)
                await write(chunk)
        if written != f.size:
            raise OSError(f"{f.name}: size changed while downloading")
        entry = _CENTRAL_HEADER.pack(
            0x02014B50, (3 << 8) | 20, 20, _FLAG_UTF8, 0, dos_time, dos_date,
            crc, f.size, f.size, len(name), 0, 0, 0, 0,
            0o100644 << 16, offset,
        )  # fmt: skip
        central.append(entry + name)
        offset += _LOCAL_HEADER.size + len(name) + f.size
    directory = b"".join(central)
    await write(
        directory
        + _END_RECORD.pack(
            0x06054B50, 0, 0, len(files), len(files), len(directory), offset, 0
        )
    )
    return offset + len(directory) + _END_RECORD.size

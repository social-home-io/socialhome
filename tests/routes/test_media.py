"""Tests for socialhome.routes.media — including signed-URL auth."""

import pathlib

import pytest

from socialhome.app_keys import (
    config_key,
    media_signer_key,
    profile_picture_repo_key,
)
from socialhome.csp import MEDIA_CSP, PLAYABLE_MEDIA_CSP

from .conftest import _auth


@pytest.fixture
async def media_file(client):
    """Drop a tiny WebP-shaped blob into the media dir under a known
    filename so ``GET /api/media/<name>`` can stream it back.

    Returns the canonical (unsigned) URL.
    """
    cfg = client.app[config_key]
    media_dir = pathlib.Path(cfg.media_path)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "abc.webp").write_bytes(b"\x52\x49\x46\x46\x00\x00\x00\x00WEBPVP8 ")
    return "/api/media/abc.webp"


async def test_get_nonexistent_media_404(client):
    """GET /api/media/nonexistent returns 404 when authed."""
    r = await client.get("/api/media/nonexistent.webp", headers=_auth(client._tok))
    assert r.status == 404


async def test_get_with_bearer_token_succeeds(client, media_file):
    """Bearer auth still works for media — fetch() callers rely on it."""
    r = await client.get(media_file, headers=_auth(client._tok))
    assert r.status == 200
    body = await r.read()
    assert body.startswith(b"\x52\x49\x46\x46")  # "RIFF"


async def test_get_with_valid_signature_succeeds(client, media_file):
    """Signed URL authenticates without any Authorization header — this
    is the path browsers use for ``<img src>`` etc."""
    signer = client.app[media_signer_key]
    signed = signer.sign(media_file)
    r = await client.get(signed)  # no Authorization header
    assert r.status == 200


async def test_get_with_tampered_signature_401(client, media_file):
    """Flipping a single character of the sig causes auth to fail."""
    signer = client.app[media_signer_key]
    signed = signer.sign(media_file)
    # Mutate the last char of the sig so HMAC compare fails.
    if signed.endswith("A"):
        tampered = signed[:-1] + "B"
    else:
        tampered = signed[:-1] + "A"
    r = await client.get(tampered)
    assert r.status == 401


async def test_get_with_expired_signature_401(client, media_file):
    """A URL whose ``exp`` is in the past returns 401."""
    signer = client.app[media_signer_key]
    # Sign with a 1-second TTL using ``now`` far in the past.
    signed = signer.sign(media_file, ttl=1, now=1)
    r = await client.get(signed)
    assert r.status == 401


async def test_get_without_any_auth_401(client, media_file):
    """Plain canonical URL with no auth headers and no sig → 401."""
    r = await client.get(media_file)
    assert r.status == 401


async def _upload_file(
    client,
    *,
    filename: str,
    body: bytes,
    content_type: str = "application/octet-stream",
):
    """Issue a multipart POST to ``/api/media/upload``.

    Tiny stdlib-shaped multipart so the upload tests don't pull in
    extra deps. Returns the raw aiohttp response.
    """
    boundary = "----test-boundary"
    parts = [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
        body,
        f"--{boundary}--".encode(),
        b"",
    ]
    payload = b"\r\n".join(parts)
    return await client.post(
        "/api/media/upload",
        data=payload,
        headers={
            **_auth(client._tok),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )


async def test_upload_file_passthrough_pdf(client):
    """A PDF takes the file-passthrough branch and lands under media."""
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"x" * 200
    r = await _upload_file(
        client,
        filename="invoice.pdf",
        body=pdf_bytes,
        content_type="application/pdf",
    )
    assert r.status == 201
    body = await r.json()
    # ``url`` shape is ``api/media/<hex>.pdf`` — verify extension
    # preservation + sanitised hex filename.
    assert body["url"].startswith("api/media/")
    assert body["filename"].endswith(".pdf")
    # The bytes round-trip verbatim — no transcoding for files.
    cfg = client.app[config_key]
    stored = pathlib.Path(cfg.media_path) / body["filename"]
    assert stored.read_bytes() == pdf_bytes


async def test_upload_file_rejects_executable_ext(client):
    """``.exe`` (and other native-execute extensions) get a 422."""
    r = await _upload_file(
        client,
        filename="malware.exe",
        body=b"MZ\x90" + b"\x00" * 50,
        content_type="application/x-msdownload",
    )
    assert r.status == 422


async def test_upload_file_rejects_too_large(client):
    """Bytes above ``FILE_MAX_UPLOAD_BYTES`` return 413."""
    from socialhome.domain.media_constraints import FILE_MAX_UPLOAD_BYTES

    too_big = b"x" * (FILE_MAX_UPLOAD_BYTES + 1)
    r = await _upload_file(
        client,
        filename="huge.bin",
        body=too_big,
        content_type="application/octet-stream",
    )
    assert r.status == 413


async def test_upload_image_still_transcodes(client):
    """Sanity: the image path still runs through ImageProcessor.

    A real WebP from Pillow round-trips to a UUID ``.webp`` name
    so the image branch isn't accidentally diverted to the file
    passthrough.
    """
    import io
    from PIL import Image

    img = Image.new("RGB", (16, 16), color=(80, 200, 120))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=82)
    r = await _upload_file(
        client,
        filename="hello.webp",
        body=buf.getvalue(),
        content_type="image/webp",
    )
    assert r.status == 201
    body = await r.json()
    assert body["filename"].endswith(".webp")


async def test_upload_file_extension_sanitised(client):
    """An odd-case extension lands as a sanitised UUID name."""
    r = await _upload_file(
        client,
        filename="weird name.TAR.GZ",
        body=b"\x1f\x8b\x08" + b"\x00" * 100,
        content_type="application/gzip",
    )
    assert r.status == 201
    body = await r.json()
    # Extension picked from the original (lowercased, alnum-only)
    # — ``.gz`` is what ``_sanitise_file_ext`` returns from
    # ``Path('...').suffix``.
    assert body["filename"].endswith(".gz")


async def test_upload_file_no_extension(client):
    """Extensionless filenames get a UUID name with no suffix."""
    r = await _upload_file(
        client,
        filename="dump",
        body=b"hello world",
        content_type="application/octet-stream",
    )
    assert r.status == 201
    body = await r.json()
    # No extension — pure hex.
    assert "." not in body["filename"]


async def test_upload_missing_file_field_400(client):
    """An empty multipart body returns 400 with a clear error."""
    boundary = "----empty"
    payload = f"--{boundary}--\r\n".encode()
    r = await client.post(
        "/api/media/upload",
        data=payload,
        headers={
            **_auth(client._tok),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status == 400


async def test_upload_non_multipart_400(client):
    """JSON body rejected with 400."""
    r = await client.post(
        "/api/media/upload",
        json={"foo": "bar"},
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_get_media_path_traversal_400(client):
    """``..`` and slashes get a 400 before the file lookup."""
    r = await client.get(
        "/api/media/..%2Fpasswd",
        headers=_auth(client._tok),
    )
    # 400 or 404 both prove the route didn't read outside media_dir;
    # the path-traversal guard rejects anything with ``/`` or
    # leading ``.``.
    assert r.status in (400, 404)


async def test_upload_video_is_async(client):
    """A video upload returns 201 with ``media_status='processing'`` and
    a ``.webm`` URL, enqueues exactly one transcode job, and does NOT
    write the output file inline (it's still queued)."""
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    # Stop the background loop so it can't drain (or reschedule, failing
    # the real transcode on these fake bytes) the row before we assert
    # it's queued.
    await client.app[media_transcode_service_key].stop()

    r = await _upload_file(
        client,
        filename="clip.mp4",
        body=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
        content_type="video/mp4",
    )
    assert r.status == 201
    body = await r.json()
    assert body["media_status"] == "processing"
    assert body["url"].startswith("api/media/")
    assert body["url"].endswith(".webm")
    assert body["thumbnail_url"].endswith(".webp")
    out_name = body["filename"]
    assert out_name.endswith(".webm")

    # The ``.webm`` output + ``.webp`` poster share one UUID stem so the
    # poster path is derivable from the media URL server-side (no
    # thumbnail column on feed/DM/moment rows).
    stem = body["url"][len("api/media/") : -len(".webm")]
    assert body["thumbnail_url"] == f"api/media/{stem}.webp"

    # Exactly one job enqueued, keyed by the returned output filename.
    repo = client.app[media_transcode_repo_key]
    due = await repo.list_due()
    assert len(due) == 1
    assert due[0].output_filename == out_name

    # The .webm doesn't exist yet — it's still queued, not transcoded.
    cfg = client.app[config_key]
    assert not (pathlib.Path(cfg.media_path) / out_name).exists()


async def test_upload_video_flush_produces_file(client, monkeypatch):
    """Driving ``flush_once`` with a stub processor writes the output +
    poster and clears the job (readiness == absent row)."""
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    svc = client.app[media_transcode_service_key]
    # Stop the background loop first so it can't race ``flush_once`` (the
    # real VideoProcessor would fail on the fake bytes + reschedule the
    # row past its next-attempt window). With the loop stopped + a stub
    # processor wired, the flush below is deterministic.
    await svc.stop()

    class _StubProcessor:
        async def process(self, src, name):
            return b"WEBMDATA", "out.webm"

        async def generate_thumbnail(self, src):
            return b"WEBPDATA"

    monkeypatch.setattr(svc, "_processor", _StubProcessor())

    r = await _upload_file(
        client,
        filename="clip.mp4",
        body=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
        content_type="video/mp4",
    )
    assert r.status == 201
    body = await r.json()
    out_name = body["filename"]
    thumb_name = body["thumbnail_url"].rsplit("/", 1)[-1]

    done = await svc.flush_once()
    assert done == 1

    cfg = client.app[config_key]
    media_dir = pathlib.Path(cfg.media_path)
    assert (media_dir / out_name).read_bytes() == b"WEBMDATA"
    assert (media_dir / thumb_name).read_bytes() == b"WEBPDATA"

    # Completed job is deleted — nothing left due.
    repo = client.app[media_transcode_repo_key]
    assert await repo.list_due() == []


async def test_signed_url_for_user_picture(client):
    """Same scheme works against ``/api/users/{id}/picture`` —
    avatars rely on it. We don't need a real picture row; the
    auth-strategy decision happens before the route handler runs.
    Without a stored picture the route returns 404, so we just assert
    that the response is *not* 401 (i.e. signed-URL auth succeeded)."""
    signer = client.app[media_signer_key]
    canonical = f"/api/users/{client._uid}/picture"
    signed = signer.sign(canonical)
    r = await client.get(signed)
    # 404 (no picture set) or 200 (rare) both prove auth passed.
    assert r.status != 401


# ── Sandboxed media responses ────────────────────────────────────────────

_SVG_WITH_SCRIPT = (
    b'<svg xmlns="http://www.w3.org/2000/svg">'
    b"<script>localStorage.getItem('sh_token')</script></svg>"
)


def _store(client, name: str, data: bytes) -> str:
    media_dir = pathlib.Path(client.app[config_key].media_path)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / name).write_bytes(data)
    return f"/api/media/{name}"


async def test_media_response_carries_sandbox_csp(client, media_file):
    r = await client.get(media_file, headers=_auth(client._tok))
    assert r.status == 200
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_jpeg_still_served_inline_as_image(client):
    url = _store(client, "photo.jpg", b"\xff\xd8\xff\xe0fakejpeg")
    r = await client.get(url, headers=_auth(client._tok))
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/jpeg"
    assert r.headers["Content-Disposition"] == 'inline; filename="photo.jpg"'
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("x.svg", _SVG_WITH_SCRIPT),
        ("x.html", b"<script>alert(document.domain)</script>"),
        ("x.xml", b"<?xml version='1.0'?><a/>"),
        ("x.js", b"alert(1)"),
        ("x.unknownext", b"??"),
    ],
)
async def test_script_capable_files_download_as_octet_stream(client, name, data):
    """A stored ``.svg`` / ``.html`` / … opened directly must never
    render on the app origin (the bearer token is in localStorage)."""
    url = _store(client, name, data)
    r = await client.get(url, headers=_auth(client._tok))
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert r.headers["Content-Disposition"] == f'attachment; filename="{name}"'
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP
    assert await r.read() == data


async def test_video_is_inline_without_sandbox_so_it_plays(client):
    url = _store(client, "clip.webm", b"\x1a\x45\xdf\xa3")
    r = await client.get(url, headers=_auth(client._tok))
    assert r.headers["Content-Type"] == "video/webm"
    assert r.headers["Content-Disposition"].startswith("inline;")
    assert r.headers["Content-Security-Policy"] == PLAYABLE_MEDIA_CSP


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        # Voice notes — ``AudioProcessor`` keeps the container extension.
        ("0a1b.ogg", b"OggS\x00\x02", "audio/ogg"),  # Firefox
        ("0a1c.webm", b"\x1a\x45\xdf\xa3", "video/webm"),  # Chromium
        ("0a1d.m4a", b"\x00\x00\x00\x20ftypM4A ", "audio/mp4"),  # Safari
        # Video post — ``MediaTranscodeService`` writes ``<stem>.webm``.
        ("0b2c.webm", b"\x1a\x45\xdf\xa3", "video/webm"),
        # DM video / voice note received over federation —
        # ``<message_id><ext>`` from ``_mime_to_ext``.
        ("msg-123.webm", b"\x1a\x45\xdf\xa3", "video/webm"),
        ("msg-124.ogg", b"OggS\x00\x02", "audio/ogg"),
        ("msg-125.m4a", b"\x00\x00\x00\x20ftypM4A ", "audio/mp4"),
    ],
)
async def test_server_written_audio_and_video_play_inline(client, name, data, mime):
    url = _store(client, name, data)
    r = await client.get(url, headers=_auth(client._tok))
    assert r.status == 200
    assert r.headers["Content-Type"] == mime
    assert r.headers["Content-Disposition"] == f'inline; filename="{name}"'
    assert r.headers["Content-Security-Policy"] == PLAYABLE_MEDIA_CSP
    assert "form-action 'none'" in r.headers["Content-Security-Policy"]
    assert await r.read() == data


@pytest.mark.parametrize(
    "name",
    ["song.mp3", "clip.mov", "clip.mp4", "list.m3u8", "list.m3u", "a.flac"],
)
async def test_foreign_audio_and_video_download_sandboxed(client, name):
    """The file passthrough can store any extension; only the types
    the server itself writes play unsandboxed."""
    url = _store(client, name, b"\x00\x01")
    r = await client.get(url, headers=_auth(client._tok))
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert r.headers["Content-Disposition"] == f'attachment; filename="{name}"'
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP


async def test_pdf_and_text_are_inline(client):
    for name, mime in (("doc.pdf", "application/pdf"), ("notes.txt", "text/plain")):
        url = _store(client, name, b"%PDF-1.4" if name.endswith("pdf") else b"hi")
        r = await client.get(url, headers=_auth(client._tok))
        assert r.headers["Content-Type"] == mime
        assert r.headers["Content-Disposition"].startswith("inline;")
        assert r.headers["Content-Security-Policy"] == MEDIA_CSP


async def test_user_picture_carries_sandbox_csp(client):
    repo = client.app[profile_picture_repo_key]
    await repo.set_user_picture(
        client._uid, bytes_webp=b"RIFF....WEBP", hash="h1", width=1, height=1
    )
    r = await client.get(
        f"/api/users/{client._uid}/picture", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/webp"
    assert r.headers["Content-Security-Policy"] == MEDIA_CSP

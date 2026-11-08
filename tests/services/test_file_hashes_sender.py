"""The ``files`` map sender mixin (:mod:`socialhome.services.file_hashes_sender`)."""

from __future__ import annotations

import hashlib
import pathlib

from socialhome.services.file_hashes_sender import FileHashesMixin
from socialhome.services.media_file_hasher import MediaFileHasher


class _Sender(FileHashesMixin):
    __slots__ = ("_file_hasher",)

    def __init__(self) -> None:
        self._file_hasher = None


async def test_without_a_hasher_the_payload_is_unchanged() -> None:
    payload = {"id": "p"}
    assert await _Sender()._with_file_hashes(payload, ["api/media/x"]) is payload


async def test_a_wired_hasher_adds_the_map(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.webp").write_bytes(b"x")
    sender = _Sender()
    sender.attach_file_hasher(MediaFileHasher(tmp_path))
    out = await sender._with_file_hashes({"id": "p"}, ["api/media/x.webp"])
    assert out == {
        "id": "p",
        "files": {"x.webp": {"h": hashlib.sha256(b"x").hexdigest(), "size": 1}},
        "files_suite": "sha256",
    }


async def test_a_hasher_error_is_fail_soft(tmp_path: pathlib.Path) -> None:
    class _Broken(MediaFileHasher):
        async def with_hashes(self, payload, urls):  # type: ignore[override]
            raise OSError("disk gone")

    sender = _Sender()
    sender.attach_file_hasher(_Broken(tmp_path))
    assert await sender._with_file_hashes({"id": "p"}, ["api/media/x"]) == {"id": "p"}

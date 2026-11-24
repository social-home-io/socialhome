"""Full HTTP-mocked tests for :class:`MomentPublicService`.

Mirrors the shape of ``test_highlight_publication_service.py``:
stubs the aiohttp session so the sign-then-POST pattern is
exercised end-to-end without a real socket, and asserts the local
state transitions on every flow (register / deregister / follow /
unfollow / set_default_share / fetch_directory).
"""

from __future__ import annotations

import pytest

from socialhome.domain.child_protection import AccountProtectedError
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_service import (
    MomentPublicError,
    MomentPublicFollowAtHomeError,
    MomentPublicService,
)


class _StubResp:
    def __init__(self, status: int = 201, body: dict | None = None) -> None:
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _StubSession:
    """Captures every call so tests can assert URL + body shape."""

    def __init__(self, *, status: int = 201, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.params: list[dict | None] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self.status, self.body)

    def get(self, url, *, params=None, **_kw):
        self.gets.append(url)
        self.params.append(params)
        return _StubResp(self.status, self.body)


@pytest.fixture
async def repos(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) VALUES('g1','gfs-1','GFS One','ff'*32,"
        "'https://gfs1.example','active', datetime('now'))"
    )
    return {
        "regs": SqliteMomentPublicRegistrationRepo(db),
        "follows": SqliteMomentPublicFollowRepo(db),
        "users": SqliteUserRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
    }


def _make_service(repos, *, session, federation=None):
    svc = MomentPublicService(
        repos["regs"],
        repos["follows"],
        repos["users"],
        repos["gfs"],
        federation_repo=federation,
    )
    svc.attach_session(session)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    return svc


async def test_register_signs_body_and_persists_local_row(repos):
    sess = _StubSession(body={"user_id": "u1"})
    svc = _make_service(repos, session=sess)
    reg = await svc.register(user_id="u1", gfs_id="g1", default_share=True)
    assert reg.user_id == "u1" and reg.gfs_id == "g1"
    assert sess.posts and sess.posts[0][0].endswith("/gfs/moments/users/register")
    body = sess.posts[0][1]
    assert body["user_id"] == "u1"
    assert body["instance_id"] == "inst-self"
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert body["home_instance_pk"]  # 64 hex chars
    assert "signature" in body
    saved = await repos["regs"].get(user_id="u1", gfs_id="g1")
    assert saved is not None and saved.default_share is True


async def test_register_unknown_user_raises_lookup(repos):
    svc = _make_service(repos, session=_StubSession())
    with pytest.raises(LookupError):
        await svc.register(user_id="nobody", gfs_id="g1")


async def test_register_unknown_gfs_raises(repos):
    svc = _make_service(repos, session=_StubSession())
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="missing-gfs")


async def test_register_propagates_gfs_500(repos):
    svc = _make_service(repos, session=_StubSession(status=500))
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


async def test_set_default_share_then_is_registered(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1", default_share=True)
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is True
    assert await svc.default_share(user_id="u1", gfs_id="g1") is True
    await svc.set_default_share(user_id="u1", gfs_id="g1", default_share=False)
    assert await svc.default_share(user_id="u1", gfs_id="g1") is False


async def test_deregister_clears_local_even_when_gfs_returns_404(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1")
    sess_404 = _StubSession(status=404, body={})
    svc._http_client = sess_404  # type: ignore[assignment]
    await svc.deregister(user_id="u1", gfs_id="g1")
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is False
    # The deregister still POSTs (so the GFS can clean up) before the
    # local row drops.
    assert sess_404.posts and sess_404.posts[0][0].endswith(
        "/gfs/moments/users/u1/deregister"
    )


async def test_list_registrations_round_trip(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1")
    rows = await svc.list_registrations("u1")
    assert len(rows) == 1 and rows[0].gfs_id == "g1"


async def test_follow_caches_directory_entry_locally(repos):
    sess = _StubSession(
        body={
            "user": {
                "user_id": "u-remote",
                "username": "bob",
                "display_name": "Bob",
                "picture_url": None,
                "home_instance_pk": "ab" * 32,
                "instance_id": "inst-remote",
            },
            "follow": {
                "follower_user_id": "u1",
                "followed_user_id": "u-remote",
                "created_at": 1,
            },
        }
    )
    svc = _make_service(repos, session=sess)
    f = await svc.follow(
        follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
    )
    assert f.followed_user_id == "u-remote"
    assert f.followed_instance_pk == "ab" * 32
    assert f.followed_username == "bob"
    assert f.followed_display_name == "Bob"
    body = sess.posts[0][1]
    assert body["follower_user_id"] == "u1"
    assert body["follower_instance_id"] == "inst-self"
    assert "signature" in body


async def test_follow_propagates_gfs_failure(repos):
    sess = _StubSession(status=500, body={"error": "boom"})
    svc = _make_service(repos, session=sess)
    with pytest.raises(MomentPublicError):
        await svc.follow(
            follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
        )


async def test_unfollow_drops_local_row(repos):
    # Seed a follow row first.
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    rows = await repos["follows"].list_for_follower("u1")
    assert rows == []


async def test_list_follows_round_trip(repos):
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = _make_service(repos, session=_StubSession())
    rows = await svc.list_follows("u1")
    assert len(rows) == 1 and rows[0].followed_username == "bob"


async def test_fetch_directory_returns_users_list(repos):
    svc = _make_service(
        repos,
        session=_StubSession(
            status=200,
            body={
                "users": [
                    {
                        "user_id": "u-remote",
                        "instance_id": "i-b",
                        "display_name": "Bob",
                    },
                ]
            },
        ),
    )
    users = list((await svc.fetch_directory("g1")).users)
    assert users == [
        {
            "user_id": "u-remote",
            "instance_id": "i-b",
            "display_name": "Bob",
            "username": None,
            "bio": None,
        }
    ]


async def test_fetch_directory_keeps_text_fields_text_and_at_most_200_rows(repos):
    """The GFS chose every field: what the page shows and searches is text
    or nothing, and an answer holds at most 200 rows."""
    rows = [
        {"user_id": f"x{i}", "instance_id": f"i{i}", "display_name": f"P{i}"}
        for i in range(250)
    ]
    rows[0] = {
        "user_id": "x0",
        "instance_id": "i0",
        "display_name": {"evil": 1},
        "username": ["a"],
        "bio": 7,
    }
    sess = _StubSession(status=200, body={"users": rows})
    svc = _make_service(repos, session=sess)
    users = (await svc.fetch_directory("g1")).users
    assert len(users) == 200
    assert (users[0]["display_name"], users[0]["username"], users[0]["bio"]) == (
        None,
        None,
        None,
    )
    assert users[1]["display_name"] == "P1"


def _ranked_body(sort, counts):
    return {
        "sort": sort,
        "users": [
            {"user_id": f"x{i}", "instance_id": f"i{i}", "follower_count": n}
            for i, n in enumerate(counts)
        ],
    }


async def test_fetch_directory_asks_for_a_sort_and_trusts_only_a_matching_echo(
    repos,
):
    sess = _StubSession(status=200, body=_ranked_body("followers", [5]))
    svc = _make_service(repos, session=sess)
    page = await svc.fetch_directory("g1", sort="followers")
    assert sess.params[-1] == {"sort": "followers"}
    assert page.sort == "followers"
    # An older GFS ignores the parameter: no echo, so no ranking claimed.
    sess.body = {"users": []}
    assert (await svc.fetch_directory("g1", sort="followers")).sort is None
    # An echo that is not what was asked for is not believed either.
    sess.body = _ranked_body("new", [])
    assert (await svc.fetch_directory("g1", sort="followers")).sort is None
    sess.body = _ranked_body(["followers"], [])
    assert (await svc.fetch_directory("g1", sort="followers")).sort is None
    # No sort asked: none sent, none claimed.
    sess.body = _ranked_body("followers", [])
    assert (await svc.fetch_directory("g1")).sort is None
    assert sess.params[-1] is None


async def test_fetch_directory_refuses_an_unknown_sort(repos):
    sess = _StubSession(status=200, body={"users": []})
    svc = _make_service(repos, session=sess)
    with pytest.raises(ValueError):
        await svc.fetch_directory("g1", sort="recent")
    # A search is never ranked (the GFS refuses it too): ours to refuse, 422.
    with pytest.raises(ValueError):
        await svc.fetch_directory("g1", q="bern", sort="followers")
    assert sess.gets == []


async def test_fetch_directory_keeps_only_plausible_follower_counts(repos):
    """The GFS's counts are display data: a non-negative integer of sane size
    or nothing."""
    counts = [3, -1, "5", True, 10**12, 2.5, None, 0]
    sess = _StubSession(status=200, body=_ranked_body("followers", counts))
    svc = _make_service(repos, session=sess)
    users = (await svc.fetch_directory("g1", sort="followers")).users
    kept = {u["user_id"]: u.get("follower_count") for u in users}
    assert kept == {
        "x0": 3,
        "x1": None,
        "x2": None,
        "x3": None,
        "x4": None,
        "x5": None,
        "x6": None,
        "x7": 0,
    }
    assert all("follower_count" not in u for u in users if kept[u["user_id"]] is None)


async def test_fetch_directory_survives_a_body_that_is_not_an_object(repos):
    sess = _StubSession(status=200, body=None)
    sess.body = ["not", "an", "object"]
    svc = _make_service(repos, session=sess)
    page = await svc.fetch_directory("g1", sort="followers")
    assert page.users == () and page.sort is None


async def test_fetch_directory_bad_json_is_a_gfs_failure(repos):
    """A GFS answering 200 with junk is a GFS failure (502), not our 422."""

    class _BadJson(_StubResp):
        async def json(self):
            raise ValueError("not json")

    sess = _StubSession(status=200)
    sess.get = lambda url, **_kw: _BadJson(200)
    svc = _make_service(repos, session=sess)
    with pytest.raises(MomentPublicError):
        await svc.fetch_directory("g1", sort="followers")


async def test_fetch_directory_5xx_raises(repos):
    svc = _make_service(repos, session=_StubSession(status=500, body={}))
    with pytest.raises(MomentPublicError):
        await svc.fetch_directory("g1")


async def test_service_used_before_attach_raises(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    # No attach_session/attach_identity → calls should fail noisily.
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


# ── ``aiohttp.ClientError`` paths ───────────────────────────────────────


class _RaisingSession:
    """Session whose POSTs raise ``aiohttp.ClientError`` synchronously."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url, *, json=None, **_kw):
        import aiohttp

        self.posts.append(url)
        raise aiohttp.ClientError("connection refused")

    def get(self, url, **_kw):
        import aiohttp

        raise aiohttp.ClientError("connection refused")


async def test_register_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.register(user_id="u1", gfs_id="g1")


async def test_follow_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.follow(
            follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
        )


async def test_fetch_directory_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.fetch_directory("g1")


async def test_deregister_swallows_client_error_but_clears_local(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    # Register first via a successful session.
    svc.attach_session(_StubSession(body={}))
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    await svc.register(user_id="u1", gfs_id="g1")
    # Now swap in a session that raises and call deregister.
    svc._http_client = _RaisingSession()  # type: ignore[assignment]
    await svc.deregister(user_id="u1", gfs_id="g1")
    # Local row dropped despite the GFS being unreachable.
    assert await svc.is_registered(user_id="u1", gfs_id="g1") is False


async def test_unfollow_swallows_client_error_but_clears_local(repos):
    # Seed a follow row.
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    rows = await repos["follows"].list_for_follower("u1")
    assert rows == []


# ── bio + picture push (§Momentum-public directory) ──────────────────────


async def test_register_carries_bio(repos, db):
    """``users.bio`` rides the register envelope to the GFS."""
    await db.enqueue("UPDATE users SET bio='Hello' WHERE user_id='u1'")
    sess = _StubSession(body={"user_id": "u1"})
    svc = _make_service(repos, session=sess)
    await svc.register(user_id="u1", gfs_id="g1")
    body = sess.posts[0][1]
    assert body["bio"] == "Hello"


async def test_register_pushes_picture_when_hash_changed(repos, db):
    """First register with a fresh ``picture_hash`` triggers picture upload."""
    from socialhome.repositories.profile_picture_repo import (
        SqliteProfilePictureRepo,
    )

    pic_repo = SqliteProfilePictureRepo(db)
    await pic_repo.set_user_picture(
        "u1",
        bytes_webp=b"webp-bytes",
        hash="aaaaaaaaaaaaaaa1",
        width=128,
        height=128,
    )
    await db.enqueue(
        "UPDATE users SET picture_hash='aaaaaaaaaaaaaaa1' WHERE user_id='u1'"
    )

    sess = _StubSession(body={"digest": "irrelevant"})
    svc = MomentPublicService(
        repos["regs"],
        repos["follows"],
        repos["users"],
        repos["gfs"],
        profile_picture_repo=pic_repo,
    )
    svc.attach_session(sess)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)

    await svc.register(user_id="u1", gfs_id="g1")
    upload_paths = [u for u, _ in sess.posts if u.endswith("/picture")]
    assert len(upload_paths) == 1


async def test_register_skips_picture_when_hash_matches(repos, db):
    """Second register with the same hash should NOT re-upload the picture."""
    from socialhome.repositories.profile_picture_repo import (
        SqliteProfilePictureRepo,
    )

    pic_repo = SqliteProfilePictureRepo(db)
    await pic_repo.set_user_picture(
        "u1",
        bytes_webp=b"webp-bytes",
        hash="aaaaaaaaaaaaaaa1",
        width=128,
        height=128,
    )
    await db.enqueue(
        "UPDATE users SET picture_hash='aaaaaaaaaaaaaaa1' WHERE user_id='u1'"
    )

    sess = _StubSession(body={})
    svc = MomentPublicService(
        repos["regs"],
        repos["follows"],
        repos["users"],
        repos["gfs"],
        profile_picture_repo=pic_repo,
    )
    svc.attach_session(sess)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    # First register → picture uploaded + ``last_picture_digest`` stamped.
    await svc.register(user_id="u1", gfs_id="g1")
    # Second register with the SAME picture_hash — picture upload skipped.
    await svc.register(user_id="u1", gfs_id="g1")
    upload_paths = [u for u, _ in sess.posts if u.endswith("/picture")]
    assert len(upload_paths) == 1


async def test_fetch_picture_proxies_bytes(repos):
    """``fetch_picture`` returns the (bytes, mime, etag) tuple."""

    class _BytesResp(_StubResp):
        def __init__(self):
            super().__init__(status=200)
            self.headers = {"Content-Type": "image/webp", "ETag": '"abc"'}

        async def read(self):
            return b"webp-payload"

    class _BytesSession(_StubSession):
        def get(self, url, **_kw):
            self.gets.append(url)
            return _BytesResp()

    svc = _make_service(repos, session=_BytesSession())
    out = await svc.fetch_picture("g1", "u-remote")
    assert out is not None
    raw, mime, digest = out
    assert raw == b"webp-payload"
    assert mime == "image/webp"
    assert digest == "abc"


async def test_fetch_picture_returns_none_on_404(repos):
    svc = _make_service(repos, session=_StubSession(status=404))
    out = await svc.fetch_picture("g1", "u-remote")
    assert out is None


async def test_fetch_picture_5xx_raises(repos):
    svc = _make_service(repos, session=_StubSession(status=503))
    with pytest.raises(MomentPublicError):
        await svc.fetch_picture("g1", "u-remote")


async def test_fetch_picture_raises_on_client_error(repos):
    svc = MomentPublicService(
        repos["regs"], repos["follows"], repos["users"], repos["gfs"]
    )
    svc.attach_session(_RaisingSession())  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(MomentPublicError):
        await svc.fetch_picture("g1", "u-remote")


async def test_push_picture_propagates_500(repos, db):
    """Picture upload failure logs but doesn't break the register call."""
    from socialhome.repositories.profile_picture_repo import (
        SqliteProfilePictureRepo,
    )

    pic_repo = SqliteProfilePictureRepo(db)
    await pic_repo.set_user_picture(
        "u1",
        bytes_webp=b"webp",
        hash="aaaaaaaaaaaaaaa1",
        width=64,
        height=64,
    )
    await db.enqueue(
        "UPDATE users SET picture_hash='aaaaaaaaaaaaaaa1' WHERE user_id='u1'"
    )

    # Sequential responses: register success, picture upload 500.
    class _SeqSession:
        def __init__(self):
            self.posts = []
            self._statuses = [201, 500]

        def post(self, url, *, json=None, **_kw):
            self.posts.append(url)
            status = self._statuses.pop(0) if self._statuses else 201
            return _StubResp(status, {})

        def get(self, url, **_kw):
            return _StubResp(200, {})

    sess = _SeqSession()
    svc = MomentPublicService(
        repos["regs"],
        repos["follows"],
        repos["users"],
        repos["gfs"],
        profile_picture_repo=pic_repo,
    )
    svc.attach_session(sess)  # type: ignore[arg-type]
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    # register itself succeeds; the picture push 500 is logged but
    # does not propagate.
    await svc.register(user_id="u1", gfs_id="g1")
    upload_paths = [u for u in sess.posts if u.endswith("/picture")]
    assert len(upload_paths) == 1


def _follow_reply(**user) -> dict:
    return {
        "user": {
            "user_id": "u-remote",
            "username": "bob",
            "display_name": "Bob",
            "home_instance_pk": "ab" * 32,
            "instance_id": "inst-remote",
            **user,
        },
        "follow": {
            "follower_user_id": "u1",
            "followed_user_id": "u-remote",
            "created_at": 1,
        },
    }


async def test_follow_keeps_the_directory_bio_and_picture_digest(repos):
    """The GFS directory's full SHA-256 is kept in the household's 16-char
    form — it only busts the browser cache on the proxy URL."""
    sess = _StubSession(
        body=_follow_reply(bio="Builds boats.", picture_digest="0123456789abcdef" * 4)
    )
    svc = _make_service(repos, session=sess)
    f = await svc.follow(
        follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
    )
    assert f.followed_bio == "Builds boats."
    assert f.followed_picture_digest == "0123456789abcdef"


async def test_follow_stores_no_name_for_an_author_named_by_their_id(repos):
    sess = _StubSession(
        body=_follow_reply(display_name="u-remote", bio=None, picture_digest="x/../y")
    )
    svc = _make_service(repos, session=sess)
    f = await svc.follow(
        follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote"
    )
    assert f.followed_display_name == ""
    assert f.public_name == "bob"
    assert f.followed_bio is None and f.followed_picture_digest is None


# ── Encrypted public moments (v_62) ───────────────────────────────────────


class _Forgets:
    def __init__(self) -> None:
        self.authors: list[str] = []

    async def forget_if_unfollowed(self, author_user_id: str) -> None:
        self.authors.append(author_user_id)


class _Caps:
    async def encrypted_moments_supported(self, gfs_id: str) -> bool:
        return gfs_id == "g1"

    async def moment_counts_supported(self, gfs_id: str) -> bool:
        return False


async def test_unfollow_lets_the_key_store_forget_the_author(repos):
    svc = _make_service(repos, session=_StubSession(body={}))
    forgets = _Forgets()
    svc.attach_key_inbound(forgets)  # type: ignore[arg-type]
    await svc.unfollow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    assert forgets.authors == ["u-remote"]


async def test_encrypted_delivery_reads_the_servers_proof(repos):
    svc = _make_service(repos, session=_StubSession())
    # Not wired: fail closed — reported as no delivery.
    assert await svc.encrypted_delivery("g1") is False
    svc.attach_capabilities(_Caps())  # type: ignore[arg-type]
    assert await svc.encrypted_delivery("g1") is True
    assert await svc.encrypted_delivery("g2") is False


async def test_follower_households_unable_reads_the_gfs_count(repos):
    sess = _StubSession(status=200, body={"households_unable": 3})
    svc = _make_service(repos, session=sess)
    assert await svc.follower_households_unable(user_id="u1", gfs_id="g1") == 3
    url, body = sess.posts[0]
    assert url == "https://gfs1.example/gfs/moments/users/u1/follower_stats"
    assert body["instance_id"] == "inst-self" and "signature" in body


async def test_follower_households_unable_is_none_when_unknown(repos):
    for sess in (
        _StubSession(status=403),
        _StubSession(status=200, body={"households_unable": "x"}),
    ):
        svc = _make_service(repos, session=sess)
        assert await svc.follower_households_unable(user_id="u1", gfs_id="g1") is None
    svc = _make_service(repos, session=_StubSession())
    assert await svc.follower_households_unable(user_id="u1", gfs_id="nope") is None


async def test_a_follow_declares_what_the_household_can_open(repos):
    sess = _StubSession(body=_follow_reply())
    svc = _make_service(repos, session=sess)
    await svc.follow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    body = sess.posts[0][1]
    assert body["moment_suites"] == {"kem": ["x25519"], "seal": ["aesgcm-256"]}
    assert "signature" in body


async def test_suites_are_declared_once_to_a_server_we_follow_through(repos):
    sess = _StubSession(body={"updated": 1}, status=200)
    svc = _make_service(repos, session=sess)
    svc.attach_capabilities(_Caps())  # type: ignore[arg-type]
    # Nobody here follows anyone through g1 yet: nothing is sent.
    assert await svc.declare_suites("g1") is False
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    assert await svc.declare_suites("g1") is True
    [(url, body)] = sess.posts
    assert url == "https://gfs1.example/gfs/moments/suites"
    assert body["instance_id"] == "inst-self" and body["moment_suites"]
    assert "signature" in body
    # Once per process per server.
    assert await svc.declare_suites("g1") is False
    assert len(sess.posts) == 1


async def test_suites_are_not_declared_to_an_unproven_or_refusing_server(repos):
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    sess = _StubSession(status=422)
    svc = _make_service(repos, session=sess)
    assert await svc.declare_suites("g1") is False  # capabilities unwired
    assert sess.posts == []
    svc.attach_capabilities(_Caps())  # type: ignore[arg-type]
    assert await svc.declare_suites("g1") is False  # refused
    assert len(sess.posts) == 1
    assert await svc.declare_suites("g1") is False  # retried next time
    assert len(sess.posts) == 2


class _Marks:
    def __init__(self) -> None:
        self.marked: list[tuple[str, str]] = []

    async def mark_rotation_due(self, author_user_id: str, *, reason: str) -> bool:
        self.marked.append((author_user_id, reason))
        return True


async def test_deregister_marks_the_authors_key_for_rotation(repos):
    """Followers on that server keep their key; the next publish anywhere
    must start a new generation (lazy rotation)."""
    await repos["regs"].upsert(user_id="u1", gfs_id="g1", default_share=True)
    svc = _make_service(repos, session=_StubSession(body={}))
    marks = _Marks()
    svc.attach_key_outbound(marks)  # type: ignore[arg-type]
    await svc.deregister(user_id="u1", gfs_id="g1")
    assert marks.marked == [("u1", "a registration ended")]


class _CountsCaps(_Caps):
    def __init__(self) -> None:
        self.counts = False

    async def moment_counts_supported(self, gfs_id: str) -> bool:
        return self.counts and gfs_id == "g1"


async def test_ops_are_declared_only_to_a_server_that_takes_them(repos):
    """v_69: ``ops: ["counts"]`` rides a follow or a declaration only to a
    server that proved ``moment_counts`` (an older one refuses the key) —
    and once a server proves it, the household declares again, with it."""
    caps = _CountsCaps()
    sess = _StubSession(body={"updated": 1}, status=200)
    svc = _make_service(repos, session=sess)
    svc.attach_capabilities(caps)  # type: ignore[arg-type]
    await repos["follows"].upsert(
        follower_user_id="u1",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk="ab" * 32,
        followed_username="bob",
        followed_display_name="Bob",
    )
    assert await svc.declare_suites("g1") is True
    assert "ops" not in sess.posts[-1][1]["moment_suites"]
    assert await svc.declare_suites("g1") is False
    caps.counts = True  # the server upgraded
    assert await svc.declare_suites("g1") is True
    assert sess.posts[-1][1]["moment_suites"]["ops"] == ["counts"]
    assert await svc.declare_suites("g1") is False
    assert len(sess.posts) == 2


async def test_a_follow_declares_ops_where_the_server_takes_them(repos):
    caps = _CountsCaps()
    caps.counts = True
    sess = _StubSession(body=_follow_reply())
    svc = _make_service(repos, session=sess)
    svc.attach_capabilities(caps)  # type: ignore[arg-type]
    await svc.follow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-remote")
    assert sess.posts[0][1]["moment_suites"]["ops"] == ["counts"]


async def test_new_registration_does_not_share_by_default(repos):
    """Owner decision 4: registering lists you; each moment is public only
    when its author switches it on."""
    svc = _make_service(repos, session=_StubSession(body={}))
    reg = await svc.register(user_id="u1", gfs_id="g1")
    assert reg.default_share is False
    assert await svc.default_share(user_id="u1", gfs_id="g1") is False


async def test_re_registering_keeps_the_existing_setting(repos):
    """Existing registrations keep their "Make new moments public"."""
    svc = _make_service(repos, session=_StubSession(body={}))
    await svc.register(user_id="u1", gfs_id="g1", default_share=True)
    reg = await svc.register(user_id="u1", gfs_id="g1")
    assert reg.default_share is True


# ── one Follow: people this household knows never get a GFS follow ──────


async def _seed_households(db) -> None:
    """A paired household (inst-pal, Pat) and a household met only in a
    shared space (inst-space, Sam)."""
    for iid, source in (("inst-pal", "manual"), ("inst-space", "space_session")):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,'confirmed',?)",
            (iid, iid, "aa" * 32, "x", "x", "https://x/inbox", f"in-{iid}", source),
        )
    for uid, iid, name in (
        ("u-pat", "inst-pal", "pat"),
        ("u-sam", "inst-space", "sam"),
    ):
        await db.enqueue(
            "INSERT INTO remote_users(user_id, instance_id, remote_username,"
            " display_name) VALUES(?,?,?,?)",
            (uid, iid, name, name.title()),
        )


async def test_fetch_directory_leaves_out_people_the_household_knows(repos, db):
    await _seed_households(db)
    rows = [
        {"user_id": "u1", "instance_id": "inst-self"},  # a local user
        {"user_id": "u-other", "instance_id": "inst-self"},  # our household
        {"user_id": "u-pal2", "instance_id": "inst-pal"},  # a paired household
        # A paired person under a made-up household — the GFS chose it.
        {"user_id": "u-pat", "instance_id": "inst-forged"},
        {"user_id": "u-sam", "instance_id": "inst-space"},  # space only: a stranger
        {"user_id": "u-stranger", "instance_id": "inst-far"},
        {"user_id": "u-stranger", "instance_id": "inst-far"},  # a repeat
        "not a row",
        {"user_id": "u-odd", "instance_id": ["not", "hashable"]},
        {"user_id": "u-nohome"},
    ]
    svc = _make_service(
        repos,
        session=_StubSession(status=200, body={"users": rows}),
        federation=SqliteFederationRepo(db),
    )
    users = list((await svc.fetch_directory("g1")).users)
    assert [u["user_id"] for u in users] == ["u-sam", "u-stranger"]


class _ProtectedCp:
    """Stub ChildProtectionService: every listed user is protected."""

    def __init__(self, protected: set[str]) -> None:
        self._protected = protected

    def register_gate(self, gate):
        pass

    async def require_unrestricted(self, user_id, capability):
        if user_id in self._protected:
            raise AccountProtectedError(capability)


async def test_fetch_directory_refuses_a_protected_viewer(repos):
    sess = _StubSession(status=200, body={"users": []})
    svc = _make_service(repos, session=sess)
    svc.attach_child_protection(_ProtectedCp({"u-kid"}))
    with pytest.raises(AccountProtectedError):
        await svc.fetch_directory("g1", viewer_user_id="u-kid")
    assert sess.gets == []
    assert (await svc.fetch_directory("g1", viewer_user_id="u1")).users == ()


async def test_follow_refuses_someone_the_household_knows(repos, db):
    await _seed_households(db)
    sess = _StubSession(status=201, body={"user": {"display_name": "X"}})
    svc = _make_service(repos, session=sess, federation=SqliteFederationRepo(db))
    for known in ("u1", "u-pat"):
        with pytest.raises(MomentPublicFollowAtHomeError):
            await svc.follow(follower_user_id="u1", gfs_id="g1", followed_user_id=known)
    assert sess.posts == []
    # Someone met only in a shared space is not a friend: a GFS follow.
    await svc.follow(follower_user_id="u1", gfs_id="g1", followed_user_id="u-sam")
    assert sess.posts
    assert sess.posts[0][0].endswith("/gfs/moments/users/u-sam/follow")

"""Settings → Notifications: what a person hears about (redesign D4, D9, D10).

* **Kinds** — seven, each Push / In app / Off (:mod:`~socialhome.domain.
  notification_kinds`), stored in ``users.preferences_json["notif_kinds"]``;
  :class:`~socialhome.services.notification_service.NotificationService`
  honours them on every row.
* **Space levels** — Everything / Replies and mentions / Nothing; Nothing
  takes a duration (``space_notif_prefs.muted_until``, 0130) and gives the
  level from before back afterwards.
* **Muted posts** — "Mute this post" from a row's "…" menu, in
  ``preferences_json["notif_muted_posts"]`` (cap 200, lapse after 90 quiet
  days).
* **Muted and changed** — every space, chat and post not at its default or
  muted, so each mute made elsewhere can be found and undone.

Local only — nothing here federates.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..domain.conversation import (
    ConversationType,
    SystemChatScope,
    mute_active,
    mute_until_for,
)
from ..domain.notification import SpaceNotifLevel, default_space_notif_level
from ..domain.notification_kinds import (
    KIND_CHOICES,
    NotifChange,
    NotifDelivery,
    NotifKind,
    NotifSettings,
    SpaceNotifPref,
    mutable_post_key,
)
from ..domain.space import SpaceMember
from ..domain.user import User
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.notification_repo import AbstractNotificationRepo
from ..repositories.post_repo import AbstractPostRepo
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .user_preferences import (
    NOTIF_KINDS_KEY,
    NOTIF_MUTED_POSTS_KEY,
    parse_notif_settings,
)
from .user_service import UserService

log = logging.getLogger(__name__)

#: A muted post's line shows this many characters of the post.
SNIPPET_CHARS = 80

_LEVELS = frozenset(level.value for level in SpaceNotifLevel)


def space_choice_json(pref: SpaceNotifPref | None, default: str, now: datetime) -> dict:
    """What ``GET /api/spaces/{id}/notif-prefs`` says about one space: the
    level that applies now, the default, whether it is the default, and —
    while muted — until when and the level that comes back."""
    level = pref.effective(now) if pref is not None else default
    muted = pref is not None and pref.muted_now(now)
    then = None
    if muted and pref is not None:
        then = pref.level if pref.level != SpaceNotifLevel.MUTED else default
    return {
        "level": level,
        "is_default": pref is None,
        "default_level": default,
        "muted_until": pref.muted_until if muted and pref is not None else None,
        "then_level": then,
    }


class NotificationSettingsService:
    """Reads and writes one person's notification settings."""

    __slots__ = (
        "_convos",
        "_notifs",
        "_posts",
        "_space_posts",
        "_spaces",
        "_system_chats",
        "_user_svc",
        "_users",
    )

    def __init__(
        self,
        *,
        users: AbstractUserRepo,
        user_service: UserService,
        notifs: AbstractNotificationRepo,
        spaces: AbstractSpaceRepo,
        convos: AbstractConversationRepo,
        posts: AbstractPostRepo,
        space_posts: AbstractSpacePostRepo,
        system_chats=None,
    ) -> None:
        self._users = users
        self._user_svc = user_service
        self._notifs = notifs
        self._spaces = spaces
        self._convos = convos
        self._posts = posts
        self._space_posts = space_posts
        #: :class:`~socialhome.services.system_chat_policy.SystemChatPolicy`
        #: — a system chat's default level. Without it: household ``all``,
        #: space ``mentions`` (the policy's own values).
        self._system_chats = system_chats

    # ── Kinds ──────────────────────────────────────────────────────────

    async def settings(self, username: str) -> NotifSettings:
        user = await self.get_user(username)
        return parse_notif_settings(user.preferences_json)

    async def set_kind(self, username: str, kind: str, choice: str) -> NotifSettings:
        """Set *kind* to *choice*; :class:`ValueError` for a kind or a choice
        that does not exist (Needs you can't be Off, Messages and calls has
        no In app)."""
        try:
            k, c = NotifKind(kind), NotifDelivery(choice)
        except ValueError as exc:
            raise ValueError("unknown kind or choice") from exc
        if c not in KIND_CHOICES[k]:
            raise ValueError(f"{k.value} can't be {c.value}")
        current = await self.settings(username)
        kinds = {key.value: value.value for key, value in current.chosen.items()}
        kinds[k.value] = c.value
        user = await self._user_svc.patch_preferences(
            username, {NOTIF_KINDS_KEY: kinds}
        )
        return parse_notif_settings(user.preferences_json)

    # ── Posts ──────────────────────────────────────────────────────────

    async def mute_post(self, user: User, key: str) -> str:
        """Mute the post *key* for *user* (D10); returns the stamp. A post
        that isn't there, or sits in a space they're not in, is a
        :class:`KeyError`."""
        if not mutable_post_key(key):
            raise ValueError("not a post")
        if await self._post_line(user, key) is None:
            raise KeyError(key)
        now = datetime.now(UTC)
        current = parse_notif_settings(user.preferences_json)
        posts = current.with_muted_post(key, now=now)
        await self._user_svc.patch_preferences(
            user.username, {NOTIF_MUTED_POSTS_KEY: posts}
        )
        return posts[key]

    async def unmute_post(self, username: str, key: str) -> None:
        """Unmute the post *key* (a no-op when it wasn't muted)."""
        current = await self.settings(username)
        if key not in current.muted_posts:
            return
        posts = {k: v for k, v in current.muted_posts.items() if k != key}
        await self._user_svc.patch_preferences(
            username, {NOTIF_MUTED_POSTS_KEY: posts or None}
        )

    # ── Spaces ─────────────────────────────────────────────────────────

    async def _seat(self, user_id: str, space_id: str):
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise PermissionError("Not a space member.")
        space = await self._spaces.get(space_id)
        if space is None:
            raise KeyError(space_id)
        return space, member

    async def space_choice(self, user_id: str, space_id: str) -> dict:
        """The caller's level in *space_id* (see :func:`space_choice_json`)."""
        space, member = await self._seat(user_id, space_id)
        pref = await self._notifs.get_space_notif_pref(
            user_id=user_id, space_id=space_id
        )
        default = default_space_notif_level(space.space_type, member.role).value
        return space_choice_json(pref, default, datetime.now(UTC))

    async def set_space_level(
        self,
        user_id: str,
        space_id: str,
        level: str,
        duration: str | None = None,
    ) -> dict:
        """Set the caller's level in *space_id* (D9).

        ``all`` / ``mentions`` apply as they are (a running mute ends).
        ``muted`` mutes for *duration* (``1h`` / ``8h`` / ``1w`` /
        ``forever``, default ``forever``) and keeps the level from before
        to come back to. :class:`ValueError` for anything else."""
        if level not in _LEVELS:
            raise ValueError("level must be one of 'all','mentions','muted'.")
        space, member = await self._seat(user_id, space_id)
        default = default_space_notif_level(space.space_type, member.role).value
        now = datetime.now(UTC)
        if level == SpaceNotifLevel.MUTED:
            until = mute_until_for(duration or "forever", now=now)
            current = await self._notifs.get_space_notif_pref(
                user_id=user_id, space_id=space_id
            )
            before = current.level if current is not None else default
            if before == SpaceNotifLevel.MUTED:
                before = default
            await self._notifs.set_space_notif_level(
                user_id=user_id, space_id=space_id, level=before, muted_until=until
            )
        else:
            if duration is not None:
                raise ValueError("only 'muted' takes a duration")
            await self._notifs.set_space_notif_level(
                user_id=user_id, space_id=space_id, level=level
            )
        pref = await self._notifs.get_space_notif_pref(
            user_id=user_id, space_id=space_id
        )
        return space_choice_json(pref, default, now)

    # ── Muted and changed ──────────────────────────────────────────────

    async def changes(self, user: User) -> list[NotifChange]:
        """Every space, chat and post *user* set off its default or muted:
        spaces, then chats, then posts."""
        now = datetime.now(UTC)
        out = await self._space_changes(user, now)
        out += await self._chat_changes(user, now)
        settings = parse_notif_settings(user.preferences_json)
        for key, _stamp in sorted(
            settings.active_muted_posts(now=now).items(),
            key=lambda kv: kv[1],
            reverse=True,
        ):
            line = await self._post_line(user, key)
            if line is not None:
                out.append(line)
        return out

    async def _space_changes(self, user: User, now: datetime) -> list[NotifChange]:
        out: list[NotifChange] = []
        for space_id, pref in await self._notifs.list_space_notif_prefs(user.user_id):
            space = await self._spaces.get(space_id)
            member: SpaceMember | None = (
                await self._spaces.get_member(space_id, user.user_id) if space else None
            )
            if space is None or member is None:
                continue
            default = default_space_notif_level(space.space_type, member.role).value
            choice = space_choice_json(pref, default, now)
            if choice["level"] == default and choice["muted_until"] is None:
                continue
            out.append(
                NotifChange(
                    target="space",
                    id=space_id,
                    level=choice["level"],
                    default_level=default,
                    place=str(space.space_type),
                    name=space.name,
                    muted_until=choice["muted_until"],
                    then_level=choice["then_level"],
                )
            )
        return out

    def _chat_default(self, conv) -> str:
        if conv.system_scope is None:
            return "all"
        if self._system_chats is not None:
            try:
                return self._system_chats.default_notif_level(conv)
            except Exception:
                log.debug("no system chat default", exc_info=True)
        return "mentions" if conv.system_scope is SystemChatScope.SPACE else "all"

    async def _chat_changes(self, user: User, now: datetime) -> list[NotifChange]:
        out: list[NotifChange] = []
        for conv, muted_until, level in await self._convos.list_notif_seats(
            user.username
        ):
            default = self._chat_default(conv)
            muted = mute_active(muted_until, now=now)
            if not muted and level == default:
                continue
            space_name = None
            if conv.system_scope is SystemChatScope.SPACE:
                space = await self._spaces.get(conv.space_id) if conv.space_id else None
                if space is None:
                    continue
                space_name = space.name
            if conv.system_scope is SystemChatScope.HOUSEHOLD:
                place = "household"
            elif conv.system_scope is SystemChatScope.SPACE:
                place = "space"
            elif conv.type is ConversationType.DM:
                place = "dm"
            else:
                place = "group"
            out.append(
                NotifChange(
                    target="chat",
                    id=conv.id,
                    level="muted" if muted else level,
                    default_level=default,
                    place=place,
                    name=space_name or await self._chat_name(conv, user.username),
                    muted_until=muted_until if muted else None,
                    then_level=level if muted else None,
                    space_id=conv.space_id,
                    space_name=space_name,
                )
            )
        return out

    async def _chat_name(self, conv, username: str) -> str | None:
        """A named group's name, a 1:1's other person; ``None`` otherwise
        (the household chat — the client names it)."""
        if conv.name:
            return conv.name
        if conv.system_scope is not None or conv.type is not ConversationType.DM:
            return None
        for m in await self._convos.list_members(conv.id):
            if m.username != username:
                other = await self._users.get(m.username)
                if other is not None:
                    return other.display_name
        for rm in await self._convos.list_remote_members(conv.id):
            if rm.display_name:
                return rm.display_name
            remote = await self._users.get_remote_by_member(
                rm.instance_id, rm.remote_username
            )
            if remote is not None:
                return remote.display_name
        return None

    async def _post_line(self, user: User, key: str) -> NotifChange | None:
        """A muted post's line — ``None`` when the post is gone or sits in a
        space *user* is not in (never a word of it then)."""
        kind, _, post_id = key.partition(":")
        space_id = space_name = None
        if kind == "post":
            post = await self._posts.get(post_id)
        else:
            found = await self._space_posts.get(post_id)
            if found is None:
                return None
            space_id, post = found
            space = await self._spaces.get(space_id)
            if (
                space is None
                or await self._spaces.get_member(space_id, user.user_id) is None
            ):
                return None
            space_name = space.name
        if post is None or getattr(post, "deleted", False):
            return None
        snippet = " ".join((post.content or "").split())[:SNIPPET_CHARS] or None
        return NotifChange(
            target="post",
            id=key,
            level="muted",
            default_level="all",
            place=kind,
            space_id=space_id,
            space_name=space_name,
            snippet=snippet,
        )

    async def get_user(self, username: str) -> User:
        """The local user *username* (:class:`KeyError` when unknown)."""
        user = await self._users.get(username)
        if user is None:
            raise KeyError(username)
        return user


__all__ = [
    "SNIPPET_CHARS",
    "NotificationSettingsService",
    "space_choice_json",
]

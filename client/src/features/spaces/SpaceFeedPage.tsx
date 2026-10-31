import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { loadSpaceMembers, setSpaceHereAllowed } from '@/store/spaceMembers'
import { useTitle } from '@/store/pageTitle'
import { t } from '@/i18n/i18n'
import type { FeedPost } from '@/types'
import { LoadErrorState } from '@/components/LoadErrorState'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { JoinRequestList } from '@/components/JoinRequestList'
import { ModerationQueue } from '@/components/ModerationQueue'
import { SpaceReports } from '@/components/SpaceReports'
import { SpaceLocationCard } from '@/components/SpaceLocationCard'
import { SpaceMemberList } from '@/components/SpaceMemberList'
import GalleryPage from '@/features/gallery/GalleryPage'
import { PostCard } from '@/components/PostCard'
import { PostPermalink } from '@/components/PostPermalink'
import { Composer, type ComposerExtras } from '@/components/Composer'
import { contentWrite } from '@/utils/contentWrite'
import { PendingReviewStrip } from '@/components/PendingReviewStrip'
import { useModerationMine } from '@/store/moderationMine'
import { markSeen } from '@/store/notifications'
import { itemPreview } from './moderationItems'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { SpaceSectionNav, type SpaceMemberSummary } from '@/components/SpaceSectionNav'
import { SpaceTasksTab } from './SpaceTasksTab'
import { SpacePagesTab } from './SpacePagesTab'
import { SpaceCalendarSection } from './SpaceCalendarSection'
import { SpaceTimetableTab } from './SpaceTimetableTab'
import {
  gamesSectionShown, legacySpaceSection, parseSpaceSection, redirectedSection, sectionAllowed,
  spaceSectionPath, visibleSpaceSections,
} from './spaceTabs'
import { joinableCount } from './games/spaceGames'
import { SpaceGamesSection, useGameApps } from './games/SpaceGamesSection'
import { SpaceOpenGamesStrip } from './games/SpaceOpenGamesStrip'
import { useSpaceGames } from './games/useSpaceGames'
import {
  canContribute,
  canModerate as roleCanModerate,
  hasSettingsAuthority,
  isFollower,
  isWriterRole,
  parseSpaceRole,
  type SpaceRole,
} from './spaceRoles'
import { AccessNote } from './AccessNote'
import { accessLevel, accessNote, blockedByAdminOnly, type AccessFeature } from './spaceAccess'
import { SpaceBazaarTab } from './SpaceBazaarTab'
import StickyBoardPage from '@/features/stickies/StickyBoardPage'
import { useSpaceTheme } from '@/hooks/useSpaceTheme'
import { useSpaceConfigWs } from '@/hooks/useSpaceConfigWs'
import { useSpaceLinks } from './useSpaceLinks'
import { useSpaceCompat } from '@/components/SpaceVersionNotice'
import { SpaceHero, type HeroMember } from '@/components/SpaceHero'
import { SpaceNeedsYou } from './SpaceNeedsYou'
import { SpaceSetupCard } from './SpaceSetupCard'
import { SpaceInviteFirst } from './SpaceInviteFirst'
import { isSetupPending } from './spaceSetup'
import { useSpaceNotifLevel } from './SpaceNotifLevels'
import {
  awaitsMyVote, setSpaceAttention, spaceAttention, useSpaceAttentionFrames, type WireAttention,
} from '@/store/spaceAttention'
import { confirmDialog } from '@/components/confirm'
import { SpaceChatView, useSpaceChatSummary } from './SpaceChatView'
import { patchSpaceChat, spaceChatOf, spaceChatOpenTo } from '@/store/spaceChat'
import { isMuteActive } from '@/utils/mute'
import { resolveAvatar } from '@/utils/avatar'
import { emptyFeedBodyKey } from './spaceFeedEmpty'
import { personLabel } from '@/utils/names'

interface SpaceDetail {
  id: string
  name: string
  /** Members, never followers — the server's one count. */
  member_count?: number
  /** §23.42 — gates the ``@here`` entry in the mention picker. */
  allow_here_mention?: boolean
  emoji: string | null
  description: string | null
  about_markdown: string | null
  cover_url: string | null
  cover_hash: string | null
  icon_url: string | null
  icon_hash: string | null
  features?: {
    pages?: boolean
    calendar?: boolean
    /** A shared weekly timetable (a class schedule) in the Calendar
     *  tab. Opt-in: absent → off. */
    timetable?: boolean
    todo?: boolean
    stickies?: boolean
    gallery?: boolean
    bazaar?: boolean
    location?: boolean
    /** The space chat (Feed tab → Feed | Chat). Absent → on. */
    chat?: boolean
    /** §23.49 — post types members may compose here; gates the
     *  composer's type picker. Absent → all types offered. */
    allowed_post_types?: string[]
    /** "Games allowed here" (group play, v_77). */
    allowed_apps?: string[]
  }
  /** §D1b — the originating instance. When it differs from
   *  ``instanceConfig.value.instance_id``, this is a stub of a
   *  remote-hosted space and local admin gestures are suppressed. */
  owner_instance_id?: string
  /** Read-only archive state — hide the composer + show a banner. */
  archived?: boolean
  /** Why archived. ``null`` = a reversible admin archive; ``'dissolved'``
   *  = the owner host dissolved the space; ``'removed'`` = this household
   *  was removed. The latter two are remote-terminated read-only archives
   *  that can't be revived — the banner copy adapts to this. */
  archived_reason?: 'dissolved' | 'removed' | null
  /** Another household has a member here. */
  has_remote_households?: boolean
  /** The viewer's own role here. A follower is not in ``/members`` (it
   *  lists the members only), so this — not the roster — says who the
   *  viewer is. */
  my_role?: string
  /** The tier: private / household / public / global. */
  space_type?: string
  /** Discovery category (``'general'`` / absent = none). */
  category?: string | null
  /** "Needs you" (owners / admins / moderators); ``null`` otherwise. */
  attention?: WireAttention | null
}

/** A ``/members`` row, as far as this page reads it. */
interface MemberRow {
  user_id: string
  role?: string
  display_name?: string | null
  space_display_name?: string | null
  personal_alias?: string | null
  picture_url?: string | null
}

/** Reason-aware copy for the read-only archive banner + empty-state.
 *  Returns ``null`` when the space isn't archived. Pure (no DOM, no
 *  signals) so the wording can be unit-tested without rendering the page. */
export function archivedCopy(
  archived: boolean | undefined,
  reason: 'dissolved' | 'removed' | null | undefined,
): { title: string; body: string; empty: string } | null {
  if (!archived) return null
  if (reason === 'dissolved') {
    return {
      title: t('space.archived.dissolved_title'),
      body: t('space.archived.dissolved_body'),
      empty: t('space.archived.dissolved_empty'),
    }
  }
  if (reason === 'removed') {
    return {
      title: t('space.archived.removed_title'),
      body: t('space.archived.removed_body'),
      empty: t('space.archived.removed_empty'),
    }
  }
  // Normal, reversible admin archive.
  return {
    title: t('space.archived.title'),
    body: t('space.archived.body'),
    empty: t('space.archived.empty'),
  }
}

const posts = signal<FeedPost[]>([])
const loading = signal(true)
const viewerRole = signal<SpaceRole | undefined>(undefined)
const spaceDetail = signal<SpaceDetail | null>(null)
/** The space detail answered (or failed) — until then ``viewerRole`` is
 *  unknown, not "no role". */
const roleKnown = signal(false)
const memberCount = signal<number | null>(null)
/** Members and admins (owner + admin) — the Members row of More. */
const memberSummary = signal<SpaceMemberSummary | null>(null)
/** The hero's facepile: the first four members. */
const memberPreview = signal<HeroMember[]>([])
/** The space detail could not be read (and nothing is on screen). */
const detailFailed = signal(false)
/** The feed could not be read for another reason than "no seat". */
const feedFailed = signal(false)

/** The space header + the viewer's role: ``GET /api/spaces/{id}`` (name,
 *  emoji, cover, features → tabs, archive state, ``my_role`` → admin-only
 *  and follower UI) and the member list (member count — a follower's
 *  lists the members only). Runs on open and again on every
 *  ``space.config.changed`` frame — a rename, feature toggle or admin
 *  grant elsewhere shows up without a reload. A failed refetch keeps
 *  what's on screen. */
async function loadSpaceHeader(spaceId: string) {
  await Promise.all([
    api.get(`/api/spaces/${spaceId}`).then((d) => {
      const detail = d as SpaceDetail
      spaceDetail.value = detail
      detailFailed.value = false
      setSpaceHereAllowed(spaceId, detail.allow_here_mention === true)
      viewerRole.value = parseSpaceRole(detail.my_role)
      setSpaceAttention(spaceId, detail.attention)
      roleKnown.value = true
    }).catch(() => {
      roleKnown.value = true /* keep the last role */
      // Nothing on screen yet: say so with Retry. A failed refetch keeps
      // what's there.
      if (!spaceDetail.value) detailFailed.value = true
    }),
    api.get(`/api/spaces/${spaceId}/members`)
      .then((rows: MemberRow[]) => {
        // Members only: followers aren't members (a follower's roster
        // lists the members alone anyway).
        const members = rows.filter((m) => m.role !== 'subscriber')
        memberCount.value = members.length
        memberSummary.value = {
          count: members.length,
          admins: members.filter((m) => m.role === 'owner' || m.role === 'admin').length,
        }
        memberPreview.value = members.slice(0, 4).map((m) => ({
          user_id: m.user_id,
          name: personLabel(m.space_display_name, m.personal_alias, m.display_name),
          picture_url: m.picture_url ?? null,
        }))
      })
      .catch(() => { /* keep the last count */ }),
  ])
}

async function loadSpaceFeed(spaceId: string) {
  const rows = await api.get(`/api/spaces/${spaceId}/feed`) as FeedPost[]
  posts.value = rows
}

export default function SpaceFeedPage() {
  const { params, query } = useRoute()
  const spaceId = params.id
  // Every section has its own path: ``/spaces/:id`` is the feed,
  // ``/spaces/:id/:section`` the rest — Refresh and Back keep it.
  const parsedSection = parseSpaceSection(params.section)
  const section = parsedSection ?? 'feed'
  // ``?post={id}`` (a notification's link): the Feed shows that post
  // alone with its comments (``?comment={cid}`` picked out).
  const focusPost = section === 'feed' ? (query?.post ?? null) : null
  const feedList = section === 'feed' && !focusPost
  const loc = useLocation() as ReturnType<typeof useLocation> | undefined

  // Apply the space's custom theme (§23 customization). The hook
  // fetches /api/spaces/{id}/theme, sets CSS vars, and cleans up on
  // unmount so household colours return as the user leaves.
  useSpaceTheme(spaceId)
  // Live header / tabs / role on a config change elsewhere; a dissolve
  // leaves for the spaces list with a toast.
  useSpaceConfigWs(spaceId, () => { void loadSpaceHeader(spaceId) })
  // The viewer's own items waiting for review (§4.3) — the
  // "Pending review (n)" strip on each tab.
  useModerationMine(spaceId)
  // Surface the space's name in the global TopBar (matches the
  // household feed pattern). Falls back to "Space" while the detail
  // request is in flight.
  const detail = spaceDetail.value
  useTitle(
    detail
      ? (detail.emoji ? `${detail.emoji} ${detail.name}` : detail.name)
      : t('spaces.space_title'),
  )

  useEffect(() => {
    loading.value = true
    viewerRole.value = undefined
    roleKnown.value = false
    spaceDetail.value = null
    memberCount.value = null
    memberSummary.value = null
    memberPreview.value = []
    detailFailed.value = false
    feedFailed.value = false
    spaceAttention.value = null
    void loadHouseholdUsers()
    void loadSpaceMembers(spaceId)
    loadSpaceFeed(spaceId)
      .catch((err: unknown) => {
        posts.value = []
        // 403: the viewer has no seat here (the feed is members-only). The
        // landing page shows a public space's join options, or says the
        // space isn't public — never an empty feed with a composer.
        if ((err as { status?: unknown })?.status === 403) {
          loc?.route(`/spaces/${spaceId}/about`, true)
          return
        }
        // Anything else (offline, a server error): an error with Retry,
        // not the "no posts yet" empty state.
        feedFailed.value = true
      })
      .finally(() => { loading.value = false })
    void loadSpaceHeader(spaceId)

    const off4 = ws.on('space.post.created', (e) => {
      const d = e.data as { space_id?: string | null }
      if (d.space_id === spaceId) void loadSpaceFeed(spaceId)
    })
    // Live comment counts — the space analogue of store/feed.ts. The
    // frame fans out to space members as
    // {type, post_id, space_id, comment}; bump the matching post's
    // comment_count in place so the badge updates without a refetch.
    const offComment = ws.on('comment.added', (e) => {
      const d = e.data as { space_id?: string | null; post_id?: string }
      if (d.space_id === spaceId) {
        posts.value = posts.value.map(p =>
          p.id === d.post_id
            ? { ...p, comment_count: (p.comment_count ?? 0) + 1 }
            : p,
        )
      }
    })
    // space.post.moderated is the only space-post mutation the backend
    // broadcasts to space members (realtime_service._on_space_post_moderated);
    // post.edited / post.deleted / post.reaction_changed go to the
    // household only and carry no space_id, so they're not wired here.
    const offModerated = ws.on('space.post.moderated', (e) => {
      const d = e.data as { space_id?: string | null }
      if (d.space_id === spaceId) void loadSpaceFeed(spaceId)
    })
    return () => {
      off4()
      offComment()
      offModerated()
    }
    // ``loc`` (the no-seat redirect) changes with every URL change — every
    // section switch included — so as a dependency it would refetch the
    // whole space on each of them.
  }, [spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Links from before sections had paths (a stored bell entry, a
  // bookmark): ``?tab=tasks``, ``?view=chat``, ``#join-requests`` → the
  // section's own path, replacing the entry so Back skips the old form.
  // A section this page doesn't know → the feed.
  useEffect(() => {
    if (!loc) return
    if (parsedSection === null) { loc.route(spaceSectionPath(spaceId, 'feed'), true); return }
    if (params.section) return
    const legacy = legacySpaceSection(query, typeof location === 'undefined' ? '' : location.hash)
    if (legacy) loc.route(spaceSectionPath(spaceId, legacy), true)
  }, [spaceId, params.section, query?.tab, query?.view]) // eslint-disable-line react-hooks/exhaustive-deps

  const features = spaceDetail.value?.features
  // The space chat is a section for a member (never a follower) while
  // the space keeps chat on and the server agrees.
  const chatEligible = features?.chat !== false && isWriterRole(viewerRole.value)
  // A space of one has nobody to chat with: Chat appears when the second
  // member joins (an unknown count doesn't hide it, so the section never
  // falls back to the feed while the roster loads).
  // The server's member count (the one every surface shows); the roster's
  // own count only while an older server sends none.
  const membersShown = spaceDetail.value?.member_count ?? memberCount.value
  const soloSpace = membersShown === 1
  const chatOn = !soloSpace && spaceChatOpenTo(features, viewerRole.value, spaceChatOf(spaceId))
  const follower = isFollower(viewerRole.value)
  const canModerateHere = roleCanModerate(viewerRole.value)
  // Games (group play): the people who choose them (owner / admins /
  // moderators, D9) see the section even before any game is allowed;
  // never a follower, never a global space.
  const globalSpace = spaceDetail.value?.space_type === 'global'
  const gamesViewer = {
    choosesGames: canModerateHere,
    global: globalSpace,
    writer: roleKnown.value && isWriterRole(viewerRole.value),
  }
  const gamesOn = gamesSectionShown(features, { chat: chatOn, follower, ...gamesViewer })
  const games = useSpaceGames(spaceId, gamesOn, (features?.allowed_apps ?? []).join(','))
  useGameApps(gamesOn)
  // A section the viewer can't have here (feature off, a follower on the
  // Map, Moderation without content authority) falls back to the feed —
  // once the space and the viewer's role are known, and again if an
  // admin turns the section off while it's open. An old Calendar link in
  // a space that keeps only a timetable opens the Timetable section.
  useEffect(() => {
    if (!loc || !spaceDetail.value || !roleKnown.value) return
    const viewer = { chat: chatOn, follower, canModerate: canModerateHere, ...gamesViewer }
    if (!sectionAllowed(section, features, viewer)) {
      const moved = redirectedSection(section, features)
      const target = moved && sectionAllowed(moved, features, viewer) ? moved : 'feed'
      loc.route(spaceSectionPath(spaceId, target), true)
    }
  }, [spaceId, section, features, roleKnown.value, chatOn, follower, canModerateHere, globalSpace, gamesViewer.writer]) // eslint-disable-line react-hooks/exhaustive-deps

  const chatOpen = section === 'chat'
  useSpaceChatSummary(spaceId, chatEligible, chatOpen)

  // Viewing the Feed reads it: the Spaces list's "N new" starts again from
  // now — on opening the section, and again when posts arrive while it's
  // on screen. Any seat (a follower reads too); a hint, so a failure is
  // silent.
  const seated = roleKnown.value && viewerRole.value !== undefined
  const postCount = posts.value.length
  useEffect(() => {
    if (section !== 'feed' || !seated) return
    void (async () => {
      try { await api.post(`/api/spaces/${spaceId}/feed/read`, {}) } catch { /* a hint */ }
    })()
  }, [spaceId, section, seated, postCount])

  // The feed itself is on screen (not one post picked out of it): the
  // notifications that open the space — "joined", "you're in", a review
  // decided — read as read. Its posts' notifications wait until that
  // post is opened.
  useEffect(() => {
    if (!feedList || !seated) return
    markSeen(`/spaces/${spaceId}`)
  }, [spaceId, feedList, seated])

  // "Needs you" comes with the space detail (``attention``): re-read it
  // on the frames that change it. Leaving Moderation re-reads it too (a
  // report dismissed there sends no frame).
  const settingsAuthority = hasSettingsAuthority(viewerRole.value)
  useSpaceAttentionFrames(spaceId, canModerateHere, () => { void loadSpaceHeader(spaceId) })
  const prevSection = useRef(section)
  useEffect(() => {
    if (prevSection.current === 'moderation' && section !== 'moderation' && canModerateHere) {
      void loadSpaceHeader(spaceId)
    }
    prevSection.current = section
  }, [section]) // eslint-disable-line react-hooks/exhaustive-deps
  // Member households behind on the protocol — a Needs-you row for admins.
  const compat = useSpaceCompat(spaceId, roleKnown.value && settingsAuthority)
  const quickLinks = useSpaceLinks(spaceId)

  const [notifLevel, setNotifLevel] = useSpaceNotifLevel(
    spaceId, roleKnown.value && viewerRole.value !== undefined,
  )

  const handleSubmit = async (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: ComposerExtras,
  ) => {
    const body: Record<string, unknown> = {
      type, content, media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    if (extras?.noLinkPreview) body.no_link_preview = true
    // Poll / schedule ride the create request (atomic with the post, so
    // a post held for review carries them).
    if (extras?.poll) {
      body.poll = {
        question: extras.poll.question,
        options: extras.poll.options,
        allow_multiple: extras.poll.allow_multiple,
        closes_at: extras.poll.closes_at,
      }
    }
    if (extras?.schedule) {
      body.schedule = { title: extras.schedule.title, slots: extras.schedule.slots }
    }
    const res = await contentWrite<{ id: string }>(
      api.post(`/api/spaces/${spaceId}/posts`, body),
      { spaceId },
    )
    // Held for review (202): nothing is in the feed yet.
    if (res.queued) return undefined
    showToast(t('feed.post_shared'), 'success')
    await loadSpaceFeed(spaceId)
    return res.data?.id
  }

  const handleReact = async (postId: string, emoji: string) => {
    await api.post(
      `/api/spaces/${spaceId}/posts/${postId}/reactions`, { emoji },
    )
    void loadSpaceFeed(spaceId)
  }

  const handleDelete = async (postId: string) => {
    if (!await confirmDialog(t('space.feed.delete_confirm'), { destructive: true })) return
    await api.delete(`/api/spaces/${spaceId}/posts/${postId}`)
    showToast(t('space.feed.deleted'), 'info')
    void loadSpaceFeed(spaceId)
  }

  /** Inline edit of a post's text: ``true`` closes the editor (saved,
   *  or held for review), ``false`` keeps it (toasted). A 403 means the
   *  viewer's seat or the space's access level changed — refetch both. */
  const handleEdit = async (postId: string, content: string): Promise<boolean> => {
    try {
      const res = await contentWrite<{ id: string; content: string; edited_at: string | null }>(
        api.patch(`/api/spaces/${spaceId}/posts/${postId}`, { content }),
        { spaceId },
      )
      if (res.queued) return true
      posts.value = posts.value.map(p => p.id === postId
        ? { ...p, content: res.data.content, edited_at: res.data.edited_at }
        : p)
      showToast(t('post.edit.saved'), 'success')
      // The link preview may have been dropped by the edit.
      void loadSpaceFeed(spaceId)
      return true
    } catch (err: unknown) {
      showToast(t('post.edit.failed', { error: String((err as Error)?.message ?? err) }), 'error')
      if ((err as { status?: unknown })?.status === 403) void loadSpaceHeader(spaceId)
      return false
    }
  }

  if (loading.value) return <Spinner />

  // Neither the space nor its feed could be read: an error with Retry —
  // never an empty feed (which would say "no posts yet").
  const retry = () => {
    loading.value = true
    feedFailed.value = false
    detailFailed.value = false
    void loadSpaceHeader(spaceId)
    loadSpaceFeed(spaceId)
      .catch(() => { feedFailed.value = true })
      .finally(() => { loading.value = false })
  }
  if (detailFailed.value && !spaceDetail.value) {
    return (
      <div class="sh-space-feed sh-space-scope">
        <LoadErrorState headingLevel={2} message={t('space.load_failed')} onRetry={retry} />
      </div>
    )
  }

  // Leave the space (a member who isn't the owner): confirm, then drop
  // the seat and go to the spaces list.
  const leaveSpace = async () => {
    const name = spaceDetail.value?.name ?? t('spaces.space_title')
    const ok = await confirmDialog(t('space.leave.confirm', { name }), {
      destructive: true, confirmLabel: t('space.leave.confirm_action'),
    })
    const me = currentUser.value?.user_id
    if (!ok || !me) return
    try {
      await api.delete(`/api/spaces/${spaceId}/members/${me}`)
      showToast(t('space.leave.left', { name }), 'info')
      loc?.route('/spaces', true)
    } catch (err) {
      showToast(t('space.leave.failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  // Content authority (owner / admin / moderator, v_41): acting on other
  // people's posts, and the moderation queue — on the host and on every
  // household holding a moderator / admin seat alike, since items are
  // sent to each of them (federated moderation, v_43). No settings power
  // rides along with it; the server re-checks every action.
  const canModerate = roleCanModerate(viewerRole.value)
  const canModerateQueue = canModerate
  const s = spaceDetail.value

  // Per-space feature toggles hide their section when off (``spaceTabs``).
  const f = s?.features
  const sections = visibleSpaceSections(f, { chat: chatOn, follower, ...gamesViewer })
  const attention = spaceAttention.value
  const chatSummary = spaceChatOf(spaceId)
  // A muted chat keeps its count on the server but raises no badge — the
  // household Chat tab's rule.
  const chatUnread = chatOpen || isMuteActive(chatSummary?.muted_until) ? 0 : (chatSummary?.unread ?? 0)
  // Space timetables: owners / admins edit, everyone else reads. The
  // server's role check is the authority (a remote-hosted space's
  // admin edits too — the host verifies it); an archive is read-only.
  const canEditTimetable = !s?.archived && hasSettingsAuthority(viewerRole.value)
  // §4.3 per-feature access levels: may the viewer create / edit there?
  // ``adminOnly`` is the case worth a note — a member or moderator in an
  // ADMIN_ONLY feature (a subscriber reads everywhere anyway). The server
  // refuses the writes regardless (403 ACCESS_ADMIN_ONLY).
  const canWrite = (feature: AccessFeature) =>
    canContribute(accessLevel(f, feature), viewerRole.value)
  const adminOnly = (feature: AccessFeature) =>
    roleKnown.value && blockedByAdminOnly(accessLevel(f, feature), viewerRole.value)
  // Why the sticky board is read-only (no add / edit / move, and each
  // note by someone else carries its own Report): archived, kept to the
  // admins (§4.3), or a viewer who can't write (a follower). ``null`` →
  // writable.
  const stickiesReadOnly = (): string | null => {
    if (s?.archived) return t('stickies.read_only_archived')
    if (adminOnly('stickies')) return accessNote('stickies')
    if (roleKnown.value && !isWriterRole(viewerRole.value)) return t('stickies.read_only_viewer')
    return null
  }
  // Edit / Delete on a post: the author, or content authority (owner /
  // admin / moderator) on somebody else's — once the member list said
  // who the viewer is, never for a read-only subscriber, and not where
  // the space keeps posts to its admins (§4.3).
  const canActOnPost = (author: string) =>
    roleKnown.value && isWriterRole(viewerRole.value)
    && (author === currentUser.value?.user_id || canModerate)
    && !adminOnly('posts')

  return (
    <div class={chatOpen ? 'sh-space-feed sh-space-scope sh-space-feed--chat' : 'sh-space-feed sh-space-scope'}>
      <SpaceSectionNav
        spaceId={spaceId}
        spaceName={s?.name ?? t('spaces.space_title')}
        sections={sections}
        active={section}
        chatUnread={chatUnread}
        gamesOpen={joinableCount(games.games?.tables ?? [])}
        members={memberSummary.value && membersShown != null
          ? { ...memberSummary.value, count: membersShown }
          : memberSummary.value}
        needsYou={canModerateQueue && attention?.spaceId === spaceId
          ? {
              review: attention.review, reports: attention.reports, joinRequests: attention.joinRequests,
              proposals: attention.proposals
                .filter((p) => awaitsMyVote(p, settingsAuthority, viewerRole.value === 'owner')).length,
            }
          : null}
        notifLevel={notifLevel}
        onNotifLevel={setNotifLevel}
        chatNotif={chatOn && chatSummary?.enabled && chatSummary.conversation_id ? {
          convId: chatSummary.conversation_id,
          mutedUntil: chatSummary.muted_until ?? null,
          level: chatSummary.notif_level ?? 'mentions',
          onMute: (until) => patchSpaceChat({ muted_until: until }),
          onLevel: (level) => patchSpaceChat({ notif_level: level }),
        } : null}
        // Everyone with a seat opens the space's settings — the page
        // shows each their own: admins the whole page (a remote admin what
        // forwards to the host), members their notifications, "You in
        // this space" and Leave, followers notifications and Unfollow.
        settingsHref={viewerRole.value ? addBase(`/spaces/${spaceId}/settings`) : null}
        canEditSections={hasSettingsAuthority(viewerRole.value)}
        // Members leave from More; the owner hands ownership on first;
        // a follower unfollows from the feed instead.
        leave={viewerRole.value === 'owner' ? 'owner'
          : isWriterRole(viewerRole.value) ? 'member' : null}
        onLeave={() => { void leaveSpace() }}
      />
      {/* The hero and "Needs you" open the Feed only: the other sections
       *  are tools, and the top bar already names the space. */}
      {s && feedList && (
        <SpaceHero
          name={s.name}
          emoji={s.emoji ?? null}
          coverUrl={s.cover_url ?? null}
          iconUrl={s.icon_url ?? null}
          about={s.about_markdown ?? null}
          memberCount={membersShown}
          // A member's space picture, else their household one.
          members={memberPreview.value.map((m) => ({
            ...m, picture_url: resolveAvatar(spaceId, m.user_id, m.picture_url),
          }))}
          membersHref={addBase(spaceSectionPath(spaceId, 'members'))}
          tier={s.space_type ?? null}
          links={quickLinks}
        />
      )}
      {s && feedList && canModerateQueue && (
        <SpaceNeedsYou
          spaceId={spaceId}
          attention={attention?.spaceId === spaceId ? attention : null}
          compat={compat}
          canVote={settingsAuthority}
          isOwner={viewerRole.value === 'owner'}
          onChanged={() => { void loadSpaceHeader(spaceId) }}
        />
      )}
      {/* A new space asks its owner once what it's for (owner decision 3). */}
      {s && feedList && viewerRole.value === 'owner' && !s.archived
        && isSetupPending(spaceId) && (
        <SpaceSetupCard
          spaceId={spaceId}
          name={s.name}
          category={s.category ?? null}
          features={s.features as Record<string, unknown> | undefined}
          onDone={() => { void loadSpaceHeader(spaceId) }}
        />
      )}
      {s && feedList && settingsAuthority && !s.archived && membersShown === 1 && (
        <SpaceInviteFirst spaceId={spaceId} role={viewerRole.value} />
      )}

      {/* "Open games" on top of the feed — only while a game waits for
       *  people (group play D5); nothing at all otherwise. */}
      {s && feedList && gamesOn && (
        <SpaceOpenGamesStrip spaceId={spaceId} games={games.games}
                             onChanged={() => { void games.reload() }} />
      )}

      {chatOpen && (
        <SpaceChatView spaceId={spaceId} spaceName={s?.name ?? t('spaces.space_title')}
                       canModerate={canModerate} archived={s?.archived === true} />
      )}

      {/* A post a link pointed at (``?post=``, a notification): that post
       *  alone with its comments, a way back to the whole feed. */}
      {focusPost && (
        <PostPermalink
          postId={focusPost}
          spaceId={spaceId}
          commentId={query?.comment ?? null}
          backPath={spaceSectionPath(spaceId, 'feed')}
        />
      )}

      {feedList && (
        <div class="sh-feed sh-space-feed-content">
          {viewerRole.value === 'subscriber' ? (
            <div class="sh-subscriber-banner" role="status">
              <span class="sh-subscriber-banner__icon" aria-hidden="true">🔔</span>
              <div class="sh-subscriber-banner__body">
                <strong>{t('space.subscriber.title')}</strong>
                <p class="sh-muted">
                  {(() => {
                    // Subscriber-engagement opt-ins (§23.49) — admins can
                    // open one or both paths.  Banner copy reflects what
                    // the viewer can actually do without contacting an
                    // admin.
                    const f = s?.features as {
                      allow_subscriber_react?: boolean
                      allow_subscriber_comment?: boolean
                    } | undefined
                    const canReact   = !!f?.allow_subscriber_react
                    const canComment = !!f?.allow_subscriber_comment
                    if (canReact && canComment) {
                      return t('space.subscriber.react_comment')
                    }
                    if (canReact) {
                      return t('space.subscriber.react')
                    }
                    if (canComment) {
                      return t('space.subscriber.comment')
                    }
                    return t('space.subscriber.read_only')
                  })()}
                </p>
              </div>
              <button
                type="button"
                class="sh-subscribe-btn sh-subscribe-btn--on"
                aria-label={t('space.subscriber.unsubscribe_aria')}
                title={t('space.subscriber.unsubscribe_title')}
                onClick={async () => {
                  try {
                    await api.delete(`/api/spaces/${spaceId}/subscribe`)
                    showToast(t('space.subscriber.unsubscribed'), 'info')
                    // A follower no longer reads this feed: leave it for
                    // the spaces list in-app (no SPA reload), replacing
                    // the entry so Back doesn't return to it.
                    loc?.route('/spaces', true)
                  } catch (exc) {
                    showToast((exc as Error).message, 'error')
                  }
                }}
              >
                🔕 {t('space.subscriber.unsubscribe')}
              </button>
            </div>
          ) : spaceDetail.value?.archived ? (
            <div class="sh-subscriber-banner" role="status">
              <span class="sh-subscriber-banner__icon" aria-hidden="true">🗄️</span>
              <div class="sh-subscriber-banner__body">
                <strong>
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.title}
                </strong>
                <p class="sh-muted">
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.body}
                </p>
              </div>
            </div>
          ) : adminOnly('posts') ? (
            <AccessNote feature="posts" />
          ) : (
            <Composer onSubmit={handleSubmit} context="Space" tier={s?.space_type ?? null} spaceId={spaceId}
              allowedTypes={spaceDetail.value?.features?.allowed_post_types}
              bazaarEnabled={spaceDetail.value?.features?.bazaar ?? true} />
          )}
          <PendingReviewStrip spaceId={spaceId} feature="posts" />
          {feedFailed.value && (
            <LoadErrorState headingLevel={2} message={t('space.feed.load_failed')} onRetry={retry} />
          )}
          {!feedFailed.value && posts.value.length === 0 && (
            <div class="sh-empty-state">
              <div aria-hidden="true">{spaceDetail.value?.archived ? '🗄️' : '💬'}</div>
              <h2>{t('spaces.no_posts')}</h2>
              {spaceDetail.value?.archived ? (
                <p class="sh-muted">
                  {archivedCopy(
                    spaceDetail.value?.archived,
                    spaceDetail.value?.archived_reason,
                  )?.empty}
                </p>
              ) : adminOnly('posts') ? null : (
                <>
                  <p>{t(emptyFeedBodyKey(
                    spaceDetail.value?.space_type,
                    membersShown,
                    spaceDetail.value?.has_remote_households,
                  ))}</p>
                  <p class="sh-muted">{t('space.feed.empty_hint')}</p>
                </>
              )}
            </div>
          )}
          {posts.value.map(post => (
            <div key={post.id} class="sh-feed-item">
              <PostCard
                post={post}
                onReact={(emoji) => handleReact(post.id, emoji)}
                onComment={() => openCommentOverlay(post, spaceId)}
                // The author, or content authority (owner / admin /
                // moderator) acting on somebody else's post — unless the
                // space keeps posts to its admins (§4.3).
                onDelete={canActOnPost(post.author)
                  ? () => handleDelete(post.id)
                  : undefined}
                // Same rule for editing the text, and never in an archive.
                onEdit={canActOnPost(post.author) && !s?.archived
                  ? (content) => handleEdit(post.id, content)
                  : undefined}
                spaceId={spaceId}
                surface="space"
              />
            </div>
          ))}
        </div>
      )}

      {section === 'members' && (
        <SpaceMemberList spaceId={spaceId} viewerRole={viewerRole.value}
                         joinRequests={attention?.spaceId === spaceId ? attention.joinRequests : 0} />
      )}

      {section === 'pages' && (
        <SpacePagesTab
          spaceId={spaceId}
          role={viewerRole.value}
          level={accessLevel(f, 'pages')}
          // Read-only until the member list answers (no flash of edit
          // controls a subscriber can't use).
          writable={roleKnown.value && canWrite('pages')}
          adminOnly={adminOnly('pages')}
          archived={!!s?.archived}
          hostInstanceId={s?.owner_instance_id ?? null}
        />
      )}

      {section === 'games' && sections.includes('games') && (
        <SpaceGamesSection spaceId={spaceId} state={games} />
      )}
      {section === 'calendar' && sections.includes('calendar') && (
        <SpaceCalendarSection
          spaceId={spaceId}
          canWrite={canWrite('calendar')}
          adminOnly={adminOnly('calendar')}
          archived={!!s?.archived}
        />
      )}

      {section === 'timetable' && sections.includes('timetable') && (
        <SpaceTimetableTab spaceId={spaceId} canEdit={canEditTimetable} />
      )}

      {section === 'tasks' && (
        <PendingReviewStrip spaceId={spaceId} feature="tasks" />
      )}
      {section === 'tasks' && (
        <SpaceTasksTab
          spaceId={spaceId}
          // Unknown until the member list answers: the tab waits rather
          // than flashing locked cards at a member.
          writable={roleKnown.value ? canWrite('tasks') : undefined}
          adminOnly={accessLevel(f, 'tasks') === 'admin_only'}
          archived={!!s?.archived}
        />
      )}

      {section === 'stickies' && (
        <PendingReviewStrip spaceId={spaceId} feature="stickies" />
      )}
      {section === 'stickies' && (
        <StickyBoardPage
          spaceId={spaceId}
          readOnly={stickiesReadOnly()}
        />
      )}

      {section === 'gallery' && (
        <GalleryPage
          spaceId={spaceId}
          readOnly={roleKnown.value && !isWriterRole(viewerRole.value)}
        />
      )}

      {section === 'bazaar' && (
        <PendingReviewStrip spaceId={spaceId} feature="posts"
                            filter={i => !!itemPreview(i).bazaar} />
      )}
      {section === 'bazaar' && (
        <SpaceBazaarTab spaceId={spaceId} canSell={!adminOnly('posts')}
                        follower={isFollower(viewerRole.value)} />
      )}

      {section === 'map' && sections.includes('map') && (
        // Passing ``currentUserId`` lights up the "Share my location"
        // chip + button at the top of the map. Without it the share
        // surface stays hidden (the card otherwise renders the map
        // read-only for spectators).
        <SpaceLocationCard
          spaceId={spaceId}
          currentUserId={currentUser.value?.user_id}
        />
      )}

      {section === 'moderation' && canModerateQueue && hasSettingsAuthority(viewerRole.value) && (
        <JoinRequestList spaceId={spaceId} />
      )}
      {section === 'moderation' && canModerateQueue && (
        // Approving creates the item as the approver: a moderator can't
        // where the feature is kept to admins (§4.3) — rejecting stays open.
        <ModerationQueue
          spaceId={spaceId}
          canApprove={(feature) => !adminOnly(feature as AccessFeature)}
        />
      )}
      {section === 'moderation' && canModerateQueue && (
        <SpaceReports spaceId={spaceId}
                      onOpenTab={(tab) => loc?.route(spaceSectionPath(spaceId, tab))} />
      )}
    </div>
  )
}

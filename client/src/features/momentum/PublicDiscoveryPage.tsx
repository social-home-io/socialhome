/**
 * Discover — the Momentum tab (``/momentum/discover``, decision 5): people
 * to follow, in two sections (design: ``docs/protocol/momentum_discovery.md``).
 *
 * * **Friends** (``DiscoverFriends``) — your own and your paired
 *   households' people with the household follow, which never leaves the
 *   household. Shown to everyone, a protected account included.
 * * **On a GFS** — people sharing publicly on one of this household's
 *   GFSes, with the GFS follow; the hint under its heading says the GFS
 *   learns who you follow. Each person shows once: the household's proxy
 *   leaves out you and the people of your own and paired households
 *   (``MomentPublicService.fetch_directory``) — they are in Friends — and
 *   refuses a GFS follow of them (decision 7, "one Follow"). Not for a
 *   protected account.
 *
 * Friends comes first, unless you have no friends yet and a GFS: then the
 * GFS list leads and Friends is one line under it. No friends and no GFS is
 * one card. Each section loads on its own, so Friends (local) never waits
 * for a GFS.
 *
 * One search box filters both sections here; the GFS never sees it. A GFS
 * that doesn't answer keeps the last list it gave in this session, labelled
 * with its age, plus an inline error with Try again — never "no one signed
 * up".
 */
import { useEffect } from 'preact/hooks'
import { computed, signal } from '@preact/signals'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Avatar } from '@/components/Avatar'
import { openPerson } from '@/components/PersonSheet'
import { isRestricted } from '@/components/ProtectedNotice'
import { Spinner } from '@/components/Spinner'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { relativeDocsTime } from '@/utils/relativeTime'
import { formatLocale, t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { isBlocked, loadBlocks } from '@/store/blocks'
import { loadFollows as loadHouseholdFollows } from '@/store/follows'
import {
  fetchGfsDirectory,
  follows,
  followUser,
  gfsConnections,
  gfsConnectionsFailed,
  loadFollows,
  loadGfsConnections,
  unfollowUser,
} from '@/store/momentPublic'
import { fold, type FriendsPayload, type Person } from '@/features/friends/friendsModel'
import type { MomentPublicDirectoryUser } from '@/types'
import { DiscoverFriends, discoverFriends } from './DiscoverFriends'
import { FollowToggle, toggleFollow } from './FollowToggle'

const selectedGfs = signal<string | null>(null)
const directory = signal<MomentPublicDirectoryUser[]>([])
const loading = signal(true)
const searchQuery = signal('')
const busyUser = signal<string | null>(null)

/** The last directory each GFS answered with, and when — kept for the
 *  session so an unreachable GFS still shows who was there, with its age. */
const directoryCache = new Map<string, { users: MomentPublicDirectoryUser[], at: number }>()
/** The GFS whose directory read just failed (unreachable ≠ "no one
 *  signed up"), else null. */
const directoryFailed = signal<string | null>(null)

/** ``GET /api/friends`` — ``null`` until it answers. */
const friends = signal<FriendsPayload | null>(null)
const friendsFailed = signal(false)

/** The GFS rows to list. The household's proxy already left out everyone
 *  it knows — you, your household and your paired households' people (by
 *  ``user_id`` and household, whatever the GFS claims) — so they stay in
 *  Friends with the household follow, even while Friends is loading or
 *  failed. Here: people you blocked go too, and the search filters. */
const strangers = computed<MomentPublicDirectoryUser[]>(() => {
  const me = currentUser.value?.user_id ?? null
  const q = fold(searchQuery.value.trim())
  return directory.value.filter((u) => {
    if ((me && u.user_id === me) || isBlocked(u.user_id)) return false
    return !q || [u.display_name, u.username, u.bio].some((s) => !!s && fold(s).includes(q))
  })
})

/** Bumped per read, so an answer meant for an earlier visit (another
 *  account) never lands after a newer read started. */
let friendsRead = 0

async function loadFriends(): Promise<void> {
  const read = ++friendsRead
  friendsFailed.value = false
  try {
    const r = await api.get<FriendsPayload>('/api/friends')
    if (!r?.instance || !Array.isArray(r.instance.members) || !Array.isArray(r.households)) {
      throw new Error('unexpected /api/friends payload')
    }
    if (read === friendsRead) friends.value = r
  } catch {
    if (read === friendsRead) friendsFailed.value = true
  }
}

async function loadDirectory(gfsId: string): Promise<void> {
  loading.value = true
  directory.value = directoryCache.get(gfsId)?.users ?? []
  try {
    const users = await fetchGfsDirectory(gfsId)
    directoryCache.set(gfsId, { users, at: Date.now() })
    if (selectedGfs.value === gfsId) {
      directory.value = users
      directoryFailed.value = null
    }
  } catch {
    if (selectedGfs.value === gfsId) directoryFailed.value = gfsId
  } finally {
    loading.value = false
  }
}

async function bootstrapGfs(): Promise<void> {
  loading.value = true
  directoryFailed.value = null
  // Joins the read Momentum just started, or reads afresh.
  const conns = await loadGfsConnections()
  if (gfsConnectionsFailed.value || !conns) {
    loading.value = false
    return
  }
  if (!conns.some(g => g.id === selectedGfs.value)) {
    selectedGfs.value = conns[0]?.id ?? null
  }
  void loadFollows()
  if (selectedGfs.value) await loadDirectory(selectedGfs.value)
  else loading.value = false
}

export default function PublicDiscoveryPage() {
  const me = currentUser.value?.user_id ?? null
  const restricted = isRestricted('public_moments')
  useEffect(() => {
    // Fresh per visit and per account: a list read for someone who signed
    // out since is never shown, not even while this one loads.
    friends.value = null
    void loadFriends()
    void loadHouseholdFollows()
    void loadBlocks()
    if (!restricted) void bootstrapGfs()
  }, [restricted, me])

  const people: Person[] | null = friends.value
    ? discoverFriends(friends.value, { selfId: me, locale: formatLocale(), hidden: isBlocked })
    : null
  const gfses = restricted ? [] : gfsConnections.value
  const noFriends = people !== null && people.length === 0
  const hasGfs = (gfses?.length ?? 0) > 0
  const gfsUnknown = !restricted && gfsConnections.value === null

  const friendsSection = (
    <DiscoverFriends
      people={people}
      failed={friendsFailed.value}
      query={searchQuery.value}
      onRetry={() => { void loadFriends() }}
    />
  )

  // No friends and no GFS: the one card Friends shows says what to do.
  if (noFriends && !hasGfs && !gfsUnknown) {
    return <div class="sh-momentum-discover">{friendsSection}</div>
  }

  const gfsId = selectedGfs.value
  return (
    <div class="sh-momentum-discover">
      <div class="sh-momentum-discover__bar">
        {gfses && gfses.length > 1 && (
          <select
            class="sh-momentum-discover__gfs"
            aria-label={t('moment.discover.gfs_label')}
            value={gfsId ?? ''}
            onChange={(ev) => {
              const next = (ev.currentTarget as HTMLSelectElement).value
              selectedGfs.value = next
              void loadDirectory(next)
            }}
          >
            {gfses.map((g) => (
              <option key={g.id} value={g.id}>{g.display_name}</option>
            ))}
          </select>
        )}
        <input
          type="search"
          class="sh-momentum-discover-search"
          aria-label={t('moment.discover.search')}
          placeholder={t('moment.discover.search')}
          value={searchQuery.value}
          onInput={(ev) => (searchQuery.value = (ev.currentTarget as HTMLInputElement).value)}
        />
      </div>

      {noFriends && hasGfs
        ? (
          <>
            <GfsSection />
            <p class="sh-muted sh-momentum-discover__note">
              {t('moment.discover.friends_empty')}{' '}
              <a href={addBase('/connections')}>{t('moment.discover.open_connections')}</a>
            </p>
          </>
        )
        : (
          <>
            {friendsSection}
            {!restricted && <GfsSection />}
          </>
        )}
    </div>
  )
}

const GFS_HEADING_ID = 'sh-discover-gfs-heading'

/** The people sharing publicly on the selected GFS. */
function GfsSection() {
  if (gfsConnectionsFailed.value && gfsConnections.value === null) {
    return (
      <LoadErrorState
        message={t('moment.discover.connections_failed')}
        detail={t('load.household_quiet')}
        retryLabel={t('load.try_again')}
        onRetry={() => { void loadGfsConnections(true).then(() => bootstrapGfs()) }}
      />
    )
  }
  const gfses = gfsConnections.value
  if (gfses === null) return <Spinner />
  if (gfses.length === 0) {
    return <p class="sh-muted sh-momentum-discover__note">{t('moment.discover.no_gfs_body')}</p>
  }

  const gfsId = selectedGfs.value
  const unreachable = directoryFailed.value !== null && directoryFailed.value === gfsId
  const cached = gfsId ? directoryCache.get(gfsId) : undefined
  const gfsName = gfses.find(g => g.id === gfsId)?.display_name ?? 'GFS'

  const isFollowing = (userId: string) =>
    follows.value.some((f) => f.gfs_id === gfsId && f.followed_user_id === userId)

  const onToggle = async (user: MomentPublicDirectoryUser) => {
    if (!gfsId || busyUser.value) return
    busyUser.value = user.user_id
    try {
      await toggleFollow(
        user.user_id, user.display_name || user.username, isFollowing(user.user_id),
        () => followUser(gfsId, user.user_id), () => unfollowUser(gfsId, user.user_id),
      )
    } finally {
      busyUser.value = null
    }
  }

  const openSheet = (u: MomentPublicDirectoryUser) => {
    openPerson(u.user_id, {
      person: {
        display_name: u.display_name || u.username,
        picture_url: discoveryAvatarUrl(u),
        ...(u.bio ? { bio: u.bio } : {}),
        ...(gfsId ? { public_via: { gfs_id: gfsId } } : {}),
      },
    })
  }

  const rows = strangers.value
  return (
    <section class="sh-discover-section" aria-labelledby={GFS_HEADING_ID}>
      <div class="sh-discover-section__head">
        <h2 id={GFS_HEADING_ID} class="sh-momentum-discover__title">
          {t('moment.discover.heading', { gfs: gfsName })}
        </h2>
      </div>
      <p class="sh-muted sh-momentum-discover__note">
        {t('moment.discover.follow_note', { gfs: gfsName })}
      </p>

      {unreachable && (
        <StaleNotice
          urgent
          message={cached
            ? t('moment.discover.unreachable_cached', {
              gfs: gfsName, age: relativeDocsTime(new Date(cached.at).toISOString()),
            })
            : t('moment.discover.unreachable', { gfs: gfsName })}
          onRetry={() => { if (gfsId) void loadDirectory(gfsId) }}
        />
      )}

      {loading.value && directory.value.length === 0 && <Spinner />}
      {!loading.value && !unreachable && rows.length === 0 && (
        <p class="sh-muted">
          {searchQuery.value ? t('moment.discover.no_match') : t('moment.discover.empty')}
        </p>
      )}

      {rows.length > 0 && (
        <ul class="sh-momentum-discover-list" aria-labelledby={GFS_HEADING_ID}>
          {rows.map((u) => {
            const name = u.display_name || u.username
            return (
              <li key={u.user_id} class="sh-momentum-discover-row">
                <Avatar src={discoveryAvatarUrl(u)} name={name} size={40} />
                <div class="sh-momentum-discover-meta">
                  <button type="button" class="sh-person-btn sh-momentum-discover-name"
                    title={name} onClick={() => openSheet(u)}>
                    <bdi dir="auto">{name}</bdi>
                  </button>
                  <span class="sh-muted sh-momentum-discover-sub">
                    <bdi dir="auto">@{u.username}</bdi>
                  </span>
                  {u.bio && <p class="sh-momentum-discover-bio" dir="auto">{u.bio}</p>}
                </div>
                <FollowToggle
                  name={name}
                  following={isFollowing(u.user_id)}
                  busy={busyUser.value === u.user_id}
                  onClick={() => void onToggle(u)}
                />
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

/** Tests only. */
export function _resetDiscoverForTests(): void {
  directoryCache.clear()
  directoryFailed.value = null
  selectedGfs.value = null
  directory.value = []
  searchQuery.value = ''
  friends.value = null
  friendsFailed.value = false
  busyUser.value = null
}

/** Our own picture-proxy path, as the backend emits it (signed). */
const PROXY_PICTURE_RE =
  /^api\/gfs\/[A-Za-z0-9_-][A-Za-z0-9._:-]*\/moments\/users\/[A-Za-z0-9_-][A-Za-z0-9._:-]*\/picture\?/

/** Avatar for a directory row: only the GFS-mirrored picture, fetched
 *  through our own backend. The backend replaces the household's
 *  self-reported ``picture_url`` (a third-party tracker, or a path that
 *  resolves against our origin) with its signed picture-proxy URL — an
 *  ``<img>`` carries no bearer, so the signature is what lets it load.
 *  Anything else is refused here too, so a stale backend can't leak the
 *  viewer's IP. No URL → the initials fallback. The path is relative so
 *  it resolves under the ingress ``<base href>``. */
export function discoveryAvatarUrl(u: MomentPublicDirectoryUser): string | null {
  const url = u.picture_url
  if (!url || !PROXY_PICTURE_RE.test(url)) return null
  return url
}

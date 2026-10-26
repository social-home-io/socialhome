/**
 * Discover — the Momentum tab (``/momentum/discover``, decision 5) that
 * lists people sharing publicly on one of this household's GFSes.
 *
 * * A GFS select (when there is more than one) and a search box. The
 *   search filters the list here; the GFS never sees what you typed.
 * * People from your own household or a paired household are marked as
 *   such instead of offering a Follow: their moments already reach you
 *   through pairing, and a GFS follow would tell the GFS who follows whom
 *   for nothing (decision 7, "one Follow").
 * * Follow is a GFS follow, and the note under the list says so: the GFS
 *   learns that your household follows that person.
 * * A GFS that doesn't answer keeps the last list it gave in this session,
 *   labelled with its age, plus an inline error with Try again — never
 *   "no one signed up".
 *
 * A name opens the person sheet as someone known through this GFS, so its
 * Follow row is the same GFS follow.
 */
import { useEffect } from 'preact/hooks'
import { computed, signal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { openPerson } from '@/components/PersonSheet'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { Spinner } from '@/components/Spinner'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { relativeDocsTime } from '@/utils/relativeTime'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
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
import type { MomentPublicDirectoryUser } from '@/types'

const selectedGfs = signal<string | null>(null)
const directory = signal<MomentPublicDirectoryUser[]>([])
const loading = signal(true)
const searchQuery = signal('')
const busyUser = signal<string | null>(null)

const filtered = computed<MomentPublicDirectoryUser[]>(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return directory.value
  return directory.value.filter(
    (u) =>
      (u.display_name ?? '').toLowerCase().includes(q) ||
      (u.username ?? '').toLowerCase().includes(q) ||
      (u.bio ?? '').toLowerCase().includes(q),
  )
})

/** The last directory each GFS answered with, and when — kept for the
 *  session so an unreachable GFS still shows who was there, with its age. */
const directoryCache = new Map<string, { users: MomentPublicDirectoryUser[], at: number }>()
/** The GFS whose directory read just failed (unreachable ≠ "no one
 *  signed up"), else null. */
const directoryFailed = signal<string | null>(null)

/** Households the caller already shares moments with: their own and the
 *  paired ones (``GET /api/friends``). ``null`` = not known; then nobody
 *  is marked and every row offers Follow. */
const ownInstance = signal<string | null>(null)
const pairedInstances = signal<Set<string> | null>(null)

interface FriendsOverview {
  instance?: { instance_id?: string | null }
  households?: { instance_id: string }[]
}

async function loadHouseholds(): Promise<void> {
  if (pairedInstances.value !== null) return
  try {
    const r = await api.get<FriendsOverview>('/api/friends')
    ownInstance.value = r?.instance?.instance_id ?? null
    pairedInstances.value = new Set((r?.households ?? []).map((h) => h.instance_id))
  } catch { /* unknown: nobody is marked */ }
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

async function bootstrap(): Promise<void> {
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
  void loadHouseholds()
  if (selectedGfs.value) await loadDirectory(selectedGfs.value)
  else loading.value = false
}

type Relation = 'self' | 'own' | 'paired' | 'public'

function relationOf(u: MomentPublicDirectoryUser, me: string | null): Relation {
  if (me && u.user_id === me) return 'self'
  if (ownInstance.value && u.instance_id === ownInstance.value) return 'own'
  if (pairedInstances.value?.has(u.instance_id)) return 'paired'
  return 'public'
}

export default function PublicDiscoveryPage() {
  const me = currentUser.value?.user_id ?? null
  const restricted = isRestricted('public_moments')
  useEffect(() => {
    if (!restricted) void bootstrap()
  }, [restricted])

  if (restricted) {
    return (
      <div class="sh-momentum-discover">
        <ProtectedNotice capability="public_moments" />
      </div>
    )
  }

  if (gfsConnectionsFailed.value && gfsConnections.value === null) {
    return (
      <div class="sh-momentum-discover">
        <LoadErrorState
          message={t('moment.discover.connections_failed')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          onRetry={() => { void loadGfsConnections(true).then(() => bootstrap()) }}
        />
      </div>
    )
  }

  const gfses = gfsConnections.value
  if (gfses === null) return <div class="sh-momentum-discover"><Spinner /></div>

  if (gfses.length === 0) {
    return (
      <div class="sh-momentum-discover sh-empty-state">
        <h3 style={{ margin: 0 }}>{t('moment.discover.no_gfs_title')}</h3>
        <p>{t('moment.discover.no_gfs_body')}</p>
      </div>
    )
  }

  const gfsId = selectedGfs.value
  const unreachable = directoryFailed.value !== null && directoryFailed.value === gfsId
  const cached = gfsId ? directoryCache.get(gfsId) : undefined
  const gfsName = gfses.find(g => g.id === gfsId)?.display_name ?? 'GFS'

  const isFollowing = (userId: string) =>
    follows.value.some((f) => f.gfs_id === gfsId && f.followed_user_id === userId)

  const onSelectGfs = (next: string) => {
    selectedGfs.value = next
    void loadDirectory(next)
  }

  const onToggle = async (user: MomentPublicDirectoryUser) => {
    if (!gfsId || busyUser.value) return
    const on = isFollowing(user.user_id)
    busyUser.value = user.user_id
    try {
      if (on) {
        await unfollowUser(gfsId, user.user_id)
        showToast(t('user_actions.unfollowed', { name: user.display_name }), 'success')
      } else {
        await followUser(gfsId, user.user_id)
        showToast(t('user_actions.followed', { name: user.display_name }), 'success')
      }
    } catch (err) {
      showToast(
        t(on ? 'user_actions.unfollow_failed' : 'user_actions.follow_failed',
          { error: String((err as Error)?.message ?? err) }),
        'error',
      )
    } finally {
      busyUser.value = null
    }
  }

  const openSheet = (u: MomentPublicDirectoryUser, relation: Relation) => {
    openPerson(u.user_id, {
      person: {
        display_name: u.display_name || u.username,
        picture_url: discoveryAvatarUrl(u),
        ...(u.bio ? { bio: u.bio } : {}),
        // Paired and own people are looked up as themselves; only a
        // stranger is someone known through this GFS.
        ...(relation === 'public' && gfsId ? { public_via: { gfs_id: gfsId } } : {}),
      },
    })
  }

  return (
    <div class="sh-momentum-discover">
      <div class="sh-momentum-discover__bar">
        {gfses.length > 1 && (
          <select
            class="sh-momentum-discover__gfs"
            aria-label={t('moment.discover.gfs_label')}
            value={gfsId ?? ''}
            onChange={(ev) => onSelectGfs((ev.currentTarget as HTMLSelectElement).value)}
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

      <h2 class="sh-momentum-discover__title">{t('moment.discover.heading', { gfs: gfsName })}</h2>

      {loading.value && directory.value.length === 0 && <Spinner />}
      {!loading.value && !unreachable && filtered.value.length === 0 && (
        <p class="sh-muted">
          {searchQuery.value ? t('moment.discover.no_match') : t('moment.discover.empty')}
        </p>
      )}

      {filtered.value.length > 0 && (
        <ul class="sh-momentum-discover-list">
          {filtered.value.map((u) => {
            const relation = relationOf(u, me)
            const name = u.display_name || u.username
            const following = isFollowing(u.user_id)
            return (
              <li key={u.user_id} class="sh-momentum-discover-row">
                <Avatar src={discoveryAvatarUrl(u)} name={name} size={48} />
                <div class="sh-momentum-discover-meta">
                  <button type="button" class="sh-person-btn sh-momentum-discover-name"
                    onClick={() => openSheet(u, relation)}>
                    <bdi dir="auto">{name}</bdi>
                  </button>
                  <span class="sh-muted sh-momentum-discover-sub">
                    {relation === 'paired' ? t('moment.discover.paired_note')
                      : relation === 'own' ? t('moment.discover.own_note')
                        : relation === 'self' ? t('moment.discover.self_note')
                          : <bdi dir="auto">@{u.username}</bdi>}
                  </span>
                  {relation === 'public' && u.bio && (
                    <p class="sh-momentum-discover-bio" dir="auto">{u.bio}</p>
                  )}
                </div>
                {relation === 'paired' && (
                  <span class="sh-momentum-discover-tag">{t('moment.discover.paired_tag')}</span>
                )}
                {relation === 'public' && (
                  <Button
                    variant={following ? 'secondary' : 'primary'}
                    aria-pressed={following ? 'true' : 'false'}
                    aria-label={t(following ? 'moment.discover.following_aria' : 'moment.discover.follow_aria', { name })}
                    loading={busyUser.value === u.user_id}
                    onClick={() => void onToggle(u)}
                  >
                    {following ? t('person.following') : t('user_actions.follow')}
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <p class="sh-muted sh-momentum-discover__note">
        {t('moment.discover.follow_note', { gfs: gfsName })}
      </p>
    </div>
  )
}

/** Tests only. */
export function _resetDiscoverForTests(): void {
  directoryCache.clear()
  directoryFailed.value = null
  selectedGfs.value = null
  directory.value = []
  searchQuery.value = ''
  ownInstance.value = null
  pairedInstances.value = null
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

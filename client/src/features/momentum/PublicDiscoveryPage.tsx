/**
 * PublicDiscoveryPage — browse the GFS public-Momentum directory.
 *
 * Picks a paired GFS, fetches its registered-user directory, and
 * lets the caller follow / unfollow each user. Routed at
 * ``/momentum/public/discover``.
 */
import { useEffect } from 'preact/hooks'
import { computed, signal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { Spinner } from '@/components/Spinner'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { relativeDocsTime } from '@/utils/relativeTime'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import {
  fetchGfsDirectory,
  follows,
  followUser,
  loadFollows,
  unfollowUser,
} from '@/store/momentPublic'
import type { GfsConnection, MomentPublicDirectoryUser } from '@/types'

const gfses = signal<GfsConnection[]>([])
const selectedGfs = signal<string | null>(null)
const directory = signal<MomentPublicDirectoryUser[]>([])
const loading = signal(true)
const searchQuery = signal('')

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

/** The last directory each GFS answered with, and when — kept so an
 *  unreachable GFS still shows who was there, labelled with its age. */
const directoryCache = new Map<string, { users: MomentPublicDirectoryUser[], at: number }>()
/** The GFS whose directory read just failed (unreachable ≠ "no one
 *  signed up"), else null. */
const directoryFailed = signal<string | null>(null)
/** Our own GFS connections didn't load — not the same as having none. */
const connectionsFailed = signal(false)

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
  connectionsFailed.value = false
  directoryFailed.value = null
  try {
    // Active GFS only — a pending/suspended connection can't serve a
    // directory and would auto-select into a doomed fetch.
    const conns = await api.get<GfsConnection[]>('/api/gfs/connections')
    gfses.value = (conns ?? []).filter(c => c.status === 'active')
    if (!gfses.value.some(g => g.id === selectedGfs.value)) {
      selectedGfs.value = gfses.value[0]?.id ?? null
    }
  } catch {
    connectionsFailed.value = true
    loading.value = false
    return
  }
  try {
    await loadFollows()
  } catch { /* the Follow buttons fall back to "Follow"; not fatal */ }
  try {
    if (selectedGfs.value) await loadDirectory(selectedGfs.value)
  } finally {
    loading.value = false
  }
}

export default function PublicDiscoveryPage() {
  useTitle(t('page_title.discover_momentum'))
  useEffect(() => {
    void bootstrap()
  }, [])

  const isFollowing = (userId: string) =>
    follows.value.some(
      (f) => f.gfs_id === selectedGfs.value && f.followed_user_id === userId,
    )

  const onSelectGfs = (gfsId: string) => {
    selectedGfs.value = gfsId
    void loadDirectory(gfsId)
  }

  const onFollow = async (user: MomentPublicDirectoryUser) => {
    if (!selectedGfs.value) return
    try {
      await followUser(selectedGfs.value, user.user_id)
      showToast(t('user_actions.followed', { name: user.display_name }), 'success')
    } catch (err) {
      showToast(
        t('user_actions.follow_failed', { error: String((err as Error)?.message ?? err) }),
        'error',
      )
    }
  }

  const onUnfollow = async (user: MomentPublicDirectoryUser) => {
    if (!selectedGfs.value) return
    try {
      await unfollowUser(selectedGfs.value, user.user_id)
    } catch (err) {
      showToast(
        t('user_actions.unfollow_failed', { error: String((err as Error)?.message ?? err) }),
        'error',
      )
    }
  }

  if (isRestricted('public_moments')) {
    return (
      <div class="sh-momentum-discover">
        <header class="sh-page-header"><h2>{t('page_title.discover_momentum')}</h2></header>
        <ProtectedNotice capability="public_moments" />
      </div>
    )
  }

  if (connectionsFailed.value) {
    return (
      <div class="sh-momentum-discover">
        <header class="sh-page-header"><h2>{t('page_title.discover_momentum')}</h2></header>
        <LoadErrorState
          message={t('moment.discover.connections_failed')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          onRetry={() => void bootstrap()}
        />
      </div>
    )
  }

  if (gfses.value.length === 0) {
    return (
      <div class="sh-empty-state">
        <h3 style={{ margin: 0 }}>{t('moment.discover.no_gfs_title')}</h3>
        <p>{t('moment.discover.no_gfs_body')}</p>
      </div>
    )
  }

  const unreachable = directoryFailed.value !== null && directoryFailed.value === selectedGfs.value
  const cached = selectedGfs.value ? directoryCache.get(selectedGfs.value) : undefined
  const gfsName = gfses.value.find(g => g.id === selectedGfs.value)?.display_name ?? 'GFS'

  return (
    <div class="sh-momentum-discover">
      <header class="sh-page-header">
        <h2>{t('page_title.discover_momentum')}</h2>
        {gfses.value.length > 1 && (
          <select
            value={selectedGfs.value ?? ''}
            onChange={(ev) =>
              onSelectGfs((ev.currentTarget as HTMLSelectElement).value)
            }
          >
            {gfses.value.map((g) => (
              <option key={g.id} value={g.id}>
                {g.display_name}
              </option>
            ))}
          </select>
        )}
      </header>

      <input
        type="search"
        class="sh-momentum-discover-search"
        placeholder={t('moment.discover.search')}
        value={searchQuery.value}
        onInput={(ev) =>
          (searchQuery.value = (ev.currentTarget as HTMLInputElement).value)
        }
      />

      {unreachable && (
        <StaleNotice
          urgent
          message={cached
            ? t('moment.discover.unreachable_cached', {
              gfs: gfsName, age: relativeDocsTime(new Date(cached.at).toISOString()),
            })
            : t('moment.discover.unreachable', { gfs: gfsName })}
          onRetry={() => { if (selectedGfs.value) void loadDirectory(selectedGfs.value) }}
        />
      )}
      {loading.value && <Spinner />}
      {!loading.value && !unreachable && filtered.value.length === 0 && (
        <p class="sh-muted">
          {searchQuery.value
            ? t('moment.discover.no_match')
            : t('moment.discover.empty')}
        </p>
      )}

      <ul class="sh-momentum-discover-list">
        {filtered.value.map((u) => (
          <li key={u.user_id} class="sh-momentum-discover-row">
            <Avatar
              src={discoveryAvatarUrl(u)}
              name={u.display_name || u.username}
              size={48}
            />
            <div class="sh-momentum-discover-meta">
              <strong>{u.display_name}</strong>
              <span class="sh-muted">@{u.username}</span>
              {u.bio && <p class="sh-momentum-discover-bio">{u.bio}</p>}
            </div>
            {isFollowing(u.user_id) ? (
              <Button variant="secondary" onClick={() => void onUnfollow(u)}>
                {t('moment.discover.following')}
              </Button>
            ) : (
              <Button onClick={() => void onFollow(u)}>{t('user_actions.follow')}</Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
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

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

async function loadDirectory(gfsId: string): Promise<void> {
  loading.value = true
  try {
    directory.value = await fetchGfsDirectory(gfsId)
  } catch (err) {
    showToast(
      t('moment.discover.load_failed', { error: String((err as Error)?.message ?? err) }),
      'error',
    )
    directory.value = []
  } finally {
    loading.value = false
  }
}

async function bootstrap(): Promise<void> {
  loading.value = true
  try {
    // Active GFS only — a pending/suspended connection can't serve a
    // directory and would auto-select into a doomed fetch.
    const conns = await api.get<GfsConnection[]>('/api/gfs/connections')
    gfses.value = (conns ?? []).filter(c => c.status === 'active')
    if (gfses.value.length > 0 && !selectedGfs.value) {
      selectedGfs.value = gfses.value[0].id
    }
    await loadFollows()
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

  if (gfses.value.length === 0) {
    return (
      <div class="sh-empty-state">
        <h3 style={{ margin: 0 }}>{t('moment.discover.no_gfs_title')}</h3>
        <p>{t('moment.discover.no_gfs_body')}</p>
      </div>
    )
  }

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

      {loading.value && <Spinner />}
      {!loading.value && filtered.value.length === 0 && (
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

function discoveryAvatarUrl(u: MomentPublicDirectoryUser): string | null {
  // Prefer the GFS-mirrored avatar when we have a digest; falls back
  // to the per-instance picture_url (only reachable when the home
  // instance is publicly addressable).
  if (u.picture_digest) {
    const gfs = selectedGfs.value
    if (gfs) {
      return `/api/gfs/${encodeURIComponent(gfs)}/moments/users/${encodeURIComponent(
        u.user_id,
      )}/picture?v=${encodeURIComponent(u.picture_digest)}`
    }
  }
  return u.picture_url
}

/**
 * Public-Momentum store (§Momentum-public).
 *
 * Caches the current user's GFS registrations + follows so the
 * sidebar / composer can render synchronously without re-fetching.
 * Reads are populated from ``/api/moments/public/*`` on demand;
 * mutations write through the API and update the cache on success.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import type {
  GfsConnection,
  MomentPublicDirectoryUser,
  MomentPublicFollow,
  MomentPublicRegistration,
} from '@/types'

export const registrations = signal<MomentPublicRegistration[]>([])
export const follows = signal<MomentPublicFollow[]>([])

export async function loadRegistrations(): Promise<void> {
  try {
    const rows = (await api.get(
      '/api/moments/public/registrations',
    )) as MomentPublicRegistration[]
    registrations.value = rows ?? []
  } catch {
    /* not authed / transient; leave the cache untouched */
  }
}

export async function loadFollows(): Promise<void> {
  try {
    const rows = (await api.get(
      '/api/moments/public/follows',
    )) as MomentPublicFollow[]
    follows.value = rows ?? []
  } catch {
    /* not authed / transient */
  }
}

/** Register on a GFS. Without ``defaultShare`` the server decides: off
 *  for a new registration (owner decision 4 — each moment is public only
 *  when its author switches it on), unchanged for an existing one. */
export async function registerOnGfs(
  gfsId: string,
  defaultShare?: boolean,
): Promise<void> {
  const body: Record<string, unknown> = { gfs_id: gfsId }
  if (defaultShare !== undefined) body.default_share = defaultShare
  const row = (await api.post('/api/moments/public/registrations', body)) as MomentPublicRegistration
  registrations.value = [
    ...registrations.value.filter((r) => r.gfs_id !== gfsId),
    row,
  ]
}

export async function deregisterFromGfs(gfsId: string): Promise<void> {
  await api.delete(`/api/moments/public/registrations/${gfsId}`)
  registrations.value = registrations.value.filter((r) => r.gfs_id !== gfsId)
}

export async function setDefaultShare(
  gfsId: string,
  defaultShare: boolean,
): Promise<void> {
  await api.patch(`/api/moments/public/registrations/${gfsId}`, {
    default_share: defaultShare,
  })
  registrations.value = registrations.value.map((r) =>
    r.gfs_id === gfsId ? { ...r, default_share: defaultShare } : r,
  )
}

export async function followUser(
  gfsId: string,
  followedUserId: string,
): Promise<MomentPublicFollow> {
  const row = (await api.post('/api/moments/public/follows', {
    gfs_id: gfsId,
    followed_user_id: followedUserId,
  })) as MomentPublicFollow
  follows.value = [
    ...follows.value.filter(
      (f) =>
        !(f.gfs_id === gfsId && f.followed_user_id === followedUserId),
    ),
    row,
  ]
  return row
}

export async function unfollowUser(
  gfsId: string,
  followedUserId: string,
): Promise<void> {
  await api.delete(
    `/api/moments/public/follows/${gfsId}/${followedUserId}`,
  )
  follows.value = follows.value.filter(
    (f) =>
      !(f.gfs_id === gfsId && f.followed_user_id === followedUserId),
  )
}

export async function fetchGfsDirectory(
  gfsId: string,
): Promise<MomentPublicDirectoryUser[]> {
  const data = (await api.get(`/api/gfs/${gfsId}/moments/users`)) as {
    users?: MomentPublicDirectoryUser[]
  }
  return data.users ?? []
}

/** This household's active GFS connections (``null`` = not read yet or
 *  the read failed — never "none"). Discover, the person sheet's GFS
 *  follow and Settings › Public sharing read them; once per session. */
export const gfsConnections = signal<GfsConnection[] | null>(null)
/** The last read of the connections failed (an error, not "no GFS"). */
export const gfsConnectionsFailed = signal(false)
let gfsLoading: Promise<GfsConnection[] | null> | null = null

export function loadGfsConnections(force = false): Promise<GfsConnection[] | null> {
  if (gfsLoading && !force) return gfsLoading
  gfsLoading = Promise.resolve()
    .then(() => api.get<GfsConnection[]>('/api/gfs/connections'))
    .then((rows) => {
      // Only an active server serves a directory, takes a follow or a
      // registration (``_require_active_gfs``).
      gfsConnections.value = (rows ?? []).filter((c) => c.status === 'active')
      gfsConnectionsFailed.value = false
      return gfsConnections.value
    })
    .catch(() => {
      gfsConnectionsFailed.value = true
      gfsLoading = null
      return gfsConnections.value
    })
  return gfsLoading
}

/** A connection's display name, or ``null`` when unknown. */
export function gfsName(gfsId: string | null | undefined): string | null {
  if (!gfsId) return null
  return gfsConnections.value?.find((g) => g.id === gfsId)?.display_name ?? null
}

/** The caller's GFS follow of *userId* (on any server), if any. */
export function gfsFollowOf(userId: string): MomentPublicFollow | undefined {
  return follows.value.find((f) => f.followed_user_id === userId)
}

/** Tests only. */
export function _resetGfsConnectionsForTests(): void {
  gfsLoading = null
  gfsConnections.value = null
  gfsConnectionsFailed.value = false
}

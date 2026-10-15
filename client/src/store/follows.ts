/**
 * Follow list — viewer-private (§Momentum).
 *
 * Mirrors ``GET /api/moments/follows`` so the Following panel and any
 * inline follow toggle can render without waiting on a fetch
 * round-trip. The server enforces the relationship; this store is an
 * optimistic cache of who the *current viewer* is following.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import type { PersonSummary } from '@/types'

export interface FollowEntry extends PersonSummary {
  created_at: string
}

export const followedUsers = signal<FollowEntry[]>([])
export const followedUserIds = signal<Set<string>>(new Set())

let _loaded = false

function syncIds(rows: FollowEntry[]): void {
  followedUsers.value = rows
  followedUserIds.value = new Set(rows.map(r => r.user_id))
}

export async function loadFollows(force = false): Promise<FollowEntry[]> {
  if (_loaded && !force) return followedUsers.value
  try {
    const r = await api.get('/api/moments/follows') as { follows: FollowEntry[] }
    syncIds(r.follows ?? [])
    _loaded = true
  } catch {
    // Auth not ready yet, or transient — leave the cache empty so
    // surfaces still render. Re-attempted on next call.
  }
  return followedUsers.value
}

export async function followUser(userId: string): Promise<void> {
  await api.post('/api/moments/follows', { user_id: userId })
  if (!followedUserIds.value.has(userId)) {
    syncIds([
      { user_id: userId, created_at: new Date().toISOString() },
      ...followedUsers.value,
    ])
  }
}

export async function unfollowUser(userId: string): Promise<void> {
  await api.delete(`/api/moments/follows/${encodeURIComponent(userId)}`)
  syncIds(followedUsers.value.filter(f => f.user_id !== userId))
}

export function isFollowing(userId: string | null | undefined): boolean {
  if (!userId) return false
  return followedUserIds.value.has(userId)
}

/** Test helper — reset the in-memory cache without hitting the API. */
export function _resetFollowsForTest(): void {
  _loaded = false
  followedUsers.value = []
  followedUserIds.value = new Set()
}

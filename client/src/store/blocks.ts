/**
 * Personal block list — viewer-private (§Privacy).
 *
 * Mirrors ``GET /api/blocks`` so UI surfaces (Highlights ring, Highlight
 * viewer header, Settings → Privacy) can hide rows without waiting on
 * a fetch round-trip. The server enforces visibility — this store is
 * an optimistic cache.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import type { PersonSummary } from '@/types'

export interface BlockEntry extends PersonSummary {
  blocked_at: string
}

export const blockedUsers = signal<BlockEntry[]>([])
export const blockedUserIds = signal<Set<string>>(new Set())

let _loaded = false

function syncIds(rows: BlockEntry[]): void {
  blockedUsers.value = rows
  blockedUserIds.value = new Set(rows.map(r => r.user_id))
}

export async function loadBlocks(force = false): Promise<BlockEntry[]> {
  if (_loaded && !force) return blockedUsers.value
  try {
    const r = await api.get('/api/blocks') as { blocks: BlockEntry[] }
    syncIds(r.blocks ?? [])
    _loaded = true
  } catch {
    // Auth not ready yet, or transient — leave the cache empty so
    // surfaces still render. Re-attempted on next call.
  }
  return blockedUsers.value
}

export async function blockUser(userId: string): Promise<void> {
  await api.post('/api/blocks', { user_id: userId })
  // Optimistic insert with current timestamp; server's blocked_at
  // back-fills on next loadBlocks(true) without changing the set.
  if (!blockedUserIds.value.has(userId)) {
    syncIds([
      { user_id: userId, blocked_at: new Date().toISOString() },
      ...blockedUsers.value,
    ])
  }
}

export async function unblockUser(userId: string): Promise<void> {
  await api.delete(`/api/blocks/${encodeURIComponent(userId)}`)
  syncIds(blockedUsers.value.filter(b => b.user_id !== userId))
}

export function isBlocked(userId: string | null | undefined): boolean {
  if (!userId) return false
  return blockedUserIds.value.has(userId)
}

/** Test helper — reset the in-memory cache without hitting the API. */
export function _resetBlocksForTest(): void {
  _loaded = false
  blockedUsers.value = []
  blockedUserIds.value = new Set()
}

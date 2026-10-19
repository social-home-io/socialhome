/**
 * Module-level cache of space DirectoryEntry rows the user has seen on
 * the Spaces page's Discover section, keyed by space_id.
 *
 * The :class:`SpacePublicDetailPage` reads from this cache so a tap
 * from the browser lands instantly on a populated detail view — no
 * second round-trip for data the SPA already has. A direct cold-link
 * (e.g. someone shares the URL) falls back to ``/api/spaces/{id}``
 * (local spaces) or shows a "load the directory first" prompt.
 */
import { signal } from '@preact/signals'
import type { DirectoryEntry } from '@/types'

export const directoryCache = signal<Map<string, DirectoryEntry>>(new Map())

export function cacheDirectoryEntries(entries: DirectoryEntry[]): void {
  const next = new Map(directoryCache.value)
  for (const e of entries) next.set(e.space_id, e)
  directoryCache.value = next
}

export function getCachedEntry(spaceId: string): DirectoryEntry | null {
  return directoryCache.value.get(spaceId) ?? null
}

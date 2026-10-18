/**
 * What each section of a space holds — the meta line under the section
 * rows in More ("5 open · 2 yours", "2 albums · 5 photos").
 *
 * ``GET /api/spaces/{id}/summary`` counts at request time; a section the
 * space has turned off comes back ``null``. More reads it each time it
 * opens (cheap, and never stale for long). A failed read just leaves the
 * rows without a meta line — the label is what matters.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { isOne, t } from '@/i18n/i18n'
import type { SpaceSection } from './spaceTabs'

export interface SpaceSummary {
  tasks: { open: number; mine: number } | null
  pages: { count: number } | null
  gallery: { albums: number; items: number } | null
  stickies: { count: number } | null
  bazaar: { active: number } | null
  calendar: { upcoming: number; days: number } | null
}

const n = (count: number) => ({ count: String(count) })

/** The meta line for ``section``, or ``null`` (no count for it, or the
 *  summary isn't known). Literal keys so the i18n check sees them. */
export function sectionMeta(section: SpaceSection, s: SpaceSummary | null): string | null {
  if (!s) return null
  switch (section) {
    case 'tasks': {
      if (!s.tasks) return null
      if (s.tasks.open === 0) return t('space.summary.tasks_none')
      const open = t('space.summary.tasks_open', n(s.tasks.open))
      return s.tasks.mine > 0 ? `${open} · ${t('space.summary.tasks_mine', n(s.tasks.mine))}` : open
    }
    case 'pages':
      if (!s.pages) return null
      if (s.pages.count === 0) return t('space.summary.pages_none')
      return t(isOne(s.pages.count) ? 'space.summary.pages_one' : 'space.summary.pages', n(s.pages.count))
    case 'gallery': {
      if (!s.gallery) return null
      const { albums, items } = s.gallery
      if (albums === 0) return t('space.summary.albums_none')
      const a = t(isOne(albums) ? 'space.summary.albums_one' : 'space.summary.albums', n(albums))
      const p = t(isOne(items) ? 'space.summary.photos_one' : 'space.summary.photos', n(items))
      return `${a} · ${p}`
    }
    case 'stickies':
      if (!s.stickies) return null
      if (s.stickies.count === 0) return t('space.summary.stickies_none')
      return t(isOne(s.stickies.count) ? 'space.summary.stickies_one' : 'space.summary.stickies', n(s.stickies.count))
    case 'bazaar':
      if (!s.bazaar) return null
      if (s.bazaar.active === 0) return t('space.summary.bazaar_none')
      return t('space.summary.bazaar', n(s.bazaar.active))
    case 'calendar':
      if (!s.calendar) return null
      if (s.calendar.upcoming === 0) return t('space.summary.events_none')
      return t('space.summary.events', n(s.calendar.upcoming))
    default:
      return null
  }
}

/** The summary while ``enabled`` (More is open), re-read each time it
 *  turns on; ``null`` until it answers or when it fails. */
export function useSpaceSummary(spaceId: string, enabled: boolean): SpaceSummary | null {
  const [summary, setSummary] = useState<SpaceSummary | null>(null)
  useEffect(() => {
    if (!enabled) return
    let stopped = false
    api.get<SpaceSummary>(`/api/spaces/${spaceId}/summary`)
      .then((body) => { if (!stopped) setSummary(body) })
      .catch(() => { if (!stopped) setSummary(null) })
    return () => { stopped = true }
  }, [spaceId, enabled])
  return summary
}

/**
 * A space's quick links — the admin-configured shortcuts every member
 * sees as chips in the space hero (``SpaceHero``).
 *
 * Only http(s) links are kept: the server accepts nothing else, and a
 * row that predates that check (or any other scheme) is dropped, never
 * linked. No favicon is fetched for a link — the chip shows a plain link
 * icon (no third-party fetches).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { safeHref } from '@/utils/safeHref'

interface WireLink {
  id: string
  label: string
  url: string
  position: number
}

export interface QuickLink {
  id: string
  label: string
  href: string
}

/** The links that may be followed, in the admin's order. */
export function quickLinks(rows: readonly WireLink[]): QuickLink[] {
  return rows.flatMap((link) => {
    const href = safeHref(link.url)
    if (!href || !/^https?:/i.test(href)) return []
    return [{ id: link.id, label: link.label, href }]
  })
}

/** ``GET /api/spaces/{id}/links`` — empty until it answers, and on any
 *  error (a follower without access, offline): the hero then simply has
 *  no chips. */
export function useSpaceLinks(spaceId: string): QuickLink[] {
  const [links, setLinks] = useState<QuickLink[]>([])
  useEffect(() => {
    let stopped = false
    setLinks([])
    api.get(`/api/spaces/${spaceId}/links`)
      .then((body) => {
        if (!stopped) setLinks(quickLinks((body as { links?: WireLink[] }).links ?? []))
      })
      .catch(() => { /* no chips */ })
    return () => { stopped = true }
  }, [spaceId])
  return links
}

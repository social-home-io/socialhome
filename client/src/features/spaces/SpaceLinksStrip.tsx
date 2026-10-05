/**
 * SpaceLinksStrip — member-facing display of the admin-configured
 * quick-links for a space.
 *
 * Renders as a horizontal pill row under the space hero. External URLs
 * open in a new tab with ``rel="noopener noreferrer"``. Hidden when no
 * links are configured to avoid an empty strip.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { safeHref } from '@/utils/safeHref'

interface SpaceLink {
  id: string
  label: string
  url: string
  position: number
}

interface Props {
  spaceId: string
}

export function SpaceLinksStrip({ spaceId }: Props) {
  const [links, setLinks] = useState<SpaceLink[]>([])

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const body = await api.get(`/api/spaces/${spaceId}/links`) as {
          links: SpaceLink[]
        }
        if (!stopped) setLinks(body.links)
      } catch {
        // Non-member / 404 → just render nothing.
      }
    }
    void load()
    return () => { stopped = true }
  }, [spaceId])

  if (links.length === 0) return null

  return (
    <div class="sh-space-links-strip" role="navigation" aria-label={t('space.links.title')}>
      {links.flatMap(link => {
        // Quick links are http(s)-only server-side; a row that predates
        // that check (or any other scheme) is skipped, not linked.
        const href = safeHref(link.url)
        if (!href || !/^https?:/i.test(href)) return []
        return [(
        <a key={link.id}
           href={href}
           target="_blank"
           rel="noopener noreferrer"
           class="sh-space-links-strip__link">
          {link.label}
        </a>
        )]
      })}
    </div>
  )
}

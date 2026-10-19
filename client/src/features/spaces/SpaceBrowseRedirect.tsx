/**
 * SpaceBrowseRedirect — keeps the retired ``/spaces/browse`` URL working.
 * Browsing is the Spaces page's Discover section now; a bookmark lands on
 * ``/spaces?discover=…`` (an old ``?tab=`` name maps onto the chips),
 * replacing the entry so Back doesn't bounce through here. ``route`` takes
 * the in-app path; the router adds any ingress prefix.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { parseDiscoverSource } from './discover'

export default function SpaceBrowseRedirect() {
  const loc = useLocation()
  useEffect(() => {
    const src = parseDiscoverSource(loc.query?.discover ?? loc.query?.tab) ?? 'household'
    loc.route(`/spaces?discover=${src}`, true)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps -- once, on mount
  return null
}

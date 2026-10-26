/**
 * MomentumArchiveRedirect — preserves the legacy ``/momentum/archive``
 * URL: a hashtag link (``?tag=…``) goes to ``/momentum?tag=…``, anything
 * else to your own moments, ``/momentum/yours`` (the Archive tab
 * became Yours, decision 6a).
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export default function MomentumArchiveRedirect() {
  const loc = useLocation()
  useEffect(() => {
    const q = loc.url.split('?')[1] ?? ''
    const params = new URLSearchParams(q)
    const tag = params.get('tag')
    const target = tag
      ? `/momentum?tag=${encodeURIComponent(tag)}`
      : '/momentum/yours'
    loc.route(target, true)
  }, [])
  return null
}

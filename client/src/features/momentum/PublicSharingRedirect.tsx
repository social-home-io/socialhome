/**
 * PublicSharingRedirect — the old ``/momentum/public/sharing`` page moved
 * to Settings › Privacy › Public sharing (decision 5); old links (a
 * bookmark, an older pairing screen) land there.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'

export const PUBLIC_SHARING_PATH = '/settings#public-sharing'

export default function PublicSharingRedirect() {
  const loc = useLocation()
  useEffect(() => { loc.route(PUBLIC_SHARING_PATH, true) }, [])
  return null
}

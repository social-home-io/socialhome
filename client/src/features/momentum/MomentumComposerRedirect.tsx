/**
 * MomentumComposerRedirect — back-compat for the deep link ``/momentum/new``.
 *
 * The composer used to be a routed full page. It's now a Modal mounted on
 * the host ``MomentumPage``. To keep bookmarks and notification links
 * working, the old route still resolves: this opens the dialog and
 * replaces the URL with ``/momentum`` so the dialog has somewhere to mount.
 * (``/momentum/{id}/reply`` is the moment page with its reply field
 * focused — see :mod:`./MomentumDetailPage`.)
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { openMomentumComposer } from '@/components/MomentumComposerDialog'

export default function MomentumComposerRedirect() {
  const loc = useLocation()

  useEffect(() => {
    openMomentumComposer()
    // Replace, not push — the deep link itself shouldn't appear in
    // history; a back-tap from the dialog should leave the user on
    // wherever they came from, not on the redirect URL.
    loc.route('/momentum', true)
  }, [])

  // Render nothing while the redirect happens. The host page's spinner
  // (or content) takes over within the same tick.
  return null
}

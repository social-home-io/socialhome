/**
 * Asking before joining a GFS space (v_97).
 *
 * A global space carries everything only on its channel at a GFS, so a
 * household joins (or follows) it only once it is connected to a GFS
 * (owner sign-off 2, 2026-11-27). Before the join, the SPA asks the
 * backend (``GET /api/public_spaces/{id}/join-preflight``); when the
 * household is not connected, it asks the person first:
 *
 * - an admin: "Connect and join" connects the household to the default
 *   GFS (``POST /api/gfs/connections/default``) and then joins; Cancel
 *   sends nothing;
 * - anyone else: an admin must connect the household — the dialog says so
 *   and nothing is sent.
 */
import { api } from '@/api'
import { confirmDialog } from './confirm'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import { gfsConnectErrorText } from '@/features/connections/gfsErrors'

interface Preflight {
  gfs_space: boolean
  connected: boolean
  may_connect: boolean
}

/** ``true`` when the join may go ahead now. ``spaceType`` is the
 *  directory entry's (``global`` → a GFS space). */
export async function askGfsConsent(
  spaceId: string,
  spaceType: string,
  spaceName: string,
): Promise<boolean> {
  let pre: Preflight
  try {
    pre = await api.get(
      `/api/public_spaces/${encodeURIComponent(spaceId)}/join-preflight?space_type=${encodeURIComponent(spaceType)}`,
    ) as Preflight
  } catch {
    // The preflight is a convenience: the join itself still works or says
    // why not.
    return true
  }
  if (!pre?.gfs_space || pre.connected) return true
  if (!pre.may_connect) {
    await confirmDialog(t('gfs_join.consent.ask_admin', { name: spaceName }), {
      title: t('gfs_join.consent.title'),
      confirmLabel: t('gfs_join.consent.ok'),
      notice: true,
    })
    return false
  }
  const yes = await confirmDialog(t('gfs_join.consent.body', { name: spaceName }), {
    title: t('gfs_join.consent.title'),
    confirmLabel: t('gfs_join.consent.connect'),
    cancelLabel: t('common.cancel'),
  })
  if (!yes) return false
  try {
    await api.post('/api/gfs/connections/default')
    return true
  } catch (err) {
    showToast(gfsConnectErrorText(err), 'error')
    return false
  }
}

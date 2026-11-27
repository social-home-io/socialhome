/**
 * Asking before joining a GFS space (v_97).
 *
 * A GFS space (global, or private with ``private_gfs``) carries everything
 * only on its channel at the GFSs it is listed on, so a household joins (or
 * follows) one only once it is connected to one of them (owner sign-off 2,
 * 2026-11-27 — firm consent). Before the join, the SPA asks the backend
 * (``GET /api/public_spaces/{id}/join-preflight``), which decides what the
 * space is from what it knows; when the household is not connected, it
 * asks the person first:
 *
 * - an admin: "Connect and join" connects the household to the default
 *   GFS (``POST /api/gfs/connections/default``), checks again and then
 *   joins; Cancel sends nothing;
 * - anyone else: an admin must connect the household — the dialog says so
 *   and nothing is sent.
 *
 * Fails closed: a preflight that does not answer is no "yes" — nothing is
 * sent and the person is told to try again. The server refuses the join
 * the same way (``409 GFS_CONNECTION_REQUIRED``).
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

/** How often, and how far apart, the preflight is asked again after a
 *  connect: the household reads the new GFS's directory in the background. */
export const RECHECK_TRIES = 4
export const RECHECK_DELAY_MS = 1500

async function preflight(spaceId: string): Promise<Preflight> {
  return await api.get(
    `/api/public_spaces/${encodeURIComponent(spaceId)}/join-preflight`,
  ) as Preflight
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/** ``true`` when the join may go ahead now. */
export async function askGfsConsent(
  spaceId: string,
  spaceName: string,
): Promise<boolean> {
  let pre: Preflight
  try {
    pre = await preflight(spaceId)
  } catch {
    // Fail closed: without an answer we don't know the household may join.
    showToast(t('gfs_join.consent.check_failed'), 'error')
    return false
  }
  if (!pre || typeof pre.gfs_space !== 'boolean') {
    showToast(t('gfs_join.consent.check_failed'), 'error')
    return false
  }
  if (!pre.gfs_space || pre.connected) return true
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
  } catch (err) {
    showToast(gfsConnectErrorText(err), 'error')
    return false
  }
  // Connected: the space must be listed on that GFS too. Ask again while
  // the household reads its directory.
  for (let i = 0; i < RECHECK_TRIES; i++) {
    try {
      const again = await preflight(spaceId)
      if (!again.gfs_space || again.connected) return true
    } catch {
      // Ask again below.
    }
    if (i < RECHECK_TRIES - 1) await wait(RECHECK_DELAY_MS)
  }
  await confirmDialog(t('gfs_join.consent.not_listed', { name: spaceName }), {
    title: t('gfs_join.consent.title'),
    confirmLabel: t('gfs_join.consent.ok'),
    notice: true,
  })
  return false
}

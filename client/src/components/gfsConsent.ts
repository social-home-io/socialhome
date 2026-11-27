/**
 * Asking before joining a GFS space (v_97).
 *
 * A GFS space (global, or private with ``private_gfs``) carries everything
 * only through its channel at the GFSs it is listed on, so a household joins
 * (or follows) one only once it is connected to one of them (owner sign-off
 * 2, 2026-11-27 — firm consent). Before the join, the SPA asks the backend
 * (``GET /api/public_spaces/{id}/join-preflight``), which decides what the
 * space is from what it knows; when the household is not connected, it asks
 * the person first:
 *
 * - an admin, with a default GFS to connect (``default_gfs``, named in the
 *   dialog): "Connect and join" connects the household to it
 *   (``POST /api/gfs/connections/default``), checks again and then joins;
 *   Cancel sends nothing. If that GFS doesn't list the space, the person is
 *   told — the connection stays, and can be removed under Connections;
 * - an admin with no default to connect: "Open Connections", to connect a
 *   GFS that lists the space;
 * - anyone else: an admin must connect the household — the dialog says so
 *   and nothing is sent.
 *
 * Fails closed: a preflight that does not answer is no "yes" — nothing is
 * sent and the person is told to try again. The server refuses the join
 * the same way (``409 GFS_CONNECTION_REQUIRED``).
 */
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { confirmDialog } from './confirm'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import { gfsConnectErrorText } from '@/features/connections/gfsErrors'

interface Preflight {
  gfs_space: boolean
  connected: boolean
  may_connect: boolean
  default_gfs?: string | null
}

/** What the person is about to do: the dialog's words follow it. */
export type GfsConsentAction = 'join' | 'follow' | 'request'

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
  action: GfsConsentAction = 'join',
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
  const follow = action === 'follow'
  const title = t('gfs_join.consent.title')
  if (!pre.may_connect) {
    await confirmDialog(
      t(follow ? 'gfs_join.consent.ask_admin_follow' : 'gfs_join.consent.ask_admin', { name: spaceName }),
      { title, confirmLabel: t('gfs_join.consent.ok'), notice: true },
    )
    return false
  }
  const gfs = pre.default_gfs ?? null
  if (!gfs) {
    // No default to connect here: the admin picks a GFS that lists it.
    const go = await confirmDialog(t('gfs_join.consent.no_default', { name: spaceName }), {
      title,
      confirmLabel: t('gfs_join.consent.open_connections'),
      cancelLabel: t('common.cancel'),
    })
    if (go) window.location.assign(addBase('/connections'))
    return false
  }
  const yes = await confirmDialog(
    t(follow ? 'gfs_join.consent.body_follow' : 'gfs_join.consent.body', { name: spaceName, gfs }),
    {
      title,
      confirmLabel: t(
        follow ? 'gfs_join.consent.connect_follow'
          : action === 'request' ? 'gfs_join.consent.connect_continue'
            : 'gfs_join.consent.connect',
      ),
      cancelLabel: t('common.cancel'),
    },
  )
  if (!yes) return false
  showToast(t('gfs_join.consent.connecting', { gfs }), 'info')
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
  await confirmDialog(t('gfs_join.consent.not_listed', { name: spaceName, gfs }), {
    title,
    confirmLabel: t('gfs_join.consent.ok'),
    notice: true,
  })
  return false
}

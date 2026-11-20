/**
 * SpaceGfsNotice — "this space is on a GFS your household isn't on",
 * shown in the space itself (the settings list it too, read-only).
 *
 * Read at request time from the owner's signed list (``space.gfs_servers``,
 * each with ``connected`` for this household) — a global space, or a
 * private one using the GFS (``features.private_gfs``). One row per GFS
 * this household isn't connected to:
 *
 * - **Needed** when this household stands in for the host
 *   (``stands_in_for_host``: a delegated admin holding the signing seed).
 *   It can't be dismissed while the GFS is missing.
 * - **Optional** for everyone else: "connect for a steadier connection",
 *   dismissible. The dismissal is a per-viewer convenience in
 *   ``localStorage`` — per person, per space, per GFS — so a GFS the owner
 *   adds later still shows.
 *
 * A household admin gets a one-click "Connect" that opens the Connections
 * page's GFS flow aimed at that GFS (``/connections?connect_gfs=…``,
 * anchored on the document base); anyone else gets the "ask a household
 * admin" hint instead.
 */
import { useState } from 'preact/hooks'
import type { JSX } from 'preact'
import { addBase } from '@/baseUrl'
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import { gfsHost } from './GfsChecklist'
import { hasChannelServersToShow, hasGfsServersToShow } from './SpaceGfsListing'
import type { Space, SpaceGfsServer } from '@/types'

export type GfsNoticeSpace = Pick<Space, 'id' | 'space_type' | 'gfs_servers'> & {
  features?: Space['features'] | null
  gfs_unlisted?: boolean
  stands_in_for_host?: boolean
}

const KEY = 'sh.space_gfs_notice.dismissed:'

function dismissKey(userId: string, spaceId: string, inboxUrl: string): string {
  return `${KEY}${userId}:${spaceId}:${inboxUrl}`
}

/** Whether this person hid the optional notice for *inboxUrl* in *spaceId*. */
export function isGfsNoticeDismissed(
  userId: string | null | undefined, spaceId: string, inboxUrl: string,
): boolean {
  if (!userId) return false
  try {
    return window.localStorage.getItem(dismissKey(userId, spaceId, inboxUrl)) === '1'
  } catch {
    return false
  }
}

export function dismissGfsNotice(
  userId: string | null | undefined, spaceId: string, inboxUrl: string,
): void {
  if (!userId) return
  try {
    window.localStorage.setItem(dismissKey(userId, spaceId, inboxUrl), '1')
  } catch {
    // Storage blocked (private window, quota): hidden for this visit only.
  }
}

/** The in-app path that opens the GFS connect flow aimed at *inboxUrl*. */
export function connectGfsPath(inboxUrl: string): string {
  return `/connections?connect_gfs=${encodeURIComponent(inboxUrl)}`
}

/** The listed GFSs this household isn't connected to, for a space that
 *  uses GFSs at all (global, or private with the GFS channel). */
export function missingGfsServers(space: GfsNoticeSpace): SpaceGfsServer[] {
  const s = space as Space
  if (!hasGfsServersToShow(s) && !hasChannelServersToShow(s)) return []
  return (space.gfs_servers ?? []).filter(g => !g.connected)
}

export function SpaceGfsNotice({ space }: { space: GfsNoticeSpace }): JSX.Element | null {
  const userId = currentUser.value?.user_id ?? null
  const isAdmin = currentUser.value?.is_admin === true
  const needed = space.stands_in_for_host === true
  // Bumped on a dismiss so the row goes at once (storage may be blocked).
  const [hidden, setHidden] = useState<string[]>([])
  const rows = missingGfsServers(space).filter(g => needed || (
    !hidden.includes(g.inbox_url) && !isGfsNoticeDismissed(userId, space.id, g.inbox_url)
  ))
  if (rows.length === 0) return null
  return (
    <div class="sh-space-gfs-notice" data-testid="space-gfs-notice">
      {rows.map(g => {
        const name = g.display_name || gfsHost(g.inbox_url)
        return (
          <div key={g.inbox_url}
               class={`sh-space-gfs-notice__row${needed ? ' sh-space-gfs-notice__row--needed' : ''}`}
               role="status"
               data-testid={needed ? 'space-gfs-notice-needed' : 'space-gfs-notice-optional'}>
            <span class="sh-space-gfs-notice__icon" aria-hidden="true">{needed ? '⚠️' : '🛰️'}</span>
            <div class="sh-space-gfs-notice__body">
              <p class="sh-space-gfs-notice__text">
                {needed
                  ? t('space.gfs_notice.needed', { name })
                  : t('space.gfs_notice.optional', { name })}
              </p>
              {isAdmin ? (
                <a class={`sh-btn sh-btn--${needed ? 'primary' : 'secondary'} sh-space-gfs-notice__connect`}
                   href={addBase(connectGfsPath(g.inbox_url))}
                   data-testid="space-gfs-notice-connect">
                  {t('space.gfs_notice.connect', { name })}
                </a>
              ) : (
                <p class="sh-muted sh-space-gfs-notice__hint" data-testid="space-gfs-notice-ask-admin">
                  {t('space.gfs_listing.connect_hint_member', { name })}
                </p>
              )}
            </div>
            {!needed && (
              <button type="button" class="sh-space-gfs-notice__dismiss"
                      aria-label={t('space.gfs_notice.dismiss_aria', { name })}
                      title={t('common.close')}
                      data-testid="space-gfs-notice-dismiss"
                      onClick={() => {
                        dismissGfsNotice(userId, space.id, g.inbox_url)
                        setHidden(h => [...h, g.inbox_url])
                      }}>
                ✕
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

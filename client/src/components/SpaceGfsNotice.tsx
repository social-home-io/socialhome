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
 * - **Required** (v_97, ``gfs_connect_needed``): a GFS space hosted
 *   elsewhere whose content rides only its channel, and this household holds
 *   no connection that can carry it to any of its GFSs — so its posts stop
 *   reaching the household (at once, or when the 14-day window after the
 *   switch ends). Every listed GFS we aren't on is a row; none can be
 *   dismissed. With no row to name (no list known, or the GFS we're on
 *   can't carry it yet) one row points at Connections.
 *
 * A household admin gets a one-click "Connect" that opens the Connections
 * page's GFS flow aimed at that GFS (``/connections?connect_gfs=…``,
 * anchored on the document base); anyone else gets the "ask a household
 * admin" hint instead.
 */
import { useRef, useState } from 'preact/hooks'
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
  /** v_97 — the household must connect a GFS of this space (see above). */
  gfs_connect_needed?: boolean
  /** The space's name, for the Required copy. */
  name?: string
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

/** v_97 — the Required prompt: this household must connect a GFS of the
 *  space to keep getting it. */
function RequiredGfsNotice({ space }: { space: GfsNoticeSpace }): JSX.Element {
  const isAdmin = currentUser.value?.is_admin === true
  const spaceName = space.name ?? ''
  const rows: Array<{ key: string; name: string | null; href: string }> =
    (space.gfs_servers ?? []).filter(g => !g.connected).map(g => ({
      key: g.inbox_url,
      name: g.display_name || gfsHost(g.inbox_url),
      href: connectGfsPath(g.inbox_url),
    }))
  if (rows.length === 0) rows.push({ key: '', name: null, href: '/connections' })
  return (
    <div class="sh-space-gfs-notice" role="status" data-testid="space-gfs-notice">
      {rows.map(r => (
        <div key={r.key}
             class="sh-space-gfs-notice__row sh-space-gfs-notice__row--needed"
             data-testid="space-gfs-notice-required">
          <span class="sh-space-gfs-notice__icon" aria-hidden="true">⚠️</span>
          <div class="sh-space-gfs-notice__body">
            <p class="sh-space-gfs-notice__text">
              {r.name
                ? t(isAdmin ? 'space.gfs_notice.required' : 'space.gfs_notice.required_member',
                  { space: spaceName, name: r.name })
                : t(isAdmin ? 'space.gfs_notice.required_any' : 'space.gfs_notice.required_any_member',
                  { space: spaceName })}
            </p>
            {isAdmin && (
              <a class="sh-btn sh-btn--primary sh-space-gfs-notice__connect"
                 href={addBase(r.href)}
                 aria-label={r.name
                   ? t('space.gfs_notice.connect_aria', { name: r.name })
                   : t('space.gfs_notice.connect_any_aria')}
                 data-testid="space-gfs-notice-connect">
                {t('space.gfs_notice.connect')}
              </a>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

export function SpaceGfsNotice({ space }: { space: GfsNoticeSpace }): JSX.Element | null {
  if (space.gfs_connect_needed === true) return <RequiredGfsNotice space={space} />
  return <OptionalGfsNotice space={space} />
}

function OptionalGfsNotice({ space }: { space: GfsNoticeSpace }): JSX.Element | null {
  const userId = currentUser.value?.user_id ?? null
  const isAdmin = currentUser.value?.is_admin === true
  const needed = space.stands_in_for_host === true
  // Hidden this visit, by space + GFS (the page may stay mounted from one
  // space to the next), so a row goes at once even with storage blocked.
  const [hidden, setHidden] = useState<string[]>([])
  const rootRef = useRef<HTMLDivElement | null>(null)
  const hiddenKey = (url: string) => `${space.id}\n${url}`
  const rows = missingGfsServers(space).filter(g => needed || (
    !hidden.includes(hiddenKey(g.inbox_url)) && !isGfsNoticeDismissed(userId, space.id, g.inbox_url)
  ))
  if (rows.length === 0) return null
  const dismiss = (url: string) => {
    // The ✕ removes its own row: land focus on the next row's ✕, else on
    // the page's main region, never on <body>.
    const buttons = [...(rootRef.current?.querySelectorAll<HTMLButtonElement>(
      '.sh-space-gfs-notice__dismiss') ?? [])]
    const at = buttons.findIndex(b => b.dataset.gfs === url)
    const next = buttons[at + 1] ?? buttons[at - 1] ?? null
    dismissGfsNotice(userId, space.id, url)
    setHidden(h => [...h, hiddenKey(url)])
    setTimeout(() => {
      const target = next?.isConnected ? next : document.getElementById('main')
      target?.focus()
    }, 0)
  }
  return (
    <div class="sh-space-gfs-notice" role="status" ref={rootRef} data-testid="space-gfs-notice">
      {rows.map(g => {
        const name = g.display_name || gfsHost(g.inbox_url)
        return (
          <div key={g.inbox_url}
               class={`sh-space-gfs-notice__row${needed ? ' sh-space-gfs-notice__row--needed' : ''}`}
               data-testid={needed ? 'space-gfs-notice-needed' : 'space-gfs-notice-optional'}>
            <span class="sh-space-gfs-notice__icon" aria-hidden="true">{needed ? '⚠️' : '🛰️'}</span>
            <div class="sh-space-gfs-notice__body">
              {isAdmin ? (
                <>
                  <p class="sh-space-gfs-notice__text">
                    {needed
                      ? t('space.gfs_notice.needed', { name })
                      : t('space.gfs_notice.optional', { name })}
                  </p>
                  <a class={`sh-btn sh-btn--${needed ? 'primary' : 'secondary'} sh-space-gfs-notice__connect`}
                     href={addBase(connectGfsPath(g.inbox_url))}
                     aria-label={t('space.gfs_notice.connect_aria', { name })}
                     data-testid="space-gfs-notice-connect">
                    {t('space.gfs_notice.connect')}
                  </a>
                </>
              ) : (
                // Connecting is a household admin's job: say whom to ask.
                <p class="sh-space-gfs-notice__text" data-testid="space-gfs-notice-ask-admin">
                  {needed
                    ? t('space.gfs_notice.needed_member', { name })
                    : t('space.gfs_listing.connect_hint_member', { name })}
                </p>
              )}
            </div>
            {!needed && (
              <button type="button" class="sh-space-gfs-notice__dismiss"
                      aria-label={t('space.gfs_notice.dismiss_aria', { name })}
                      title={t('common.close')}
                      data-testid="space-gfs-notice-dismiss"
                      data-gfs={g.inbox_url}
                      onClick={() => dismiss(g.inbox_url)}>
                ✕
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

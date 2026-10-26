/**
 * PublicSharingPanel — Settings › Privacy › Public sharing (decision 5;
 * ``/settings#public-sharing``). It used to be its own page
 * (``/momentum/public/sharing``, now a redirect), reachable only from the
 * pairing screen; Yours and the composer's public row link here.
 *
 * One row per active GFS of this household:
 *
 * * **Listed or not.** "Sign up" lists you on that GFS so people outside
 *   your paired households can follow you; "Leave" (asks first) takes you
 *   off it.
 * * **Make new moments public** — a switch per GFS you're listed on, off
 *   unless you chose it (decision 4, #1188): off, you switch public on per
 *   moment as you write it. Replies are never public.
 * * The v_62 notes: a server that can't carry encrypted moments gets
 *   none, and how many follower households can't read them yet.
 * * Copy profile link — your public page on that GFS.
 *
 * Instant save, like the panels beside it. A protected account can't share
 * publicly and sees why instead of rows.
 */
import { useEffect, useId } from 'preact/hooks'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { LoadErrorState } from '@/components/LoadErrorState'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { Switch } from '@/components/Switch'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import {
  deregisterFromGfs,
  gfsConnections,
  gfsConnectionsFailed,
  loadGfsConnections,
  loadRegistrations,
  registerOnGfs,
  registrations,
  setDefaultShare,
} from '@/store/momentPublic'
import type { GfsConnection, MomentPublicRegistration } from '@/types'

/** Public, guest-readable index of the caller's current public moments. */
function shareUrl(conn: GfsConnection): string | null {
  const uid = currentUser.value?.user_id
  if (!uid) return null
  return `${conn.inbox_url.replace(/\/+$/, '')}/moments/${encodeURIComponent(uid)}`
}

async function copyShareUrl(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    showToast(t('moment.sharing.link_copied'), 'success')
  } catch {
    showToast(url, 'info')
  }
}

const errText = (err: unknown) => String((err as Error)?.message ?? err)

function reload(force = false): void {
  void loadGfsConnections(force)
  void loadRegistrations()
}

export function PublicSharingPanel() {
  const restricted = isRestricted('public_moments')
  useEffect(() => {
    // Fresh per visit: pairing a GFS links straight here.
    if (!restricted) reload(true)
  }, [restricted])

  let body
  if (restricted) {
    body = <ProtectedNotice capability="public_moments" hideLink />
  } else if (gfsConnectionsFailed.value && gfsConnections.value === null) {
    body = (
      <LoadErrorState
        message={t('moment.discover.connections_failed')}
        detail={t('load.household_quiet')}
        retryLabel={t('load.try_again')}
        onRetry={() => reload(true)}
      />
    )
  } else if (gfsConnections.value === null) {
    body = <p class="sh-muted" role="status">{t('common.loading')}</p>
  } else if (gfsConnections.value.length === 0) {
    body = <p class="sh-muted">{t('moment.sharing.empty_body')}</p>
  } else {
    body = (
      <>
        <p class="sh-muted">{t('moment.sharing.intro')}</p>
        <ul class="sh-public-sharing-list">
          {gfsConnections.value.map((g) => (
            <GfsRow key={g.id} gfs={g}
              reg={registrations.value.find((r) => r.gfs_id === g.id) ?? null} />
          ))}
        </ul>
      </>
    )
  }

  return (
    <div id="public-sharing" class="sh-settings-subcard sh-public-sharing">
      <h3>{t('moment.sharing.title')}</h3>
      {body}
    </div>
  )
}

function GfsRow({ gfs, reg }: { gfs: GfsConnection; reg: MomentPublicRegistration | null }) {
  const hintId = useId()
  const switchId = useId()
  const unsupported = reg?.delivery === 'unsupported_server'
  const unable = reg?.followers_unable ?? 0
  const link = reg ? shareUrl(gfs) : null

  const onRegister = async () => {
    try {
      await registerOnGfs(gfs.id)
      showToast(t('moment.sharing.registered'), 'success')
    } catch (err) {
      showToast(t('moment.sharing.register_failed', { error: errText(err) }), 'error')
    }
  }
  const onLeave = async () => {
    const ok = await confirmDialog(t('moment.sharing.leave_body', { gfs: gfs.display_name }), {
      title: t('moment.sharing.leave_title', { gfs: gfs.display_name }),
      confirmLabel: t('moment.sharing.unregister'),
      destructive: true,
    })
    if (!ok) return
    try {
      await deregisterFromGfs(gfs.id)
      showToast(t('moment.sharing.unregistered'), 'success')
    } catch (err) {
      showToast(t('moment.sharing.unregister_failed', { error: errText(err) }), 'error')
    }
  }
  const onToggleDefault = async (next: boolean) => {
    try {
      await setDefaultShare(gfs.id, next)
    } catch (err) {
      showToast(t('moment.sharing.toggle_failed', { error: errText(err) }), 'error')
    }
  }

  return (
    <li class="sh-public-sharing-row">
      <div class="sh-public-sharing-head">
        <span class="sh-public-sharing-icon" aria-hidden="true"><IconGlobe /></span>
        <span class="sh-public-sharing-name">
          <strong><bdi dir="auto">{gfs.display_name}</bdi></strong>
          <span class="sh-muted">{reg ? t('moment.sharing.listed') : t('moment.sharing.not_listed')}</span>
        </span>
        {reg ? (
          <Button variant="secondary" onClick={() => void onLeave()}
            aria-label={t('moment.sharing.leave_aria', { gfs: gfs.display_name })}>
            {t('moment.sharing.unregister')}
          </Button>
        ) : (
          <Button onClick={() => void onRegister()}
            aria-label={t('moment.sharing.register_aria', { gfs: gfs.display_name })}>
            {t('moment.sharing.register')}
          </Button>
        )}
      </div>
      {reg && unsupported && (
        <p class="sh-public-sharing-warning" role="status">
          {t('moment.sharing.unsupported_server')}
        </p>
      )}
      {reg && !unsupported && unable > 0 && (
        <p class="sh-public-sharing-note sh-muted" role="status">
          {t('moment.sharing.followers_unable', { count: String(unable) })}
        </p>
      )}
      {reg && (
        <div class="sh-public-sharing-default">
          <span class="sh-public-sharing-default__text">
            <label for={switchId}><strong>{t('moment.sharing.default_on')}</strong></label>
            <span id={hintId} class="sh-muted">{t('moment.sharing.default_hint')}</span>
          </span>
          <Switch id={switchId} checked={reg.default_share}
            aria-describedby={hintId}
            onChange={(next) => void onToggleDefault(next)} />
        </div>
      )}
      {link && (
        <div class="sh-public-sharing-actions">
          <Button variant="secondary" onClick={() => void copyShareUrl(link)}
            title={t('moment.sharing.copy_link_title')}>
            {t('moment.sharing.copy_link')}
          </Button>
        </div>
      )}
    </li>
  )
}

function IconGlobe() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
      <circle cx="12" cy="12" r="10" /><path d="M2 12h20" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  )
}

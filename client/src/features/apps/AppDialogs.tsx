/**
 * The two Apps dialogs and the pieces they share with the pages.
 *
 * - **AddAppDialog** — the consent (D2, R3 §2.2): "Add Chess for
 *   everyone?", what it will be able to do in plain words, what it can
 *   never do, who can open it. The explicit button is the consent — an
 *   admin agrees for everyone in the household.
 * - **AskAdminDialog** — "Ask an admin for Chess?" (D4); a protected
 *   account asks its grown-ups instead. The request is the deciders'
 *   Needs-you row; nothing else is sent.
 * - **UpdateButton** / **UpdateConsentDialog** — one-tap update (D7); a
 *   version asking for more lists only the new permissions and waits for
 *   a yes.
 */
import { useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { isOne, locale, t } from '@/i18n/i18n'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { ApiError } from '@/api'
import { currentUser } from '@/store/auth'
import { safeIconSrc } from '@/utils/appIcon'
import {
  catalogSourceHost,
  householdHasProtectedMinor,
  installApp,
  loadUpdates,
  newPermissionsAsked,
  requestApp,
  updateApp,
  updates,
  type AppPermission,
  type AppUpdate,
  type CatalogEntry,
} from '@/store/apps'
import {
  PERMISSION_GLYPH,
  changelogNote,
  versionLabel,
  permissionDetail,
  permissionTitle,
  ratingLabel,
  sizeLabel,
  sourceLabel,
} from './appFacts'

/** An app's icon, or the placeholder (only ``data:`` icons are shown). */
export function AppIcon({ src, size = 'md' }: { src: string | null | undefined, size?: 'sm' | 'md' | 'lg' }) {
  const ok = safeIconSrc(src)
  return ok
    ? <img src={ok} alt="" aria-hidden="true" class={`sh-app-icon sh-app-icon--${size}`} />
    : <span class={`sh-app-icon sh-app-icon--${size} sh-app-icon--blank`} aria-hidden="true">📦</span>
}

/** The plain-word permission list: short lines, or with *name* full
 *  sentences under each. Nothing asked for → one line saying so. */
export function PermissionList({ perms, name, detailed = false }: {
  perms: AppPermission[]
  name: string
  detailed?: boolean
}) {
  if (perms.length === 0) {
    return <p class="sh-app-perms__none">{t('apps.perm.none', { name })}</p>
  }
  return (
    <ul class={`sh-app-perms${detailed ? ' sh-app-perms--detailed' : ''}`}>
      {perms.map(p => (
        <li key={p} class="sh-app-perms__item">
          <span class="sh-app-perms__glyph" aria-hidden="true">{PERMISSION_GLYPH[p]}</span>
          <span class="sh-app-perms__text">
            <span class="sh-app-perms__title">{permissionTitle(p)}</span>
            {detailed && <span class="sh-app-perms__detail">{permissionDetail(p, name)}</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** Who can open an app once it is added (the age rule, D5). */
export function whoCanOpen(minAge: number): string {
  return minAge > 0
    ? t('apps.consent.who_age', { age: String(minAge) })
    : t('apps.consent.who_everyone')
}

/** The error a refused install / request shows. */
function errorText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.code === 'APP_NEEDS_NEWER_SOCIAL_HOME') return t('apps.needs_newer')
    if (err.code === 'APP_CATALOG_DISABLED') return t('apps.catalog_disabled')
  }
  return fallback
}

export function AddAppDialog({ entry, name, permissions, open, onClose, onAdded }: {
  entry: CatalogEntry
  /** The app's name in the viewer's language. */
  name: string
  permissions: AppPermission[]
  open: boolean
  onClose: () => void
  onAdded?: () => void
}) {
  const [adding, setAdding] = useState(false)
  const minAge = entry.min_age ?? 0
  const size = sizeLabel(entry.bundle_size)

  const add = async () => {
    setAdding(true)
    try {
      await installApp(entry.app_id)
      onClose()
      onAdded?.()
    } catch (err: unknown) {
      showToast(errorText(err, t('apps.install_failed')), 'error')
    } finally {
      setAdding(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('apps.consent.title', { name })}
      class="sh-app-consent"
    >
      <div class="sh-app-consent__head">
        <AppIcon src={entry.icon_url} size="sm" />
        <p class="sh-app-meta">
          {sourceLabel(catalogSourceHost.value)}
          {size && <> · {size}</>}
        </p>
      </div>
      <p class="sh-app-consent__lead">{t('apps.consent.able', { name })}</p>
      <PermissionList perms={permissions} name={name} detailed />
      <p class="sh-app-never-line">{t('apps.never.line')}</p>
      <p class="sh-app-consent__who">
        {whoCanOpen(minAge)}
        {householdHasProtectedMinor.value && <> {t('apps.consent.protected')}</>}
      </p>
      <div class="sh-modal-actions">
        <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
        <Button loading={adding} onClick={() => { void add() }}>{t('apps.install')}</Button>
      </div>
    </Modal>
  )
}

export function AskAdminDialog({ entry, name, open, onClose }: {
  entry: CatalogEntry
  name: string
  open: boolean
  onClose: () => void
}) {
  const [sending, setSending] = useState(false)
  const grownUp = !!currentUser.value?.protected

  const send = async () => {
    setSending(true)
    try {
      await requestApp(entry.app_id)
      showToast(t('apps.ask.sent'), 'success')
      onClose()
    } catch (err: unknown) {
      showToast(errorText(err, t('apps.ask.failed')), 'error')
    } finally {
      setSending(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t(grownUp ? 'apps.ask.title_grown_up' : 'apps.ask.title', { name })}
    >
      <p class="sh-app-consent__lead">
        {t(grownUp ? 'apps.ask.body_grown_up' : 'apps.ask.body')}
      </p>
      <div class="sh-modal-actions">
        <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
        <Button loading={sending} onClick={() => { void send() }}>{t('apps.ask.send')}</Button>
      </div>
    </Modal>
  )
}

/** The member's button for a catalog app: "Ask an admin" (or "Ask a
 *  grown-up"), "Asked" once sent. */
export function AskButton({ entry, name, variant = 'secondary' }: {
  entry: CatalogEntry
  name: string
  variant?: 'primary' | 'secondary'
}) {
  const [open, setOpen] = useState(false)
  if (entry.requested) {
    return <span class="sh-chip sh-chip--muted sh-app-asked">{t('apps.ask.asked')}</span>
  }
  const grownUp = !!currentUser.value?.protected
  return (
    <>
      <Button
        variant={variant}
        onClick={() => setOpen(true)}
        aria-label={t(grownUp ? 'apps.ask.button_grown_up_named' : 'apps.ask.button_named', { name })}
      >
        {t(grownUp ? 'apps.ask.button_grown_up' : 'apps.ask.button')}
      </Button>
      <AskAdminDialog entry={entry} name={name} open={open} onClose={() => setOpen(false)} />
    </>
  )
}

/**
 * **Update** — one tap (D7). Same or fewer permissions: it updates at
 * once. When the new version asks for more — known ahead from the updates
 * list, or found by the server (``409 APP_NEW_PERMISSIONS``: it checks the
 * downloaded bundle too) — the consent dialog lists only the new ones and
 * nothing changes until the admin says yes. Used by the Updates section,
 * an app's detail page and the "Needs you" row.
 */
export function UpdateButton({ appId, name, update, variant = 'primary', describedBy, onDone }: {
  appId: string
  /** The app's name in the viewer's language. */
  name: string
  /** What the updates list says about it (looked up when not given). */
  update?: AppUpdate | null
  variant?: 'primary' | 'secondary'
  describedBy?: string
  onDone?: () => void
}) {
  const info = update ?? updates.value.find(u => u.app_id === appId) ?? null
  const known = info?.new_permissions ?? []
  const [busy, setBusy] = useState(false)
  const [asked, setAsked] = useState<AppPermission[] | null>(null)

  const run = async (accept: readonly AppPermission[]) => {
    setBusy(true)
    try {
      await updateApp(appId, accept)
      setAsked(null)
      showToast(t('apps.updated_named', { name }), 'success')
      onDone?.()
    } catch (err: unknown) {
      const more = newPermissionsAsked(err)
      if (more && more.length > 0) {
        // The server found new permissions: ask, with What's new if the
        // updates list wasn't loaded here yet.
        if (!info) void loadUpdates()
        setAsked(more)
      } else {
        showToast(errorText(err, t('apps.update_failed')), 'error')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button
        variant={variant}
        loading={busy && asked === null}
        aria-describedby={describedBy}
        aria-label={t(known.length > 0 ? 'apps.update_review_named' : 'apps.update_named', { name })}
        onClick={() => {
          if (known.length > 0) setAsked(known)
          else void run([])
        }}
      >
        {known.length > 0 ? t('apps.update_review') : t('apps.update')}
      </Button>
      <UpdateConsentDialog
        name={name}
        update={info}
        permissions={asked ?? []}
        open={asked !== null}
        busy={busy}
        onAccept={() => { void run(asked ?? []) }}
        onClose={() => setAsked(null)}
      />
    </>
  )
}

/** The consent for an update that asks for more (D7): What's new, then
 *  only the new permissions in plain words. "Not now" keeps the version
 *  the household has. */
export function UpdateConsentDialog({ name, update, permissions, open, busy, onAccept, onClose }: {
  name: string
  update: AppUpdate | null
  permissions: AppPermission[]
  open: boolean
  busy: boolean
  onAccept: () => void
  onClose: () => void
}) {
  const lang = locale.value || 'en'
  const notes = (update?.changelog ?? []).map(c => changelogNote(c, lang)).filter(Boolean)
  const n = permissions.length
  return (
    <Modal open={open} onClose={onClose} title={t('apps.update_consent_title', { name })} class="sh-app-consent">
      {update && (
        <p class="sh-app-meta">
          {versionLabel(update.current_version)} → {versionLabel(update.latest_version)}
        </p>
      )}
      {notes.length > 0 && (
        <>
          <h3 class="sh-app-consent__h">{t('apps.whats_new')}</h3>
          <ul class="sh-app-consent__news">
            {notes.map(note => <li key={note}>{note}</li>)}
          </ul>
        </>
      )}
      <p class="sh-app-consent__ask" role="note">
        <strong>{t(isOne(n) ? 'apps.update_asks_one' : 'apps.update_asks', { n: String(n) })}</strong>
        {' '}
        {update
          ? t('apps.update_keeps', { version: versionLabel(update.current_version) })
          : t('apps.update_keeps_plain')}
      </p>
      <PermissionList perms={permissions} name={name} detailed />
      <p class="sh-app-never-line">{t('apps.never.line')}</p>
      <div class="sh-modal-actions">
        <Button variant="secondary" onClick={onClose}>{t('common.not_now')}</Button>
        <Button loading={busy} onClick={onAccept}>{t('apps.update_accept')}</Button>
      </div>
    </Modal>
  )
}

/** The rating chip — only when it isn't "Everyone" (R3 §2.10). */
export function RatingChip({ minAge }: { minAge: number | null | undefined }) {
  if (!minAge || minAge <= 0) return null
  return (
    <span class="sh-age-chip" title={t('apps.min_age.title', { age: String(minAge) })}>
      {ratingLabel(minAge)}
    </span>
  )
}

/** Where a detail page lives (``/apps/<id>/about`` — ``/apps/<id>``
 *  launches the app). */
export function aboutPath(appId: string): string {
  return `/apps/${encodeURIComponent(appId)}/about`
}

/** Route to an app's detail page (the router keeps the ingress prefix). */
export function useOpenAbout(): (appId: string) => void {
  const { route } = useLocation()
  return (appId: string) => route(aboutPath(appId))
}

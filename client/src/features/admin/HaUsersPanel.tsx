/**
 * HaUsersPanel — admin UI to opt HA ``person.*`` entities into Social Home.
 *
 * Lists the HA users via ``GET /api/admin/ha-users`` and renders a per-row
 * toggle. Flipping on issues a ``POST`` to provision; flipping off issues
 * a ``DELETE``. The ``synced`` flag drives the toggle state; optimistic
 * updates flip it immediately and revert on failure.
 *
 * Only mounted when ``config.mode === 'ha'``; in standalone mode the
 * endpoint 501s and AdminPage hides this tab entirely.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { Spinner } from '@/components/Spinner'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { requiresHaUserPassword } from '@/platform'
import { t } from '@/i18n/i18n'

interface HaUser {
  username:     string
  /** Public ``@``-handle; falls back to ``username`` when unset. */
  handle?:      string | null
  display_name: string
  picture_url:  string | null
  is_admin:     boolean
  synced:       boolean
}

const users = signal<HaUser[]>([])
const loading = signal(true)
const error = signal<string | null>(null)
const notAvailable = signal(false)

/** Modal-backed password prompt — replaces ``window.prompt`` so the
 *  HA-mode provision flow keeps the warm Social Home chrome instead
 *  of a browser-native dialog. */
const passwordPromptOpen = signal<{ user: HaUser } | null>(null)

export function HaUsersPanel() {
  useEffect(() => {
    let cancelled = false
    loading.value = true
    error.value = null
    notAvailable.value = false
    api.get('/api/admin/ha-users')
      .then((data: HaUser[]) => {
        if (!cancelled) users.value = data
      })
      .catch((e: unknown) => {
        if (cancelled) return
        if ((e as { status?: number })?.status === 501) {
          notAvailable.value = true
        } else {
          error.value = (e as Error)?.message || t('ha_users.load_failed')
        }
      })
      .finally(() => {
        if (!cancelled) loading.value = false
      })
    return () => {
      cancelled = true
    }
  }, [])

  /** Run the actual provision / deprovision request after the user
   *  has either confirmed (haos / deprovision) or supplied a password
   *  via :class:`HaUserPasswordPromptModal` (ha mode provision). */
  const performToggle = async (
    row: HaUser,
    body?: { password?: string },
  ) => {
    const wasSynced = row.synced
    users.value = users.value.map(u =>
      u.username === row.username ? { ...u, synced: !wasSynced } : u,
    )
    try {
      if (wasSynced) {
        await api.delete(`/api/admin/ha-users/${row.username}/provision`)
        showToast(t('ha_users.removed', { name: row.display_name }), 'info')
      } else {
        await api.post(
          `/api/admin/ha-users/${row.username}/provision`,
          body,
        )
        showToast(t('ha_users.added', { name: row.display_name }), 'success')
      }
    } catch (e: unknown) {
      users.value = users.value.map(u =>
        u.username === row.username ? { ...u, synced: wasSynced } : u,
      )
      showToast((e as Error)?.message || t('ha_users.toggle_failed'), 'error')
    }
  }

  const toggle = (row: HaUser) => {
    // ha mode: server requires a password on provision so the picked
    // HA person can sign in via /api/auth/token.  haos mode: ingress
    // signs them in, so no password.  Deprovision (wasSynced=true) is
    // password-less in both modes.
    if (!row.synced && requiresHaUserPassword()) {
      passwordPromptOpen.value = { user: row }
      return
    }
    void performToggle(row)
  }

  if (loading.value) return <Spinner />
  if (notAvailable.value) {
    return (
      <section class="sh-admin-section">
        <h2>{t('ha_users.heading')}</h2>
        <p class="sh-muted">{t('ha_users.not_available')}</p>
      </section>
    )
  }
  if (error.value) {
    return (
      <section class="sh-admin-section" role="alert">
        <h2>{t('ha_users.heading')}</h2>
        <p class="sh-error">{error.value}</p>
      </section>
    )
  }

  if (users.value.length === 0) {
    return (
      <section class="sh-admin-section">
        <h2>{t('ha_users.heading')}</h2>
        <p class="sh-muted">{t('ha_users.empty')}</p>
      </section>
    )
  }

  return (
    <section class="sh-admin-section">
      <h2>{t('ha_users.heading')}</h2>
      <p class="sh-muted">{t('ha_users.intro')}</p>
      <ul class="sh-ha-users">
        {users.value.map(u => (
          <li key={u.username} class="sh-ha-user-row">
            <Avatar name={u.display_name} src={u.picture_url} size={36} />
            <div class="sh-ha-user-info">
              <span class="sh-ha-user-name">{u.display_name}</span>
              <span class="sh-muted">@{u.handle ?? u.username}</span>
              {u.is_admin && (
                <span class="sh-badge sh-badge--admin">{t('admin.members.admin')}</span>
              )}
            </div>
            <label class="sh-switch">
              <input
                type="checkbox"
                checked={u.synced}
                aria-label={t('ha_users.sync_aria', { name: u.display_name })}
                onChange={() => toggle(u)}
              />
              <span class="sh-switch-track" />
              <span class="sh-muted">
                {u.synced ? t('ha_users.active') : t('ha_users.not_added')}
              </span>
            </label>
          </li>
        ))}
      </ul>

      <HaUserPasswordPromptModal
        state={passwordPromptOpen.value}
        onClose={() => { passwordPromptOpen.value = null }}
        onSubmit={(user, password) => {
          passwordPromptOpen.value = null
          void performToggle(user, { password })
        }}
      />
    </section>
  )
}

/** Replaces ``window.prompt`` for the HA-mode provision password
 *  step.  Validates the 8-char minimum inline so the modal can show
 *  the rule before the user submits, and preserves the modal until
 *  the user either submits a valid password or cancels. */
function HaUserPasswordPromptModal({
  state, onClose, onSubmit,
}: {
  state: { user: HaUser } | null
  onClose: () => void
  onSubmit: (user: HaUser, password: string) => void
}) {
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')

  // Reset the inputs every time a new user is targeted so a typo
  // from the previous attempt doesn't carry over.
  useEffect(() => {
    setPw('')
    setPw2('')
  }, [state?.user.username])

  if (state === null) return null
  const tooShort = pw.length > 0 && pw.length < 8
  const mismatch = pw2.length > 0 && pw !== pw2
  const canSubmit = pw.length >= 8 && pw === pw2
  const submit = () => {
    if (!canSubmit) return
    onSubmit(state.user, pw)
  }
  return (
    <Modal open={true} onClose={onClose} title={t('ha_users.pw_title')}>
      <div class="sh-form">
        <p class="sh-muted" style={{ marginTop: 0 }}>
          {t('ha_users.pw_intro', { name: state.user.display_name })}
        </p>
        <label>
          {t('ha_users.pw_new')}
          <input
            type="password"
            autoComplete="new-password"
            value={pw}
            onInput={(e) => setPw((e.target as HTMLInputElement).value)}
            aria-invalid={tooShort ? true : undefined}
          />
          {tooShort && (
            <span class="sh-form-hint sh-form-hint--error">
              {t('ha_users.pw_too_short')}
            </span>
          )}
        </label>
        <label>
          {t('ha_users.pw_confirm')}
          <input
            type="password"
            autoComplete="new-password"
            value={pw2}
            onInput={(e) => setPw2((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
            aria-invalid={mismatch ? true : undefined}
          />
          {mismatch && (
            <span class="sh-form-hint sh-form-hint--error">
              {t('ha_users.pw_mismatch')}
            </span>
          )}
        </label>
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={!canSubmit}>
            {t('ha_users.add', { name: state.user.display_name })}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

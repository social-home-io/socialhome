import { useSignal } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'
import { t } from '@/i18n/i18n'

const ERROR_ID = 'sh-username-error'

/**
 * UsernameEditor — change-your-username control for the Settings → Profile
 * tab. Mirrors the display-name editor's shape (labelled field, ``api`` call,
 * inline error + toast success) but POSTs the dedicated
 * ``me/username`` endpoint so the server is the sole format/uniqueness
 * validator.
 *
 * HA-source gating: a user provisioned from Home Assistant (``source ===
 * 'ha'``) can't rename here — HA owns the identity. Those users see the
 * username read-only with a short managed-by-HA note and no Save action, so
 * the field reads as intentional rather than broken. Only ``source ===
 * 'manual'`` (the default for locally-provisioned accounts) gets the editable
 * field.
 *
 * Server contract (this branch): ``POST /api/me/username {username}`` →
 *   200 ``{username}`` on success;
 *   422 ``INVALID_USERNAME`` (taken / bad format / reserved) — surfaced in
 *       the ``FormError`` via the backend's friendly ``detail``;
 *   403 ``HA_CONTROLLED`` — shouldn't reach us because the field is hidden
 *       for HA users, but handled gracefully as an inline error if it does.
 */
export function UsernameEditor() {
  const user = currentUser.value
  const isHaUser = user?.source === 'ha'
  const currentUsername = user?.username ?? ''

  // Local edit buffer + inline error/saving state, seeded from the live
  // username. ``useSignal`` (not a module-level signal) so the buffer is
  // per-mount and resets cleanly between Settings visits.
  const value = useSignal(currentUsername)
  const error = useSignal<string | null>(null)
  const saving = useSignal(false)

  // HA-sourced users: read-only display + a managed-by-HA note. No input,
  // no Save — the rename path is owned by Home Assistant.
  if (isHaUser) {
    return (
      <div class="sh-username-editor">
        <h3>{t('username.title')}</h3>
        <p class="sh-profile-name" style={{ margin: 0 }}>{currentUsername}</p>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          {t('username.ha_hint')}
        </p>
      </div>
    )
  }

  const trimmed = value.value.trim()
  const canSave =
    !saving.value && trimmed.length > 0 && trimmed !== currentUsername

  const handleSave = async (e: Event) => {
    e.preventDefault()
    if (!canSave) return
    error.value = null
    saving.value = true
    try {
      const res = await api.post('/api/me/username', { username: trimmed }) as
        { username: string }
      const next = res.username ?? trimmed
      value.value = next
      // Mirror the new username onto the auth store so the profile card,
      // sidenav, and any other surface built from ``currentUser`` pick it
      // up immediately without a fresh ``/api/me``.
      if (currentUser.value) {
        currentUser.value = { ...currentUser.value, username: next } as User
      }
      showToast(t('username.updated'), 'success')
    } catch (err: unknown) {
      // 422 (taken / format / reserved) and the 403 fallback both carry a
      // friendly ``detail`` on the ApiError — show it inline. A non-ApiError
      // (network) falls back to its message.
      if (err instanceof ApiError) {
        error.value =
          err.message || t('username.change_failed')
      } else {
        error.value = (err as Error).message || t('username.change_failed')
      }
    } finally {
      saving.value = false
    }
  }

  return (
    <form class="sh-username-editor sh-form" onSubmit={handleSave}>
      <h3>{t('username.title')}</h3>
      <label>
        {t('username.label')}
        <input
          value={value.value}
          maxLength={32}
          autocomplete="off"
          autocapitalize="off"
          spellcheck={false}
          aria-describedby={error.value ? ERROR_ID : undefined}
          aria-invalid={error.value ? true : undefined}
          onInput={(e) => {
            value.value = (e.target as HTMLInputElement).value
          }}
        />
      </label>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        {t('username.hint')}
      </p>
      <FormError id={ERROR_ID} message={error.value} />
      <div class="sh-form-actions">
        <Button type="submit" disabled={!canSave} loading={saving.value}>
          {t('common.save')}
        </Button>
      </div>
    </form>
  )
}

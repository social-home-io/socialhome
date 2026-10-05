import { useSignal } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'
import { t } from '@/i18n/i18n'

const ERROR_ID = 'sh-handle-error'

/**
 * HandleEditor — change-your-public-``@handle`` control for the Settings →
 * Profile tab. Mirrors the {@link UsernameEditor} shape (labelled field,
 * ``api`` call, inline error + toast success) but POSTs the dedicated
 * ``/api/me/handle`` endpoint so the server is the sole format/uniqueness
 * validator.
 *
 * Unlike the username editor there is NO HA-source read-only gate: the
 * public ``@handle`` is editable by ALL users (HA-provisioned accounts
 * included). The username is the login identifier (HA owns it); the handle is
 * the public ``@``-name and belongs to the user.
 *
 * Server contract (this branch): ``POST /api/me/handle {handle}`` →
 *   200 ``{handle}`` on success;
 *   422 ``INVALID_HANDLE`` (taken / bad format / reserved) — surfaced in the
 *       ``FormError`` via the backend's friendly ``detail``.
 */
export function HandleEditor() {
  const user = currentUser.value
  // Seed from the handle, falling back to the username when the handle is
  // still null (legacy/edge rows provisioned before handle-seeding). A
  // null-handle user then sees their username pre-filled and can save it,
  // rather than an empty field. The "unchanged" disable logic is anchored on
  // this same seeded value so Save stays off until the user actually edits.
  const currentHandle = user?.handle ?? user?.username ?? ''

  // Local edit buffer + inline error/saving state, seeded from the live
  // handle. ``useSignal`` (not a module-level signal) so the buffer is
  // per-mount and resets cleanly between Settings visits.
  const value = useSignal(currentHandle)
  const error = useSignal<string | null>(null)
  const saving = useSignal(false)

  const trimmed = value.value.trim()
  const canSave =
    !saving.value && trimmed.length > 0 && trimmed !== currentHandle

  const handleSave = async (e: Event) => {
    e.preventDefault()
    if (!canSave) return
    error.value = null
    saving.value = true
    try {
      const res = await api.post('/api/me/handle', { handle: trimmed }) as
        { handle: string }
      const next = res.handle ?? trimmed
      value.value = next
      // Mirror the new handle onto the auth store so the profile card,
      // sidenav, and any other surface built from ``currentUser`` pick it
      // up immediately without a fresh ``/api/me``.
      if (currentUser.value) {
        currentUser.value = { ...currentUser.value, handle: next } as User
      }
      showToast(t('handle.updated'), 'success')
    } catch (err: unknown) {
      // 422 (taken / format / reserved) carries a friendly ``detail`` on the
      // ApiError — show it inline. A non-ApiError (network) falls back to its
      // message.
      if (err instanceof ApiError) {
        error.value =
          err.message || t('handle.change_failed')
      } else {
        error.value = (err as Error).message || t('handle.change_failed')
      }
    } finally {
      saving.value = false
    }
  }

  return (
    <form class="sh-handle-editor sh-form" onSubmit={handleSave}>
      <h3>{t('handle.title')}</h3>
      <label>
        @handle
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
        {t('handle.hint')}
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

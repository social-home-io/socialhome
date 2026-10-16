import type { User } from '@/types'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'

export const HANDLE_ERROR_ID = 'sh-handle-error'
export const HANDLE_INPUT_ID = 'sh-profile-handle'
const HANDLE_HINT_ID = 'sh-profile-handle-hint'

/** The handle the field starts from: the public ``@handle``, else the
 *  username for legacy rows provisioned before handle seeding (so the
 *  field is never empty and saving it sets the handle). */
export function handleBaseline(
  user: Pick<User, 'handle' | 'username'> | null | undefined,
): string {
  return user?.handle ?? user?.username ?? ''
}

/**
 * HandleEditor — the public ``@handle`` field of Settings › Profile's one
 * form (decision 7). Controlled: the profile form owns the value, works
 * out whether it changed, and saves it through ``POST /api/me/handle``
 * (the server is the sole format / uniqueness validator; its 422 comes
 * back here as *error*).
 *
 * Unlike the sign-in username there is NO HA-source read-only gate: the
 * public ``@handle`` belongs to the user, HA-provisioned accounts
 * included.
 */
export function HandleEditor({ value, onInput, error, disabled }: {
  value: string
  onInput: (next: string) => void
  error: string | null | undefined
  disabled?: boolean
}) {
  return (
    <div class="sh-profile-field">
      <label class="sh-profile-field__label" for={HANDLE_INPUT_ID}>
        {t('handle.title')}
      </label>
      <div class="sh-profile-handle">
        <span class="sh-profile-handle__at" aria-hidden="true">@</span>
        <input
          id={HANDLE_INPUT_ID}
          value={value}
          maxLength={32}
          autocomplete="off"
          autocapitalize="off"
          spellcheck={false}
          disabled={disabled}
          aria-describedby={error ? `${HANDLE_ERROR_ID} ${HANDLE_HINT_ID}` : HANDLE_HINT_ID}
          aria-invalid={error ? true : undefined}
          onInput={(e) => onInput((e.target as HTMLInputElement).value)}
        />
      </div>
      <p id={HANDLE_HINT_ID} class="sh-profile-field__hint">{t('handle.hint')}</p>
      <FormError id={HANDLE_ERROR_ID} message={error} />
    </div>
  )
}

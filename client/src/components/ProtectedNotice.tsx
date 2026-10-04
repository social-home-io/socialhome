/**
 * ProtectedNotice — explains a surface a protected account can't use.
 *
 * A household admin can put an account under child protection. The
 * server then refuses a fixed set of surfaces for it with
 * ``403 ACCOUNT_PROTECTED`` (see docs/api.md → *Protected accounts*) and
 * reports them on ``/api/me`` as ``restrictions``. The SPA reads that list
 * — never an age or minor flag, which the server doesn't send — to swap
 * the entry point for this notice instead of letting the click fail.
 *
 * The server stays the authority: hiding a button here is a courtesy,
 * the 403 is the enforcement.
 */
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import { addBase } from '@/baseUrl'

/** Wire ids from ``/api/me.restrictions`` (``ProtectedCapability``). */
export type ProtectedCapability =
  | 'bazaar'
  | 'public_spaces'
  | 'public_moments'
  | 'public_links'
  | 'api_tokens'
  | 'calendar_feeds'

/** Whether the signed-in account may not use *capability*. Reactive —
 *  reads the ``currentUser`` signal, so a render that calls it updates
 *  when ``/api/me`` reloads. */
export function isRestricted(capability: ProtectedCapability): boolean {
  return currentUser.value?.restrictions?.includes(capability) ?? false
}

/** One-line description of a restricted capability. */
export function restrictionCopy(capability: string): string {
  return t(`protected.cap.${capability}`)
}

interface Props {
  capability: ProtectedCapability
  /** Drop the "see what's limited" link (e.g. inside Settings itself). */
  hideLink?: boolean
}

export function ProtectedNotice({ capability, hideLink = false }: Props) {
  return (
    <div class="sh-protected-notice" role="note" data-capability={capability}>
      <span class="sh-protected-notice__icon" aria-hidden="true">🛡️</span>
      <div class="sh-protected-notice__text">
        <strong>{t('protected.notice_title')}</strong>
        <p>
          {restrictionCopy(capability)} {t('protected.ask_guardian')}
        </p>
        {!hideLink && (
          <a class="sh-link" href={addBase('/settings#protection')}>
            {t('protected.see_what')}
          </a>
        )}
      </div>
    </div>
  )
}

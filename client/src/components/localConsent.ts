/**
 * Asking before a join carries this household's approximate location.
 *
 * A join to a local community another household hosts carries our
 * household's home rounded to ~1 km — sent to that household only, with
 * the request, and only once the person agreed (owner decision
 * 2026-10-19). Every join surface asks through here: Discover / About
 * (before the request), the invite-link dialog and an invitation's Accept
 * (when the host answers ``LOCATION_REQUIRED``), and then sends the join
 * again with ``share_location: true``.
 */
import { confirmDialog } from './confirm'
import { t } from '@/i18n/i18n'
import { ensureHouseholdHome, householdHome } from '@/store/householdHome'

/** The consent sentence, as shown in a dialog or beside a request —
 *  ``host`` ``null`` when the household's name is not known here (an
 *  invite link from a household we never met). */
export function locationConsentText(host: string | null): string {
  return host ? t('local_area.consent.body', { host }) : t('local_area.consent.body_generic')
}

/**
 * Ask the person whether their household's approximate location may go to
 * ``host`` with this join. ``false`` on Cancel — and when the household
 * has no home set: then the dialog explains where it comes from instead,
 * and nothing is sent.
 */
export async function askLocationConsent(host: string | null): Promise<boolean> {
  await ensureHouseholdHome()
  if (householdHome.value == null) {
    await confirmDialog(t('local_area.consent.no_home'), {
      title: t('local_area.consent.title'),
      confirmLabel: t('common.ok'),
    })
    return false
  }
  return confirmDialog(locationConsentText(host), {
    title: t('local_area.consent.title'),
    confirmLabel: t('local_area.consent.send'),
  })
}

/** Is ``err`` the API answer with ``code`` (an ``ApiError``)? */
export function isApiCode(err: unknown, code: string): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: unknown }).code === code
}

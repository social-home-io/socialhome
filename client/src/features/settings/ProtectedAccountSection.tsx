/**
 * ProtectedAccountSection — Settings › Profile, shown only to a protected
 * account: a compact banner ("Your account is protected · Melanie and
 * Pascal look after it") with "See what's limited" as a disclosure
 * (decision 10). The tall list it replaced pushed the profile 1.5 phone
 * screens down (P10).
 *
 * Backed by ``GET /api/me/protection`` (``{protected, restrictions,
 * guardians}``) through ``store/protection``, which also reloads it when the
 * server says this account's protection changed. The server never sends the
 * recorded age, so neither does this section — it explains *what* is
 * limited, not *why*. ``/settings#protection`` (the link on every
 * ``ProtectedNotice``) opens the disclosure.
 */
import { useEffect, useState } from 'preact/hooks'
import { restrictionCopy } from '@/components/ProtectedNotice'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { currentUser } from '@/store/auth'
import { loadMyProtection, myProtection, myProtectionFailed } from '@/store/protection'
import { formatLocale, t } from '@/i18n/i18n'

export type { MyProtection, ProtectionGuardian } from '@/store/protection'

const DETAILS_ID = 'sh-protected-details'

/** "Melanie", "Melanie and Pascal", "A, B and C" in the UI language. */
function joinNames(names: string[]): string {
  try {
    return new Intl.ListFormat(formatLocale(), { type: 'conjunction' }).format(names)
  } catch {
    return names.join(', ')
  }
}

export function ProtectedAccountSection() {
  const isProtected = currentUser.value?.protected === true
  const data = myProtection.value
  const failed = myProtectionFailed.value
  const [open, setOpen] = useState(
    () => typeof window !== 'undefined' && window.location.hash === '#protection',
  )

  useEffect(() => {
    if (isProtected) void loadMyProtection()
  }, [isProtected])

  if (!isProtected) return null

  // The list itself is already on /api/me, so it renders at once; only the
  // guardians wait for the dedicated endpoint.
  const restrictions = data?.restrictions ?? currentUser.value?.restrictions ?? []
  const names = (data?.guardians ?? []).map(g => g.display_name || g.username)

  let who: string
  if (data && names.length === 1) who = t('protected.banner.guardian_one', { name: names[0] })
  else if (data && names.length > 1) who = t('protected.banner.guardians', { names: joinNames(names) })
  else if (data) who = t('protected.section.no_guardians')
  // Still loading, or the lookup failed (the details say so).
  else who = t('protected.banner.household')

  return (
    <section
      id="protection"
      class="sh-protected-banner"
      aria-labelledby="sh-protected-account-heading"
    >
      <span class="sh-protected-banner__icon" aria-hidden="true">
        <ConnIcon name="shield" />
      </span>
      <div class="sh-protected-banner__body">
        <h2 id="sh-protected-account-heading" class="sh-protected-banner__title">
          {t('protected.section.title')}
        </h2>
        <p class="sh-protected-banner__who" data-testid="protected-guardians">{who}</p>
        <button
          type="button"
          class="sh-protected-banner__toggle"
          aria-expanded={open}
          aria-controls={DETAILS_ID}
          onClick={() => setOpen(o => !o)}
        >
          {open ? t('protected.banner.hide') : t('protected.see_what')}
        </button>
        <div id={DETAILS_ID} class="sh-protected-banner__details" hidden={!open}>
          <p>{t('protected.section.intro')}</p>
          <ul class="sh-protected-account__list">
            {restrictions.map((c) => <li key={c}>{restrictionCopy(c)}</li>)}
          </ul>
          {failed && !data
            ? <p>{t('protected.section.load_error')}</p>
            : names.length > 0 && <p>{t('protected.section.guardians_help')}</p>}
        </div>
      </div>
    </section>
  )
}

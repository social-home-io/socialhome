/**
 * ProtectedAccountSection — Settings → Profile, shown only to a protected
 * account: what the household limited and who the guardians are.
 *
 * Backed by ``GET /api/me/protection`` (``{protected, restrictions,
 * guardians}``) through ``store/protection``, which also reloads it when the
 * server says this account's protection changed. The server never sends the
 * recorded age, so neither does this section — it explains *what* is
 * limited, not *why*.
 */
import { useEffect } from 'preact/hooks'
import { Spinner } from '@/components/Spinner'
import { restrictionCopy } from '@/components/ProtectedNotice'
import { currentUser } from '@/store/auth'
import { loadMyProtection, myProtection, myProtectionFailed } from '@/store/protection'
import { t } from '@/i18n/i18n'

export type { MyProtection, ProtectionGuardian } from '@/store/protection'

export function ProtectedAccountSection() {
  const isProtected = currentUser.value?.protected === true
  const data = myProtection
  const failed = myProtectionFailed

  useEffect(() => {
    if (isProtected) void loadMyProtection()
  }, [isProtected])

  if (!isProtected) return null

  // The list itself is already on /api/me, so it renders at once; only the
  // guardians wait for the dedicated endpoint.
  const restrictions = data.value?.restrictions ?? currentUser.value?.restrictions ?? []

  return (
    <section
      id="protection"
      class="sh-settings-section sh-protected-account"
      aria-labelledby="sh-protected-account-heading"
    >
      <h2 id="sh-protected-account-heading">
        <span aria-hidden="true">🛡️ </span>{t('protected.section.title')}
      </h2>
      <p class="sh-muted">{t('protected.section.intro')}</p>
      <ul class="sh-protected-account__list">
        {restrictions.map((c) => <li key={c}>{restrictionCopy(c)}</li>)}
      </ul>

      <h3 class="sh-protected-account__guardians-heading">
        {t('protected.section.guardians')}
      </h3>
      {data.value === null && !failed.value && (
        <Spinner label={t('common.loading')} />
      )}
      {failed.value && data.value === null && (
        <p class="sh-muted">{t('protected.section.load_error')}</p>
      )}
      {data.value && data.value.guardians.length === 0 && (
        <p class="sh-muted">{t('protected.section.no_guardians')}</p>
      )}
      {data.value && data.value.guardians.length > 0 && (
        <>
          <ul class="sh-protected-account__guardians">
            {data.value.guardians.map((g) => (
              <li key={g.user_id}>
                <strong>{g.display_name || g.username}</strong>
                {g.display_name && g.display_name !== g.username && (
                  <span class="sh-muted"> @{g.username}</span>
                )}
              </li>
            ))}
          </ul>
          <p class="sh-muted">{t('protected.section.guardians_help')}</p>
        </>
      )}
    </section>
  )
}

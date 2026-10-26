/**
 * PublicSharingRow — the "Public sharing" row at the top of Momentum ›
 * Yours (decision 5). One line on where you stand (listed on which GFS,
 * whether new moments go public) and a link to Settings › Privacy ›
 * Public sharing, where it is changed. Hidden without an active GFS and
 * for a protected account (it can't share publicly).
 */
import { useEffect } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { isRestricted } from '@/components/ProtectedNotice'
import { t } from '@/i18n/i18n'
import {
  gfsConnections,
  loadGfsConnections,
  loadRegistrations,
  registrations,
} from '@/store/momentPublic'
import { PUBLIC_SHARING_PATH } from './PublicSharingRedirect'

export function PublicSharingRow() {
  const restricted = isRestricted('public_moments')
  useEffect(() => {
    if (restricted) return
    void loadGfsConnections()
    void loadRegistrations()
  }, [restricted])

  const conns = gfsConnections.value
  if (restricted || !conns || conns.length === 0) return null

  const active = new Set(conns.map(g => g.id))
  const regs = registrations.value.filter(r => active.has(r.gfs_id))
  const listedOn = regs.length === 1
    ? conns.find(g => g.id === regs[0].gfs_id)?.display_name ?? null
    : null
  const title = listedOn
    ? t('moment.yours.sharing_title_on', { gfs: listedOn })
    : t('moment.sharing.title')
  const sub = regs.length === 0
    ? t('moment.yours.sharing_not_listed')
    : regs.some(r => r.default_share)
      ? t('moment.yours.sharing_default_on')
      : t('moment.yours.sharing_default_off')

  return (
    <a class="sh-momentum-sharing-row" href={addBase(PUBLIC_SHARING_PATH)}>
      <span class="sh-momentum-sharing-row__icon" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
          <circle cx="12" cy="12" r="10" /><path d="M2 12h20" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
      </span>
      <span class="sh-momentum-sharing-row__text">
        <strong>{title}</strong>
        <span class="sh-muted">{sub}</span>
      </span>
      <span class="sh-momentum-sharing-row__chev sh-flip-rtl" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
          <path d="m9 18 6-6-6-6" />
        </svg>
      </span>
    </a>
  )
}

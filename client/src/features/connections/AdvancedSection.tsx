/**
 * AdvancedSection — admin-only, last on the page: External address (when
 * a manual one is honoured), Network relay (STUN/TURN) and Diagnostics,
 * as disclosures in one card. Shown in every mode — the relay list and
 * the diagnostics file matter most under the Home Assistant App.
 */
import { t } from '@/i18n/i18n'
import { DiagnosticsRow } from './DiagnosticsRow'
import { ExternalAddressRow } from './ExternalAddressRow'
import { NetworkRelayRow } from './NetworkRelayRow'
import { SectionHeader } from './SectionHeader'

export function AdvancedSection({ showExternalAddress }: { showExternalAddress: boolean }) {
  return (
    <section class="sh-conn-sect" aria-labelledby="sh-conn-adv-title">
      <SectionHeader id="sh-conn-adv-title" title={t('connections.adv.title')} desc={t('connections.adv.desc')} />
      <div class="sh-conn-adv">
        {showExternalAddress && <ExternalAddressRow />}
        <NetworkRelayRow />
        <DiagnosticsRow />
      </div>
    </section>
  )
}

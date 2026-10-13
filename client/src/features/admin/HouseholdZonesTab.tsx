/**
 * HouseholdZonesTab — Admin › Zones: the household's own zones
 * (``/api/household/zones``) in the shared :component:`ZonesAdmin`
 * editor, with the Home zone and the zones synced from Home Assistant.
 *
 * The zones belong to the household's presence feature; with it off the
 * API answers 403 ``FEATURE_DISABLED`` and the tab points to the toggle
 * under Settings. On a Home Assistant household a note says HA reports
 * who is in which zone, and — when the platform has zones of its own —
 * the "Sync zones from Home Assistant" switch sits above the list.
 */
import { useState } from 'preact/hooks'

import { Button } from '@/components/Button'
import { ZonesAdmin } from '@/components/ZonesAdmin'
import { t } from '@/i18n/i18n'
import { supportsZoneSync, usesHaUserDirectory } from '@/platform'
import { HouseholdZoneSyncPanel } from './HouseholdZoneSyncPanel'

export function HouseholdZonesTab({ onOpenSettings }: { onOpenSettings: () => void }) {
  // Bumped after the sync switch changed — the list refetches quietly.
  const [reloadSignal, setReloadSignal] = useState(0)
  return (
    <section class="sh-admin-section">
      <ZonesAdmin
        basePath="/api/household/zones"
        title={t('zones.household.title')}
        intro={
          // On Home Assistant, HA reports each person's zone; native zones
          // only count for clients that post a location without one.
          usesHaUserDirectory()
            ? `${t('zones.household.intro')} ${t('zones.household.ha_note')}`
            : t('zones.household.intro')
        }
        toolbar={
          supportsZoneSync()
            ? <HouseholdZoneSyncPanel onChanged={() => setReloadSignal((n) => n + 1)} />
            : undefined
        }
        reloadSignal={reloadSignal}
        emptyText={t('zones.household.empty')}
        refetchOn="household_zones_changed"
        household
        featureDisabled={
          <div class="sh-zones-admin__off-body">
            <p class="sh-muted">{t('zones.household.feature_off')}</p>
            <Button variant="secondary" onClick={onOpenSettings}>
              {t('zones.household.open_settings')}
            </Button>
          </div>
        }
      />
    </section>
  )
}

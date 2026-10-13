/**
 * HouseholdZonesTab — Admin › Zones: the household's own zones
 * (``/api/household/zones``) in the shared :component:`ZonesAdmin`
 * editor, with the Home zone and the zones synced from Home Assistant.
 *
 * The zones belong to the household's presence feature; with it off the
 * API answers 403 ``FEATURE_DISABLED`` and the tab points to the toggle
 * under Settings.
 */
import { Button } from '@/components/Button'
import { ZonesAdmin } from '@/components/ZonesAdmin'
import { t } from '@/i18n/i18n'

export function HouseholdZonesTab({ onOpenSettings }: { onOpenSettings: () => void }) {
  return (
    <section class="sh-admin-section">
      <ZonesAdmin
        basePath="/api/household/zones"
        title={t('zones.household.title')}
        intro={t('zones.household.intro')}
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

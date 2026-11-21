/**
 * SpaceSyncChip — a small chip under a space's top bar, shown ONLY when
 * the space is not up to date here (catching up, behind, host offline,
 * not synced yet) or our own items still wait to go out. Up to date with
 * nothing waiting → nothing at all. It links to the space settings' Sync
 * section (the state in full, and "Sync now").
 */
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { settingsSectionPath } from './spaceSettingsSections'
import { syncChipLabel, useSpaceSync } from './spaceSync'

export function SpaceSyncChip({ spaceId, sync }: { spaceId: string; sync: unknown }) {
  const live = useSpaceSync(spaceId, sync)
  const label = syncChipLabel(live)
  if (!live || !label) return null
  return (
    <div class="sh-sync-chip-row" role="status">
      <a class={`sh-sync-chip sh-sync-chip--${live.state}`}
         href={addBase(settingsSectionPath(spaceId, 'sync'))}
         aria-label={t('space.sync.chip_aria', { status: label })}
         data-testid="space-sync-chip">
        <span class="sh-sync-chip__dot" aria-hidden="true" />
        <span class="sh-sync-chip__text">{label}</span>
      </a>
    </div>
  )
}

/**
 * SpaceSyncChip — one small chip under a space's top bar, only when it
 * matters (owner decisions 2026-11-21/22):
 *
 *   * **Some posts may be missing ›** — the one warning (``may_be_missing``),
 *     links to the settings' "Is everything here?";
 *   * **Getting the space ready…** — first join, nothing arrived yet;
 *   * **Getting the latest…** — muted, only once a stream has been
 *     arriving for 10 s (no flicker for a normal round, nothing when it
 *     ends).
 *
 * An away host and a single failed round show nothing here (settings
 * only). Every chip has an icon and words, never colour alone. Screen
 * readers hear only a change INTO the warning (``role="status"``).
 */
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { settingsSectionPath } from './spaceSettingsSections'
import {
  CATCHING_UP_CHIP_DELAY_MS, SYNC_ICON, syncChipKind, syncChipLabel, useHeldFor,
  useMissingAnnouncement, useSpaceSync,
} from './spaceSync'

const ICON = {
  missing: SYNC_ICON.missing,
  preparing: SYNC_ICON.preparing,
  latest: SYNC_ICON.latest,
} as const

export function SpaceSyncChip({ spaceId, sync }: { spaceId: string; sync: unknown }) {
  const live = useSpaceSync(spaceId, sync)
  const slow = useHeldFor(live?.state === 'catching_up', CATCHING_UP_CHIP_DELAY_MS)
  const kind = syncChipKind(live, slow)
  const announce = useMissingAnnouncement(kind === 'missing')
  if (!live) return null
  const label = kind ? syncChipLabel(kind) : ''
  return (
    <div class={kind ? 'sh-sync-chip-row' : undefined}>
      {/* Always mounted, so a change INTO the warning is announced. */}
      <span class="sr-only" role="status">{announce}</span>
      {kind && (
        <a class={`sh-sync-chip sh-sync-chip--${kind}`}
           href={addBase(settingsSectionPath(spaceId, 'sync'))}
           aria-label={t('space.sync.chip_aria', { status: label })}
           data-testid="space-sync-chip">
          <span class="sh-sync-chip__icon" aria-hidden="true">{ICON[kind]}</span>
          <span class="sh-sync-chip__text">{label}</span>
          {kind === 'missing' && <span class="sh-sync-chip__chev" aria-hidden="true">›</span>}
        </a>
      )}
    </div>
  )
}

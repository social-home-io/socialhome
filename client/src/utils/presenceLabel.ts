/**
 * One wording for a member's presence state, shared by every surface
 * that shows it (presence page, corner, household strip, space map).
 */
import { t } from '@/i18n/i18n'

/** ``state`` in the UI language. A ``zone`` state names the zone when
 *  it is known; a missing or ``unavailable`` state reads as "Unknown";
 *  any other unexpected value is shown as sent. */
export function presenceLabel(
  state: string | null | undefined, zoneName?: string | null,
): string {
  switch (state) {
    case 'home': return t('presence.state.home')
    case 'away': return t('presence.state.away')
    case 'not_home': return t('presence.state.not_home')
    case 'zone': return zoneName || t('presence.state.in_zone')
    case null:
    case undefined:
    case '':
    case 'unavailable':
      return t('presence.state.unknown')
    default: return state
  }
}

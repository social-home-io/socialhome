/**
 * A refused group-game action (Join, Start, Remove …) in the person's own
 * words — shared by the Who's-in screen and Needs you's Join.
 */
import { ApiError } from '@/api'
import { t } from '@/i18n/i18n'

/** A refused action in the person's own words. */
export function refusalText(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case 'APP_GROUP_FULL': return t('apps.lobby.err.full')
      case 'APP_GROUP_CLOSED': return t('apps.lobby.err.closed')
      // No reason: whose household refused, and why, stays theirs.
      case 'APP_GROUP_CANT_JOIN': return t('apps.lobby.err.cant_join')
      case 'APP_GROUP_TOO_FEW': {
        const min = (err.extra?.params as { min?: unknown } | undefined)?.min
        return t('apps.lobby.err.too_few', { n: String(typeof min === 'number' ? min : '') })
      }
    }
  }
  return t('apps.lobby.err.other')
}

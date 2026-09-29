/**
 * Conversation mute helpers (§23.47).
 *
 * ``muted_until`` comes from ``GET /api/conversations`` (UTC ISO 8601, or
 * ``9999-12-31T23:59:59+00:00`` for "until I turn it back on"). The server
 * already drops a mute whose time has passed; :func:`isMuteActive` re-checks
 * so a thread left open past the end stops showing the bell-slash on the
 * next render.
 */
import { t } from '@/i18n/i18n'

export type MuteDuration = '1h' | '8h' | '1w' | 'forever'

export const MUTE_DURATIONS: readonly MuteDuration[] = ['1h', '8h', '1w', 'forever']

/** Label for each mute length — i18n key ``dms.mute.<duration>``. */
export function muteDurationLabel(d: MuteDuration): string {
  return t(`dms.mute.${d}`)
}

export function isMuteActive(
  until: string | null | undefined,
  now: number = Date.now(),
): boolean {
  if (!until) return false
  const ts = Date.parse(until)
  return Number.isFinite(ts) && ts > now
}

/** A mute with no end (the server's far-future sentinel). */
export function isMutedForever(until: string | null | undefined): boolean {
  return !!until && until.startsWith('9999-')
}

/** "Muted" / "Muted until 18:00" / "Muted until Mon 6 Oct, 10:00" — the
 *  viewer's locale and time zone, time only when it ends within a day. */
export function mutedLabel(
  until: string,
  now: number = Date.now(),
): string {
  if (isMutedForever(until)) return t('dms.mute.muted_forever')
  const end = new Date(until)
  const time = end.getTime() - now < 24 * 3600 * 1000
    ? end.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : end.toLocaleString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit',
    })
  return t('dms.mute.muted_until', { time })
}

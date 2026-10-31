/**
 * What Social Home's two invite sheets share (InviteSheet for 1:1 games,
 * GroupSheet for a group game): the tap-jacking arm on Send, a person's
 * key, and the server's invite caps in plain words.
 *
 * The caps (unanswered invites and new invites a day, per person and app)
 * are the server's. A group game counts as ONE invite there, however many
 * people it asks, so both sheets read the same counts.
 */
import { t } from '@/i18n/i18n'
import { ApiError } from '@/api'
import type { InviteCounts } from '@/store/apps'

/** Send ignores taps for this long after the sheet's list appears. */
export const SEND_ARM_MS = 600

/** Which invite cap is in the way, if any. */
export type InviteLimit = 'open' | 'daily' | null

/** One person, as both sheets key them. */
export const keyOf = (p: { instance_id: string, user_ref: string }): string =>
  `${p.instance_id}\u0000${p.user_ref}`

/** The cap the counts already hit (open first: the one the person can act on). */
export function limitFrom(c: InviteCounts | null): InviteLimit {
  if (!c) return null
  if (c.open >= c.max_open) return 'open'
  if (c.today >= c.max_per_day) return 'daily'
  return null
}

/** The cap a refused Send names, or ``null`` for any other failure. */
export function limitOf(err: unknown): InviteLimit {
  if (!(err instanceof ApiError)) return null
  if (err.code === 'APP_INVITES_OPEN_LIMIT') return 'open'
  if (err.code === 'APP_INVITES_DAILY_LIMIT') return 'daily'
  return null
}

/** A cap in the person's words; *appName* is the app's name. */
export function limitText(l: Exclude<InviteLimit, null>, counts: InviteCounts | null, appName: string): string {
  return l === 'open'
    ? t('apps.invite.limit_open', { count: String(counts?.max_open ?? 10), name: appName })
    : t('apps.invite.limit_daily', { count: String(counts?.max_per_day ?? 30) })
}

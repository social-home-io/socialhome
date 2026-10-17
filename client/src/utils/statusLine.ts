/**
 * statusLine — a member's emoji + text status as one line ("🌴 On
 * leave"), shared by the Presence page and Corner's "Who's home" card.
 *
 * The server already drops expired statuses; the client re-checks the
 * deadline so a status that lapses while the page is open stops showing
 * on the next render instead of waiting for the sweep's frame.
 */
import type { UserStatus } from '@/types'

/** "🌴 On leave" — null when there is nothing to show (unset or expired). */
export function statusLine(s: UserStatus | null | undefined, now: number = Date.now()): string | null {
  if (!s || (!s.emoji && !s.text)) return null
  if (s.expires_at && Date.parse(s.expires_at) <= now) return null
  return [s.emoji, s.text].filter(Boolean).join(' ')
}

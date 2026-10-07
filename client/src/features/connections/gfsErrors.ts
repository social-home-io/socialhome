/**
 * The words for a failed GFS connect — shared by the pairing modal
 * (QR / pasted code → ``POST /api/gfs/connections``) and onboarding's
 * open sign-up (``POST /api/gfs/connections/default``).
 *
 * The GFS refusal codes themselves (``GFS_UNREACHABLE``,
 * ``GFS_SIGNUP_CLOSED``, ``GFS_BUSY``, ``GFS_IDENTITY_MISMATCH``,
 * ``GFS_PAIRING_FAILED``, ``ALREADY_CONNECTED``) are translated where
 * every API code is — the ``MESSAGES`` table in ``apiErrors.ts`` — so
 * ``ApiError.message`` already says the right thing, in the UI language,
 * never in the GFS's own words. A code that table has no row for keeps
 * the server's reason (``routes/gfs.py`` writes those in plain English).
 *
 * This adds what that table can't know: here a 401 / 403 means "you're
 * not an admin", and a network failure gets GFS wording rather than the
 * household-pairing line.
 */
import { ApiError } from '@/api'
import { t } from '@/i18n/i18n'

/** Refusals a retry can't fix: the GFS said no for a reason that holds. */
export const FINAL_GFS_ERRORS: ReadonlySet<string> = new Set([
  'GFS_SIGNUP_CLOSED',
  'GFS_IDENTITY_MISMATCH',
])

/** The line to show for a failed GFS connect. */
export function gfsConnectErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return t('pairing.error.not_admin')
    }
    return err.message
  }
  if (err instanceof Error && err.message) {
    // Network / parse failure — keep the message but trim the prefix.
    return err.message.replace(/^Error:\s*/, '')
  }
  return t('gfs.error.generic')
}

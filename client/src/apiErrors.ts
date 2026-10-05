/**
 * The user-facing message of a failed API call — the one place that turns
 * a server error into words in the user's language.
 *
 * The server answers every error with
 * ``{"error": {"code", "detail", "params"?, …}}`` (``routes/base.py``).
 * ``detail`` is English for API clients; the SPA shows:
 *
 * 1. ``t('error.<code>', params)`` when the code is in the table below;
 * 2. else, a generic per-status line (404 / 422 / 429 / 5xx) when the
 *    server's answer was itself generic — no detail, a catch-all code, or
 *    a 5xx without a code;
 * 3. else the server's ``detail`` (an unknown, specific code);
 * 4. else ``"API <status>: <path>"``.
 *
 * Every toast site shows ``err.message``, which ``ApiError`` fills from
 * here — so a new code needs a row in ``MESSAGES`` and an ``error.*`` key
 * in every locale, nothing at the call sites.
 */
import { t } from '@/i18n/i18n'
import { accessNote } from '@/features/spaces/spaceAccess'
import { formatBazaarAmount } from '@/components/bazaarFormat'

/** The parsed ``error`` object of a canonical error body. */
export interface ApiErrorBody {
  code?: unknown
  detail?: unknown
  [extra: string]: unknown
}

type Params = Record<string, unknown>

/** The structured ``params`` the server attached, or ``{}``. */
function paramsOf(body: ApiErrorBody): Params {
  const p = body.params
  return p && typeof p === 'object' && !Array.isArray(p) ? p as Params : {}
}

/** Codes whose ``detail`` is a catch-all — the per-status line says the
 *  same thing, translated. */
const GENERIC_CODES = new Set(['NOT_FOUND', 'RATE_LIMITED', 'INTERNAL_ERROR', 'SERVICE_UNAVAILABLE'])

/** The fixed detail of the server's blanket ``ValueError`` mapping
 *  (``BaseView._iter``) — generic, so it gets the translated 422 line. */
const GENERIC_UNPROCESSABLE_DETAIL = 'Request could not be processed.'

/** A param as display text ("" when absent). */
function str(params: Params, key: string): string {
  const v = params[key]
  return v === undefined || v === null ? '' : String(v)
}

/** Code → translated message (``null`` = no translation for this shape,
 *  fall through). Static ``t('…')`` calls so the i18n check sees every key. */
const MESSAGES: Record<string, (body: ApiErrorBody, params: Params) => string | null> = {
  ACCESS_ADMIN_ONLY: (body) => accessNote(String(body.feature ?? '')),
  HOST_UNREACHABLE: (body) => body.reason === 'unknown_host'
    ? t('space.host.unknown')
    : t('space.host.unreachable'),
  HOST_TOO_OLD: (body) => body.feature === 'role_change'
    ? t('space.member.role_host_too_old')
    : t('moderation.error.host_too_old'),
  // Spaces: joining, following, writing.
  ALREADY_MEMBER: () => t('error.already_member'),
  USER_ALREADY_MEMBER: () => t('error.user_already_member'),
  BANNED: () => t('error.banned'),
  USER_BANNED: () => t('error.user_banned'),
  INVITE_ONLY: () => t('error.invite_only'),
  INVITE_EXPIRED: () => t('error.invite_expired'),
  SUBSCRIBE_NOT_ALLOWED: () => t('error.subscribe_not_allowed'),
  SUBSCRIBER_READ_ONLY: () => t('error.subscriber_read_only'),
  SPACE_ARCHIVED: () => t('error.space_archived'),
  NOT_PAIRED: () => t('error.not_paired'),
  AGE_RESTRICTED: (_b, p) => t('error.age_restricted', { min_age: str(p, 'min_age') }),
  // Conversations.
  DM_SELF: () => t('error.dm_self'),
  GROUP_TOO_SMALL: (_b, p) => t('error.group_too_small', { min: str(p, 'min') }),
  DM_TOO_LONG: (_b, p) => t('error.dm_too_long', { max: str(p, 'max') }),
  DM_BLOCKED: () => t('error.dm_blocked'),
  DM_YOU_BLOCKED: () => t('error.dm_you_blocked'),
  DM_NOT_ALLOWED: () => t('error.dm_not_allowed'),
  DM_GROUP_NOT_ALLOWED: () => t('error.dm_group_not_allowed'),
  GROUP_MEMBER_UNSUPPORTED: (_b, p) => {
    switch (p.reason) {
      case 'legacy_group': return t('error.group_member.legacy_group')
      case 'not_paired': return t('error.group_member.not_paired', { name: str(p, 'name') })
      case 'too_old': return t('error.group_member.too_old', { name: str(p, 'name') })
      default: return null
    }
  },
  // Calendar, polls, moments.
  RSVP_PAST: () => t('error.rsvp_past'),
  POLL_CLOSED: () => t('error.poll_closed'),
  MOMENT_RATE_LIMIT: () => t('error.moment_rate_limit'),
  // Bazaar.
  BID_TOO_LOW: (_b, p) => {
    const floor = Number(p.floor_amount)
    const currency = str(p, 'currency')
    if (!Number.isFinite(floor) || !currency) return null
    let amount: string
    try {
      amount = formatBazaarAmount(floor, currency)
    } catch {
      return null  // An unknown currency code: keep the server's detail.
    }
    return t('error.bid_too_low', { amount })
  },
  OWN_LISTING: () => t('error.own_listing'),
  LISTING_NOT_ACTIVE: () => t('error.listing_not_active'),
  // Pictures.
  IMAGE_TOO_LARGE: (_b, p) => t('error.image_too_large', { max_mb: str(p, 'max_mb') }),
  IMAGE_UNREADABLE: () => t('error.image_unreadable'),
  // Server-side outages the user can act on or wait out.
  GFS_UNAVAILABLE: () => t('error.gfs_unavailable'),
  STORAGE_FULL: () => t('error.storage_full'),
  AI_AGENT_UNAVAILABLE: () => t('error.ai_unavailable'),
}

/** The generic line for a status, or ``null`` when there is none. */
function statusFallback(status: number): string | null {
  if (status >= 500) return t('error.server')
  if (status === 404) return t('error.not_found')
  if (status === 422) return t('error.unprocessable')
  if (status === 429) return t('error.rate_limited')
  return null
}

/** The message an ``ApiError`` shows for ``status`` + parsed ``body``. */
export function apiErrorMessage(
  status: number, path: string, body: ApiErrorBody | null,
): string {
  const code = typeof body?.code === 'string' ? body.code : null
  const detail = typeof body?.detail === 'string' && body.detail ? body.detail : null
  if (body && code && Object.hasOwn(MESSAGES, code)) {
    const translated = MESSAGES[code](body, paramsOf(body))
    if (translated !== null) return translated
  }
  // A 5xx with a specific code (GFS_APPEAL_FAILED, ISSUER_TIMEOUT…) keeps
  // its detail: it says more than "something went wrong".
  const generic = (status >= 500 && code === null)
    || !detail
    || (code !== null && GENERIC_CODES.has(code))
    || (code === 'UNPROCESSABLE' && detail === GENERIC_UNPROCESSABLE_DETAIL)
  if (generic) {
    const fallback = statusFallback(status)
    if (fallback) return fallback
  }
  return detail ?? `API ${status}: ${path}`
}

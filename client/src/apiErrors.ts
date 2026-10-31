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
  GFS_RELAY_NOT_ALLOWED: () => t('error.gfs_relay_not_allowed'),
  // Pairing: a cancel or an approval raced a pairing that went live, or
  // a trust-relay request too old to approve.
  PAIRING_CONFIRMED: () => t('error.pairing_confirmed'),
  REQUEST_EXPIRED: () => t('error.request_expired'),
  // Highlights: a frame asked for other people than today's highlight.
  HIGHLIGHT_AUDIENCE_MISMATCH: () => t('error.highlight_audience_mismatch'),
  HIGHLIGHT_NOT_TODAY: () => t('error.highlight_not_today'),
  HIGHLIGHT_AUDIENCE_EMPTY: () => t('error.highlight_audience_empty'),
  HIGHLIGHT_AUDIENCE_PEER_TOO_OLD: () => t('error.highlight_audience_peer_too_old'),
  SPACE_NOT_PUBLIC: () => t('error.space_not_public'),
  // Listing a space on a GFS: only a global space, on servers its owner
  // picks. Publishing a public space would make it global (a tier change).
  TIER_CHANGE_REQUIRED: () => t('error.tier_change_required'),
  GFS_SELECTION_REQUIRED: () => t('error.gfs_selection_required'),
  GFS_SELECTION_INVALID: (_b, p) => {
    switch (p.reason) {
      case 'unknown': return t('error.gfs_selection.unknown')
      case 'too_many': return t('error.gfs_selection.too_many', { max: str(p, 'max') })
      case 'not_global': return t('error.gfs_selection.not_global')
      case 'unsupported': return t('error.gfs_selection.unsupported')
      default: return null
    }
  },
  USER_ALREADY_MEMBER: () => t('error.user_already_member'),
  BANNED: () => t('error.banned'),
  USER_BANNED: () => t('error.user_banned'),
  INVITE_ONLY: () => t('error.invite_only'),
  INVITE_EXPIRED: () => t('error.invite_expired'),
  SUBSCRIBE_NOT_ALLOWED: () => t('error.subscribe_not_allowed'),
  SUBSCRIBER_READ_ONLY: () => t('error.subscriber_read_only'),
  SPACE_ARCHIVED: () => t('error.space_archived'),
  INVALID_LINK: () => t('error.invalid_link'),
  NOT_PAIRED: () => t('error.not_paired'),
  AGE_RESTRICTED: (_b, p) => t('error.age_restricted', { min_age: str(p, 'min_age') }),
  // Conversations.
  DM_SELF: () => t('error.dm_self'),
  GROUP_TOO_SMALL: (_b, p) => t('error.group_too_small', { min: str(p, 'min') }),
  DM_TOO_LONG: (_b, p) => t('error.dm_too_long', { max: str(p, 'max') }),
  INVALID_MEDIA_URL: () => t('error.invalid_media_url'),
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
  OWN_MOMENT: () => t('error.moment_own_save'),
  SAVE_UNAVAILABLE: () => t('error.moment_save_unavailable'),
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
  LISTING_THREAD_CLOSED: () => t('error.listing_thread_closed'),
  SELLER_HOUSEHOLD_TOO_OLD: () => t('error.seller_household_too_old'),
  LISTING_THREAD_TEXT_ONLY: () => t('error.listing_thread_text_only'),
  LISTING_THREAD_NO_CHANGES: () => t('error.listing_thread_no_changes'),
  LISTING_THREAD_RATE_LIMITED: () => t('error.listing_thread_rate_limited'),
  BAZAAR_FAQ_TEXT: (_b, p) => t('error.bazaar_faq_text', {
    question_max: str(p, 'question_max') || '200',
    answer_max: str(p, 'answer_max') || '500',
  }),
  BAZAAR_FAQ_FULL: (_b, p) => t('error.bazaar_faq_full', { max: str(p, 'max') || '10' }),
  BAZAAR_FAQ_USED_UP: (_b, p) => t('error.bazaar_faq_used_up', { max: str(p, 'max') || '30' }),
  BAZAAR_FAQ_RATE_LIMITED: () => t('error.bazaar_faq_rate_limited'),
  // Pictures.
  IMAGE_TOO_LARGE: (_b, p) => t('error.image_too_large', { max_mb: str(p, 'max_mb') }),
  APP_BUNDLE_TOO_LARGE: () => t('error.app_bundle_too_large'),
  APP_DOWNLOAD_FAILED: () => t('error.app_download_failed'),
  APP_CATALOG_DISABLED: () => t('error.app_catalog_disabled'),
  APP_CATALOG_UNREACHABLE: () => t('error.app_catalog_unreachable'),
  APP_NOT_ENABLED: () => t('error.app_not_enabled'),
  APP_AGE_RESTRICTED: () => t('error.app_age_restricted'),
  APP_NEEDS_NEWER_SOCIAL_HOME: () => t('error.app_needs_newer_social_home'),
  APP_PERMISSION_DENIED: () => t('error.app_permission_denied'),
  // Games in a space (group play, v_77) and the group games they open.
  SPACE_APP_NOT_ALLOWED: () => t('error.space_app_not_allowed'),
  SPACE_TABLE_DAILY_LIMIT: (_b, p) => t('error.space_table_daily_limit', { limit: str(p, 'limit') }),
  SPACE_TABLE_ANNOUNCE_LIMIT: () => t('error.space_table_announce_limit'),
  SPACE_TABLE_HOUSEHOLD_TOO_OLD: () => t('error.space_table_household_too_old'),
  APP_GROUP_NOT_FOUND: () => t('error.app_group_not_found'),
  APP_GROUP_NOT_HOST: () => t('error.app_group_not_host'),
  APP_GROUP_CLOSED: () => t('error.app_group_closed'),
  APP_GROUP_FULL: () => t('error.app_group_full'),
  APP_GROUP_CANT_JOIN: () => t('error.app_group_cant_join'),
  APP_GROUPS_NOT_SUPPORTED: () => t('error.app_groups_not_supported'),
  APP_GROUP_SIZE: (_b, p) => t('error.app_group_size', { min: str(p, 'min'), max: str(p, 'max') }),
  APP_GROUP_OPEN_LIMIT: (_b, p) => t('error.app_group_open_limit', { limit: str(p, 'limit') }),
  APP_GROUP_DAILY_LIMIT: (_b, p) => t('error.app_group_daily_limit', { limit: str(p, 'limit') }),
  IMAGE_UNREADABLE: () => t('error.image_unreadable'),
  // Connecting to a GFS (QR / pasted code, or onboarding's open sign-up).
  // Our own words for every refusal, never the GFS's (``routes/gfs.py``).
  GFS_UNREACHABLE: () => t('error.gfs_unavailable'),
  GFS_SIGNUP_CLOSED: () => t('error.gfs_signup_closed'),
  GFS_BUSY: () => t('error.gfs_busy'),
  GFS_IDENTITY_MISMATCH: () => t('error.gfs_identity_mismatch'),
  GFS_PROOF_FAILED: () => t('error.gfs_proof_failed'),
  GFS_PAIRING_FAILED: () => t('error.gfs_pairing_failed'),
  ALREADY_CONNECTED: () => t('error.gfs_already_connected'),
  // Server-side outages the user can act on or wait out.
  GFS_UNAVAILABLE: () => t('error.gfs_unavailable'),
  // The GFS can't serve this yet (``params.feature``): its operator updates it.
  GFS_TOO_OLD: () => t('error.gfs_too_old'),
  // A per-moment share link can't be made (no pinned viewer / no GFS
  // that keeps encrypted media).
  SHARE_LINK_UNAVAILABLE: () => t('error.share_link_unavailable'),
  STORAGE_FULL: () => t('error.storage_full'),
  // Household dashboard (Admin › Dashboard). ``params`` carry the port /
  // length limits (``domain/household_dashboard.py``).
  DASHBOARD_PASSWORD_REQUIRED: () => t('error.dashboard_password_required'),
  DASHBOARD_PORT_FIXED: () => t('error.dashboard_port_fixed'),
  DASHBOARD_PORT_IN_USE: (_b, p) => p.port === undefined
    ? null
    : t('error.dashboard_port_in_use', { port: str(p, 'port') }),
  DASHBOARD_SETTINGS_INVALID: () => t('error.dashboard_settings_invalid'),
  DASHBOARD_PASSWORD_TOO_SHORT: (_b, p) => t('error.dashboard_password_too_short', {
    min: str(p, 'min') || '8',
  }),
  DASHBOARD_PIN_INVALID: (_b, p) => t('error.dashboard_pin_invalid', {
    min: str(p, 'min') || '4', max: str(p, 'max') || '6',
  }),
  DASHBOARD_NOT_RUNNING: () => t('error.dashboard_not_running'),
  // Wall panels (Admin › Dashboard › Wall panels).
  DASHBOARD_DISABLED: () => t('error.dashboard_disabled'),
  DASHBOARD_PAIRING_CODE_INVALID: () => t('error.dashboard_pairing_code_invalid'),
  DASHBOARD_PAIRING_THROTTLED: (_b, p) => t('error.dashboard_pairing_throttled', {
    minutes: String(Math.max(1, Math.ceil(Number(p.retry_after ?? 60) / 60))),
  }),
  DASHBOARD_DISPLAY_NOT_FOUND: () => t('error.dashboard_display_not_found'),
  DASHBOARD_DISPLAY_LIMIT: (_b, p) => t('error.dashboard_display_limit', {
    max: str(p, 'max') || '20',
  }),
  // Wall panels found in Home Assistant.
  DISPLAY_DISCOVERY_UNAVAILABLE: () => t('error.display_discovery_unavailable'),
  DISPLAY_DISCOVERY_FAILED: () => t('error.display_discovery_failed'),
  DISPLAY_PANEL_NOT_FOUND: () => t('error.display_panel_not_found'),
  DISPLAY_PANEL_OFFLINE: () => t('error.display_panel_offline'),
  DISPLAY_PANEL_NEEDS_UPDATE: () => t('error.display_panel_needs_update'),
  DASHBOARD_ADDRESS_REQUIRED: (_b, p) => p.reason === 'port_not_published'
    ? t('error.dashboard_address_port')
    : t('error.dashboard_address_required'),
  AI_AGENT_UNAVAILABLE: () => t('error.ai_unavailable'),
  // Zones (space and household). A space's ZONE_LIMIT carries no
  // params; both caps are 50.
  ZONE_LIMIT: (_b, p) => t('error.zone_limit', { max: str(p, 'max') || '50' }),
  ZONE_NAME_TAKEN: () => t('error.zone_name_taken'),
  ZONE_READ_ONLY: () => t('error.zone_read_only'),
  ZONE_IS_HOME: () => t('error.zone_is_home'),
  // Handing a space to a new owner (§23.48).
  OWNER_NOT_LOCAL: () => t('error.owner_not_local'),
  OWNER_IS_FOLLOWER: () => t('error.owner_is_follower'),
  OWNER_IS_SELF: () => t('error.owner_is_self'),
  // Local communities: worded for the reader — ``own`` our household's
  // home, ``peer`` another household's, ``host`` our home as the space's
  // host sees it.
  HOME_NOT_SET: () => t('error.home_not_set'),
  LOCATION_REQUIRED: () => t('error.location_required'),
  JOIN_REQUEST_SENT: () => t('error.join_request_sent'),
  OUTSIDE_AREA: (_b, p) => (str(p, 'whose') === 'peer'
    ? t('error.outside_area_peer') : t('error.outside_area_own')),
  LOCAL_AREA_RATE_LIMITED: (_b, p) => (str(p, 'whose') === 'peer'
    ? t('error.local_area_rate_limited_peer') : t('error.local_area_rate_limited_own')),
  LOCAL_AREA_PUBLIC_ONLY: () => t('error.local_area_public_only'),
  LOCAL_AREA_UNAVAILABLE: () => t('error.local_area_unavailable'),
  LOCAL_AREA_DELEGATED: () => t('error.local_area_delegated'),
  PLACE_PIN_GLOBAL_ONLY: () => t('error.place_pin_global_only'),
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

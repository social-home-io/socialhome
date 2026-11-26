/**
 * User-preference helpers.
 *
 * The backend stores preferences as a JSON string on the ``User`` row;
 * the frontend reads it through this module so string parsing + sane
 * defaults live in one place.
 */
import { api } from '@/api'
import { isOne, t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'

export type LandingPath = '/' | '/feed' | '/dashboard'

/** 1–5 hops (server: ``MOMENT_MAX_HOPS`` = ``MOMENT_MAX_REACH``). */
export type MomentsHops = 1 | 2 | 3 | 4 | 5

export interface MomentsPreferences {
  /** Per-user max-hops visibility (§Momentum-relay-policy). 1 = only
   *  moments authored by direct peers; 3 = the default; up to 5 (v_96). */
  max_hops?: MomentsHops
  /** How long people who follow this user keep their new moments, in
   *  days (decision 6d). The server clamps to [1, 30] and defaults to 7
   *  (``parse_moment_preferences``). */
  follower_retention_days?: number
  /** How far this user's own moments go, 1–5 hops (v_96, default 3);
   *  the household maximum and child protection bound it on the server. */
  reach?: MomentsHops
}

/** Server: ``MOMENT_DEFAULT_REACH``. */
export const MOMENT_DEFAULT_REACH = 3
/** Server: ``MOMENT_MAX_REACH`` (= ``MOMENT_MAX_HOPS``). */
export const MOMENT_MAX_REACH = 5

/** A ``moments`` hop count as the server reads it (``_strict_int``): a
 *  whole number clamps to 1–``MOMENT_MAX_REACH``, anything else is
 *  the default ``MOMENT_DEFAULT_REACH``. */
function hopsOf(v: unknown): MomentsHops {
  if (typeof v !== 'number' || !Number.isInteger(v)) return MOMENT_DEFAULT_REACH
  return Math.min(MOMENT_MAX_REACH, Math.max(1, v)) as MomentsHops
}

/** Server: ``MOMENT_DEFAULT_RETENTION_DAYS``. */
export const MOMENT_RETENTION_DEFAULT = 7
/** Server: ``MOMENT_MIN_RETENTION_DAYS`` / ``MOMENT_MAX_LIFETIME_DAYS``. */
export const MOMENT_RETENTION_MIN = 1
export const MOMENT_RETENTION_MAX = 30
/** The presets the Settings picker offers. */
export const MOMENT_RETENTION_CHOICES: readonly number[] = [1, 3, 7, 14, 30]

/** "7 days" / "1 day" — the retention picker and the Yours tab. */
export function momentRetentionLabel(days: number): string {
  return t(isOne(days) ? 'settings.momentum.days_one' : 'settings.momentum.days', { n: String(days) })
}

/** The user's ``moments`` block with the server's defaults and clamps
 *  applied, so the SPA shows what the server will act on. */
export function getMomentsPreferences(): Required<MomentsPreferences> {
  const raw = (getPreferences().moments ?? {}) as MomentsPreferences
  const days = Number(raw.follower_retention_days)
  return {
    max_hops: hopsOf(raw.max_hops),
    follower_retention_days: Number.isFinite(days) && raw.follower_retention_days != null
      ? Math.min(MOMENT_RETENTION_MAX, Math.max(MOMENT_RETENTION_MIN, Math.trunc(days)))
      : MOMENT_RETENTION_DEFAULT,
    reach: hopsOf(raw.reach),
  }
}

interface Preferences {
  landing_path?: LandingPath
  followed_space_ids?: string[]
  moments?: MomentsPreferences
  /** ha / haos only — the HA notify service that pushes this user's
   *  Companion app (e.g. ``notify.mobile_app_pascals_iphone``). Empty /
   *  absent disables HA-app push for the user (§25.3). */
  ha_notify_service?: string
  /** ha / haos only — opt-in (off by default) to fire this user's
   *  notifications on Home Assistant's event bus for automations; the
   *  event carries the type, ids and link, never the text. Refused by the
   *  server for a protected account (D14). */
  ha_automation_events?: boolean
  /** First day of the week for calendar-style views. Absent = follow
   *  the browser locale (see ``utils/week.ts``). */
  week_start?: 'mon' | 'sun' | null
  /** UI language (a code from ``i18n/locales/_meta.json``). */
  locale?: string
  /** Space timetables whose lessons join this user's Today card, in pin
   *  order (the server reads at most 20). */
  timetable_home_pins?: string[]
  [key: string]: unknown
}

export function getPreferences(): Preferences {
  const raw = (currentUser.value as unknown as {
    preferences_json?: string
  } | null)?.preferences_json
  if (!raw) return {}
  try {
    return JSON.parse(raw) as Preferences
  } catch {
    return {}
  }
}

export function getLandingPath(): LandingPath {
  return getPreferences().landing_path ?? '/'
}

export async function setPreference<K extends keyof Preferences>(
  key: K, value: Preferences[K],
): Promise<void> {
  await api.patch('/api/me', { preferences: { [key]: value } })
  // Optimistically update the cached currentUser so the new preference
  // reflects immediately without a separate ``/api/me`` round-trip.
  const prev = currentUser.value
  if (!prev) return
  const raw = (prev as unknown as {
    preferences_json?: string
  }).preferences_json ?? '{}'
  let parsed: Preferences = {}
  try { parsed = JSON.parse(raw) as Preferences } catch { /* empty */ }
  if (value === null || value === undefined) {
    delete parsed[key as string]
  } else {
    parsed[key as string] = value
  }
  currentUser.value = {
    ...prev,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    preferences_json: JSON.stringify(parsed),
  } as any
}

/**
 * momentLimits — what ``GET /api/moments/limits`` says about the caller:
 * when their next top-level moment can go, and how far their moments go
 * (v_96 reach) — plus the one way every surface reads and words a reach.
 *
 * ``reach`` is the caller's effective reach for a new moment (1–5: 1 for a
 * protected account, else their "How far my moments go" within the
 * household maximum); ``reach_max`` is that maximum. Both are ``null``
 * until read (or when the read failed) — the text then says the person's
 * own reach setting (see ``shownReach``), and the server clamps whatever
 * is picked.
 *
 * The composer, the inbox's "Share a moment" line, the reply field and
 * Settings › Momentum read the same signals, so a setting changed in one
 * place shows everywhere at once.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { Moment } from '@/types'
import {
  getMomentsPreferences,
  MOMENT_DEFAULT_REACH,
  MOMENT_MAX_REACH,
} from '@/utils/preferences'

export { MOMENT_DEFAULT_REACH, MOMENT_MAX_REACH }

export interface MomentLimits {
  /** UTC ISO 8601, ``null`` = the caller may post now. */
  next_allowed_at: string | null
  reach?: number
  reach_max?: number
}

/** The caller's reach for a new moment, ``null`` until read. */
export const myMomentReach = signal<number | null>(null)
/** The household's maximum reach, ``null`` until read. */
export const momentReachMax = signal<number | null>(null)

function hops(v: unknown): number | null {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= MOMENT_MAX_REACH
    ? v
    : null
}

/** The latest read, settled or not — a fresh read waits for it. */
let last: Promise<unknown> | null = null
/** Bumped per read: only the newest read writes the signals, so an older
 *  one landing late can't put back what it saw. */
let readSeq = 0

/** Read the limits; ``null`` when they couldn't be read (what was known
 *  before stays). Callers at the same time already share one request
 *  (``api.get`` shares identical in-flight GETs). That sharing is why a
 *  ``fresh`` read (after a save) first waits for a read sent before it:
 *  joining that one would answer with what the server said before the
 *  save. */
export async function loadMomentLimits(
  { fresh = false }: { fresh?: boolean } = {},
): Promise<MomentLimits | null> {
  if (fresh && last) await last.catch(() => undefined)
  const mine = ++readSeq
  const req = Promise.resolve().then(() => api.get<MomentLimits>('/api/moments/limits'))
  last = req
  try {
    const r = await req
    if (mine === readSeq) {
      myMomentReach.value = hops(r?.reach)
      momentReachMax.value = hops(r?.reach_max)
    }
    return r ?? null
  } catch {
    return null
  }
}

/** How far a new moment of the caller's goes, for the text: the read
 *  reach, or — until the limits are read (or when the read failed) — the
 *  person's own "How far my moments go" (1 for a protected account). That
 *  fallback is an upper bound (the household maximum can only lower it),
 *  so the text never says less than a moment may really go. */
export function shownReach(): number {
  if (myMomentReach.value != null) return myMomentReach.value
  if (currentUser.value?.protected) return 1
  return getMomentsPreferences().reach
}

/** How many hops a moment goes (its ``reach``, fixed when it was
 *  written); anything outside 1–``MOMENT_MAX_REACH`` reads as the
 *  default. */
export function momentReach(m: Pick<Moment, 'reach'>): number {
  return hops(m.reach) ?? MOMENT_DEFAULT_REACH
}

/** How far a reply goes: no further than the caller's reach, nor than the
 *  moment it answers (``null`` while that isn't loaded yet: the default). */
export function replyReach(parent: Pick<Moment, 'reach'> | null | undefined): number {
  return Math.min(shownReach(), parent ? momentReach(parent) : MOMENT_DEFAULT_REACH)
}

/** A reach sentence: reach 1 is a sentence of its own ("your household
 *  and paired households", the ``_one`` key), never "up to 1 hops".
 *  ``extra`` carries any other parameters (``n`` is filled in here).
 *
 *  Keys built here: moment.entry.reach_one, moment.composer.reach_reply_one,
 *  moment.composer.reach_more_one, moment.composer.reach_more_reply_one,
 *  moment.detail.status_reach_one, moment.detail.status_public_also_one,
 *  moment.card.who_sees_body_one, settings.momentum.reach_household_max_one. */
export function reachText(base: string, n: number, extra?: Record<string, string>): string {
  // Not i18n isOne(): the _one keys are a different sentence, not a plural
  // form, and isOne() would also pick it for French 0.
  return t(n === 1 ? `${base}_one` : base, { ...extra, n: String(n) })
}

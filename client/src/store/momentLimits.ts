/**
 * momentLimits — what ``GET /api/moments/limits`` says about the caller:
 * when their next top-level moment can go, and how far their moments go
 * (v_96 reach).
 *
 * ``reach`` is the caller's effective reach for a new moment (1–5: 1 for a
 * protected account, else their "How far my moments go" within the
 * household maximum); ``reach_max`` is that maximum. Both are ``null``
 * until read (or on an older server) — the text then says the default, 3
 * hops, and the server clamps whatever is picked.
 *
 * The composer, the inbox's "Share a moment" line, the reply field and
 * Settings › Momentum read the same signals, so a setting changed in one
 * place shows everywhere at once.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

/** Server: ``MOMENT_DEFAULT_REACH``. */
export const MOMENT_DEFAULT_REACH = 3
/** Server: ``MOMENT_MAX_REACH`` (= ``MOMENT_MAX_HOPS``). */
export const MOMENT_MAX_REACH = 5

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

/** Read the limits; ``null`` when they couldn't be read (what was known
 *  before stays). */
export async function loadMomentLimits(): Promise<MomentLimits | null> {
  try {
    const r = await api.get<MomentLimits>('/api/moments/limits')
    myMomentReach.value = hops(r?.reach)
    momentReachMax.value = hops(r?.reach_max)
    return r ?? null
  } catch {
    return null
  }
}

/** How far a new moment of the caller's goes, for the text: the read
 *  reach, or the default while unknown. */
export function shownReach(): number {
  return myMomentReach.value ?? MOMENT_DEFAULT_REACH
}

/** How far a reply goes: no further than the caller's reach, nor than the
 *  moment it answers (its ``reach``; a moment from an older server reads
 *  as the default). */
export function replyReach(parentReach: number | null | undefined): number {
  return Math.min(shownReach(), hops(parentReach) ?? MOMENT_DEFAULT_REACH)
}

/** The i18n key for a reach: reach 1 is a sentence of its own ("your
 *  household and paired households"), never "up to 1 hops". */
export function reachKey(base: string, n: number): string {
  return n === 1 ? `${base}_one` : base
}

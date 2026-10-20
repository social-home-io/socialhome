/**
 * Labels for people and households when a name may be missing.
 *
 * Owner rule: never show an id (a user id, an instance id, a GFS id, a
 * space id) as a name — not whole, not cut to 8 characters. Pass the
 * name candidates in priority order; the first non-blank one wins, and
 * when there is none the label is neutral wording instead of the id.
 */
import { t } from '@/i18n/i18n'

type Candidate = string | null | undefined

/** The first non-blank candidate, trimmed — or ``null``. */
export function firstName(...candidates: Candidate[]): string | null {
  for (const c of candidates) {
    const s = c?.trim()
    if (s) return s
  }
  return null
}

/** A person's name, else "Someone". */
export function personLabel(...candidates: Candidate[]): string {
  return firstName(...candidates) ?? t('person.someone')
}

/** A household's name, else "another household" (reads inside a
 *  sentence: "Invited by another household", "Paired with …"). */
export function householdLabel(...candidates: Candidate[]): string {
  return firstName(...candidates) ?? t('names.another_household')
}

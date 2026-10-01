/**
 * Task labels: free-form tags, at most ``MAX_LABELS`` of at most
 * ``MAX_LABEL_LEN`` characters (the server's limits), unique
 * case-insensitively — the first spelling wins, like the backend's
 * ``normalize_labels``. Identity is the cleaned label lower-cased (the
 * server's casefold: "Café" and "Cafe" are two labels). Only the
 * colour ignores accents too (``hashColor`` of the normalised name),
 * so "Garden" is the same green on every card, list and household.
 */
import type { TaskItem } from '@/types'
import { colorClass, hashColor } from '@/utils/tokenColor'

export const MAX_LABELS = 10
export const MAX_LABEL_LEN = 32

/** Trimmed, inner whitespace collapsed (what is stored). */
export function cleanLabel(raw: string): string {
  return raw.trim().replace(/\s+/g, ' ')
}

/** The case-insensitive identity of a label (as the server compares). */
export function labelKey(label: string): string {
  return cleanLabel(label).toLocaleLowerCase()
}

export function labelColorClass(label: string): string {
  return colorClass(hashColor(label))
}

export type AddLabelError = 'empty' | 'too_long' | 'too_many' | 'duplicate'

/** ``labels`` plus ``raw``, or why it can't be added. */
export function addLabel(
  labels: readonly string[], raw: string,
): { labels: string[]; error?: AddLabelError } {
  const label = cleanLabel(raw)
  if (!label) return { labels: [...labels], error: 'empty' }
  if (label.length > MAX_LABEL_LEN) return { labels: [...labels], error: 'too_long' }
  if (labels.some(l => labelKey(l) === labelKey(label))) return { labels: [...labels], error: 'duplicate' }
  if (labels.length >= MAX_LABELS) return { labels: [...labels], error: 'too_many' }
  return { labels: [...labels, label] }
}

/** Every label used in ``tasks``, one spelling per key (the first
 *  seen), sorted for the UI language. */
export function collectLabels(tasks: readonly TaskItem[], lang?: string): string[] {
  const seen = new Map<string, string>()
  for (const x of tasks) {
    for (const l of x.labels ?? []) {
      const k = labelKey(l)
      if (k && !seen.has(k)) seen.set(k, l)
    }
  }
  return [...seen.values()].sort((a, b) => a.localeCompare(b, lang, { sensitivity: 'base' }))
}

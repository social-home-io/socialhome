/**
 * Video length helpers for composers: read a picked file's length before
 * uploading it, and print seconds in the UI language ("2.5" / "2,5").
 */
import { formatLocale } from '@/i18n/i18n'

/** ``2450`` → ``2.5`` (en) / ``2,5`` (de): one decimal, locale digits. */
export function formatSeconds(ms: number): string {
  return new Intl.NumberFormat(formatLocale(), {
    minimumFractionDigits: 1, maximumFractionDigits: 1,
  }).format(ms / 1000)
}

/** The video's length in ms, or ``null`` when the browser can't read its
 *  metadata (the caller says so in the UI language). */
export function readVideoDurationMs(file: File): Promise<number | null> {
  const url = URL.createObjectURL(file)
  const probe = document.createElement('video')
  probe.preload = 'metadata'
  return new Promise<number | null>((resolve) => {
    probe.onloadedmetadata = () => resolve(probe.duration * 1_000)
    probe.onerror = () => resolve(null)
    probe.src = url
  }).finally(() => URL.revokeObjectURL(url))
}

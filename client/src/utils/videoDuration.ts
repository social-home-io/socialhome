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

/** How long a length probe may take before the file counts as unreadable
 *  (a codec the browser stalls on must not leave a composer busy). */
export const PROBE_TIMEOUT_MS = 10_000

/** The video's length in ms, or ``null`` when the browser can't read its
 *  metadata, reports no finite length, or doesn't answer within
 *  :data:`PROBE_TIMEOUT_MS` (the caller says so in the UI language). */
export function readVideoDurationMs(file: File): Promise<number | null> {
  const url = URL.createObjectURL(file)
  const probe = document.createElement('video')
  probe.preload = 'metadata'
  let timer: ReturnType<typeof setTimeout> | undefined
  return new Promise<number | null>((resolve) => {
    timer = setTimeout(() => resolve(null), PROBE_TIMEOUT_MS)
    probe.onloadedmetadata = () => {
      const ms = probe.duration * 1_000
      resolve(Number.isFinite(ms) ? ms : null)
    }
    probe.onerror = () => resolve(null)
    probe.src = url
  }).finally(() => {
    clearTimeout(timer)
    probe.removeAttribute('src')
    URL.revokeObjectURL(url)
  })
}

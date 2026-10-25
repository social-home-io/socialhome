/**
 * bytes — a byte count as people read it ("1.2 GB", "350 MB") in the UI
 * language's number format. Binary steps (1024), the familiar unit names.
 */
import { formatLocale } from '@/i18n/i18n'

const UNITS = ['byte', 'kilobyte', 'megabyte', 'gigabyte', 'terabyte'] as const

export function formatBytes(n: number): string {
  let v = Math.max(0, n)
  let i = 0
  while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i++ }
  try {
    return new Intl.NumberFormat(formatLocale(), {
      style: 'unit',
      unit: UNITS[i],
      unitDisplay: 'short',
      maximumFractionDigits: i <= 1 ? 0 : 1,
    }).format(v)
  } catch {
    return `${v.toFixed(i <= 1 ? 0 : 1)} ${['B', 'kB', 'MB', 'GB', 'TB'][i]}`
  }
}

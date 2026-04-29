/**
 * ThemeToggle — dark/light/auto theme switcher (§23.35).
 *
 * Pure UI button. The signal + effect that drives the `<html>` class
 * lives in `@/store/theme.ts` so it can be loaded eagerly from
 * `main.tsx` rather than on first /settings visit.
 */
import { theme, type Theme } from '@/store/theme'

export { theme }
export type { Theme }

export function ThemeToggle() {
  const next = () => {
    const order: Theme[] = ['light', 'dark', 'auto']
    const idx = order.indexOf(theme.value)
    theme.value = order[(idx + 1) % order.length]
  }
  const icon = theme.value === 'light' ? '☀️' : theme.value === 'dark' ? '🌙' : '🔄'
  return (
    <button class="sh-theme-toggle" onClick={next} title={`Theme: ${theme.value}`} aria-label="Toggle theme">
      {icon}
    </button>
  )
}

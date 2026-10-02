/**
 * householdTheme store — the household theme pieces the whole SPA
 * paints (§23.125). Today: the household font.
 *
 * Cold-fetched once after auth (``App.tsx``) and updated by
 * :mod:`HouseholdThemeStudio` on save. The font is applied as
 * ``--hh-font`` on ``<html>``; the body rule reads
 * ``var(--sh-space-font, var(--hh-font))`` so a space's own font wins
 * inside that space (``useSpaceTheme``). ``system`` (the column default)
 * means "no override" — the app's own font.
 */
import { computed, effect, signal } from '@preact/signals'
import { api } from '@/api'
import { APP_FONT_STACK, fontOverride, isFontId, type FontId } from '@/utils/themeFonts'

export const householdFont = signal<FontId>('system')

/** The stack the household font resolves to — what a space set to
 *  "Same as household" looks like. */
export const householdFontStack = computed(
  () => fontOverride(householdFont.value) ?? APP_FONT_STACK,
)

export async function loadHouseholdTheme(): Promise<void> {
  try {
    const t = await api.get('/api/theme') as { font_family?: unknown }
    householdFont.value = isFontId(t.font_family) ? t.font_family : 'system'
  } catch {
    // Not authed yet or backend unreachable — keep the app font.
  }
}

effect(() => {
  if (typeof document === 'undefined') return
  const stack = fontOverride(householdFont.value)
  const style = document.documentElement.style
  if (stack) style.setProperty('--hh-font', stack)
  else style.removeProperty('--hh-font')
})

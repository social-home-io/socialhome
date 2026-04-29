/**
 * Theme store — light / dark / auto, persisted in localStorage.
 *
 * Loaded eagerly from `main.tsx` so the `<html>` class is in place
 * before the first React-tree render, even if the user never opens
 * Settings. (Previously the effect lived in `ThemeToggle.tsx`, which
 * is only imported by the lazy-loaded SettingsPage chunk — meaning
 * theme application was deferred until the first /settings visit
 * and the page would flash light → real-theme on cold load.)
 *
 * `index.html` carries an inline pre-paint script that applies the
 * same class *before* any JS bundle loads. This module then takes
 * over for live changes (toggle, system-theme flip in auto mode).
 */
import { signal, effect } from '@preact/signals'

export type Theme = 'light' | 'dark' | 'auto'

const STORAGE_KEY = 'sh_theme'

function readStoredTheme(): Theme {
  try {
    const t = localStorage.getItem(STORAGE_KEY) as Theme | null
    if (t === 'light' || t === 'dark' || t === 'auto') return t
  } catch {
    // localStorage may throw in private mode / sandboxed contexts.
  }
  return 'auto'
}

export const theme = signal<Theme>(readStoredTheme())

function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyTheme(t: Theme): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  root.classList.remove('sh-theme-light', 'sh-theme-dark')
  const dark = t === 'dark' || (t === 'auto' && systemPrefersDark())
  root.classList.add(dark ? 'sh-theme-dark' : 'sh-theme-light')
}

if (typeof document !== 'undefined') {
  effect(() => {
    const t = theme.value
    try {
      localStorage.setItem(STORAGE_KEY, t)
    } catch {
      // Storage write can fail (quota, sandbox); the class still
      // applies, so the user just loses persistence for this session.
    }
    applyTheme(t)
  })

  // In `auto` mode, follow the OS-level theme as it changes (the
  // user toggles dark mode in their settings without reloading).
  // The signal value stays `auto`; only the applied class flips.
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => {
      if (theme.value === 'auto') applyTheme('auto')
    }
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', handler)
    } else if (typeof mq.addListener === 'function') {
      // Safari < 14 fallback.
      mq.addListener(handler)
    }
  }
}

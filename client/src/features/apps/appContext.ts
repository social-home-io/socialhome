/**
 * How an app should look (owner decision D14): Social Home tells every app
 * the person's language, its text direction, light or dark, whether they
 * asked for less motion, and eight colours. ``app.context`` carries it and
 * the bridge pushes ``app.context-changed`` when any of it changes while the
 * app is open (bridge.ts).
 *
 * The colours are *values*, never CSS: each one is read from the page's own
 * tokens (tokens.css, after the theme class and any household override),
 * resolved by the browser and re-serialised here as ``#rrggbb`` /
 * ``#rrggbbaa``. Anything that doesn't parse as a plain sRGB colour falls
 * back to the default palette, so nothing but a hex colour ever crosses into
 * the sandbox. The app applies them itself (the SDK's ``applyTheme()`` sets
 * them as ``--sh-*`` variables on its own ``:root``).
 *
 * | token      | from tokens.css         | what it's for                          |
 * |------------|-------------------------|----------------------------------------|
 * | `bg`       | `--sh-bg`               | the page background                    |
 * | `surface`  | `--sh-bg-tertiary`      | cards, panels, dialogs                 |
 * | `text`     | `--sh-text`             | body text                              |
 * | `muted`    | `--sh-text-muted`       | secondary text (≥ 4.5:1 on bg/surface) |
 * | `accent`   | `--sh-primary-fill`     | primary buttons, highlights            |
 * | `onAccent` | `--sh-on-primary-fill`  | text on an `accent` fill               |
 * | `border`   | `--sh-border`           | hairlines, dividers                    |
 * | `focus`    | `--sh-focus-ring`       | the keyboard focus ring                |
 */
import { effect } from '@preact/signals'
import { locale } from '@/i18n/i18n'

export const APP_TOKEN_NAMES = [
  'bg', 'surface', 'text', 'muted', 'accent', 'onAccent', 'border', 'focus',
] as const

export type AppTokenName = typeof APP_TOKEN_NAMES[number]
export type AppTokens = Record<AppTokenName, string>
export type AppTheme = 'light' | 'dark'

/** What ``app.context`` adds for the app's look (D14). */
export interface AppHostContext {
  /** The person's Social Home language (``en``, ``de``, …). */
  locale: string
  dir: 'ltr' | 'rtl'
  /** The theme the SPA shows right now (auto resolved to light or dark). */
  theme: AppTheme
  reducedMotion: boolean
  tokens: AppTokens
}

/** The tokens.css variable each app token is read from. */
const TOKEN_SOURCES: Record<AppTokenName, string> = {
  bg: '--sh-bg',
  surface: '--sh-bg-tertiary',
  text: '--sh-text',
  muted: '--sh-text-muted',
  accent: '--sh-primary-fill',
  onAccent: '--sh-on-primary-fill',
  border: '--sh-border',
  focus: '--sh-focus-ring',
}

/** The tokens.css values, resolved: what an app gets when the page can't
 *  compute its own (tests, an old browser) or a value doesn't parse. */
export const DEFAULT_TOKENS: Record<AppTheme, AppTokens> = {
  light: {
    bg: '#f4ece0',
    surface: '#efe3d2',
    text: '#1a1814',
    muted: '#685f4f',
    accent: '#b54824',
    onAccent: '#f4ece0',
    border: '#d8cfc0',
    focus: '#a44221',
  },
  dark: {
    bg: '#1a1612',
    surface: '#2f261e',
    text: '#f1e9da',
    muted: '#9a8e7d',
    accent: '#e96a3f',
    onAccent: '#1a1612',
    border: '#3d332a',
    focus: '#f1d9ca',
  },
}

const hex2 = (n: number): string => n.toString(16).padStart(2, '0')

/** ``0…255`` from a channel written as ``0…255`` / ``n%`` (``null`` when it isn't). */
function channel(s: string, scale: number): number | null {
  const pct = s.endsWith('%')
  const n = Number(pct ? s.slice(0, -1) : s)
  if (s === '' || !Number.isFinite(n)) return null
  const v = pct ? n / 100 : n / scale
  if (v < 0 || v > 1) return null
  return Math.round(v * 255)
}

/**
 * A computed colour as ``#rrggbb`` (``#rrggbbaa`` when not opaque), or
 * ``null`` when it is anything but a plain sRGB colour — the only shape a
 * token may take on its way into an app.
 */
export function toHexColor(input: string): string | null {
  const s = input.trim().toLowerCase()
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/.exec(s)
  if (m) {
    const h = m[1]
    return h.length === 3 ? `#${[...h].map(c => c + c).join('')}` : `#${h}`
  }
  let parts: string[] | null = null
  let scale = 255
  m = /^rgba?\(([^()]*)\)$/.exec(s)
  if (m) parts = m[1].split(/\s*[,/]\s*|\s+/).filter(Boolean)
  m = /^color\(srgb\s+([^()]*)\)$/.exec(s)
  if (m) {
    parts = m[1].split(/\s*\/\s*|\s+/).filter(Boolean)
    scale = 1
  }
  if (!parts || parts.length < 3 || parts.length > 4) return null
  const rgb = parts.slice(0, 3).map(p => channel(p, scale))
  const alpha = parts.length === 4 ? channel(parts[3], 1) : 255
  if (rgb.some(v => v === null) || alpha === null) return null
  const out = `#${(rgb as number[]).map(hex2).join('')}`
  return alpha === 255 ? out : out + hex2(alpha)
}

/** The eight tokens as the page shows them now (see the module doc). */
export function readTokens(theme: AppTheme): AppTokens {
  const out = { ...DEFAULT_TOKENS[theme] }
  if (typeof document === 'undefined' || !document.body) return out
  // The browser resolves var() / color-mix() for us: a probe's ``color``
  // computes to rgb() / color(srgb …).
  const probe = document.createElement('span')
  probe.style.display = 'none'
  document.body.appendChild(probe)
  try {
    for (const name of APP_TOKEN_NAMES) {
      probe.style.color = `var(${TOKEN_SOURCES[name]})`
      const v = toHexColor(String(window.getComputedStyle(probe).color ?? ''))
      if (v) out[name] = v
    }
  } finally {
    probe.remove()
  }
  return out
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
  } catch {
    return false
  }
}

/** The theme the SPA shows: store/theme.ts puts it on <html> as a class,
 *  auto already resolved against the system setting. */
export function shownTheme(): AppTheme {
  return document.documentElement.classList.contains('sh-theme-dark') ? 'dark' : 'light'
}

/** The look an app should follow right now. */
export function currentAppContext(): AppHostContext {
  const root = document.documentElement
  const theme = shownTheme()
  return {
    locale: locale.value || 'en',
    dir: root.dir === 'rtl' ? 'rtl' : 'ltr',
    theme,
    reducedMotion: prefersReducedMotion(),
    tokens: readTokens(theme),
  }
}

/**
 * Call *cb* with the new context whenever something an app sees changes:
 * the language, the theme (a toggle, or the system flipping in auto), a
 * household colour, reduced motion. Returns a function that stops watching.
 */
export function watchAppContext(cb: (c: AppHostContext) => void): () => void {
  let last = JSON.stringify(currentAppContext())
  const check = () => {
    const next = currentAppContext()
    const key = JSON.stringify(next)
    if (key === last) return
    last = key
    cb(next)
  }
  // setLocale() and the theme store write <html> lang / dir / class; a
  // household colour lands in its style.
  const mo = new MutationObserver(check)
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'lang', 'dir'] })
  // The locale signal can move before <html lang> does.
  const stopEffect = effect(() => {
    void locale.value
    queueMicrotask(check)
  })
  let mq: MediaQueryList | null = null
  try {
    mq = window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  } catch {
    mq = null
  }
  mq?.addEventListener?.('change', check)
  return () => {
    mo.disconnect()
    stopEffect()
    mq?.removeEventListener?.('change', check)
  }
}

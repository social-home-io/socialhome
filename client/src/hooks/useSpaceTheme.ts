/**
 * useSpaceTheme — apply a space's theme overrides while the user is
 * inside it, then roll back on unmount so the household palette
 * returns (§23 customization).
 *
 * The backend already exposes ``GET /api/spaces/{id}/theme`` and the
 * schema stores seven editable fields. This hook is intentionally
 * forgiving: it fetches, applies whatever it gets, and resets
 * exactly the properties it touched. A fetch error is a no-op (the
 * space just looks like the rest of the app).
 */
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { primaryFillOverrides } from '@/utils/primaryFill'
import { BRAND_ACCENT, BRAND_PRIMARY } from '@/utils/themeBrand'
import { fontOverride } from '@/utils/themeFonts'
import { DEFAULT_LAYOUT, isLayoutId } from '@/utils/themeLayouts'

interface SpaceTheme {
  primary_color?: string | null
  accent_color?: string | null
  header_image_file?: string | null
  background_tint?: string | null
  mode_override?: 'light' | 'dark' | null
  font_family?: string | null
  post_layout?: string | null
}

/** The ``space_themes`` column defaults (migration 0001) — the brand
 *  hearth + honey every space row carries until an admin picks a colour.
 *  They mean "no override": pinning them inline on ``<html>`` would force
 *  the LIGHT palette's hearth over the dark theme's lifted one. */
const DEFAULT_COLORS: Record<string, string> = {
  '--sh-primary': BRAND_PRIMARY.toLowerCase(),
  '--sh-accent': BRAND_ACCENT.toLowerCase(),
}

function isDefault(prop: string, value: string): boolean {
  return DEFAULT_COLORS[prop] === value.trim().toLowerCase()
}

export function useSpaceTheme(spaceId: string | undefined | null): void {
  useEffect(() => {
    if (!spaceId) return
    const root = document.documentElement
    const applied = new Set<string>()
    let stopped = false

    const apply = (prop: string, value: string) => {
      if (isDefault(prop, value)) return
      root.style.setProperty(prop, value)
      applied.add(prop)
    }

    const unapply = () => {
      applied.forEach(p => root.style.removeProperty(p))
      applied.clear()
      root.removeAttribute('data-space-theme')
      root.removeAttribute('data-post-layout')
    }

    void (async () => {
      try {
        const t = await api.get(
          `/api/spaces/${spaceId}/theme`,
        ) as SpaceTheme
        if (stopped) return
        // Checked here, not only inside apply(): the brand default means
        // "no override", so the per-theme on-fill props below must not be
        // pinned either — the CSS defaults already pair the brand hearth.
        if (t.primary_color && !isDefault('--sh-primary', t.primary_color)) {
          apply('--sh-primary', t.primary_color)
          // Text on a filled primary must still read on a custom
          // (often mid-tone) colour: per-theme ink + hover, read by
          // the matching theme block in tokens.css.
          for (const [prop, value] of Object.entries(primaryFillOverrides(t.primary_color))) {
            apply(prop, value)
          }
        }
        if (t.accent_color)     apply('--sh-accent',  t.accent_color)
        if (t.background_tint)  apply('--sh-bg-space-tint', t.background_tint)
        // A stored font is a schema id, not CSS; 'system' (the column
        // default) means "no override" — the household font stays.
        // ``--sh-space-font`` outranks ``--hh-font`` in the body rule.
        const stack = fontOverride(t.font_family)
        if (stack)              apply('--sh-space-font', stack)
        // A layout id (CHECK IN card/compact/magazine) drives the space
        // feed's CSS via ``data-post-layout``; 'card' is the feed's own look
        // and anything unknown is ignored.
        if (isLayoutId(t.post_layout) && t.post_layout !== DEFAULT_LAYOUT) {
          root.setAttribute('data-post-layout', t.post_layout)
        }
        if (t.mode_override === 'light' || t.mode_override === 'dark') {
          root.style.setProperty('color-scheme', t.mode_override)
          applied.add('color-scheme')
        }
        root.setAttribute('data-space-theme', spaceId)
      } catch { /* noop — keep household palette */ }
    })()

    return () => {
      stopped = true
      unapply()
    }
  }, [spaceId])
}

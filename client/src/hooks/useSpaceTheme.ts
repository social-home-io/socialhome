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

interface SpaceTheme {
  primary_color?: string | null
  accent_color?: string | null
  header_image_file?: string | null
  background_tint?: string | null
  mode_override?: 'light' | 'dark' | null
  font_family?: string | null
  post_layout?: 'compact' | 'spacious' | null
}

/** The ``space_themes`` column defaults (migration 0001) — the brand
 *  hearth + honey every space row carries until an admin picks a colour.
 *  They mean "no override": pinning them inline on ``<html>`` would force
 *  the LIGHT palette's hearth over the dark theme's lifted one. */
const DEFAULT_COLORS: Record<string, string> = {
  '--sh-primary': '#d2542a',
  '--sh-accent': '#c8902f',
}

function isDefault(prop: string, value: string): boolean {
  return DEFAULT_COLORS[prop] === value.trim().toLowerCase()
}

const POST_LAYOUT_GAP: Record<string, string> = {
  compact:  'var(--sh-space-xs)',
  spacious: 'var(--sh-space-lg)',
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
    }

    void (async () => {
      try {
        const t = await api.get(
          `/api/spaces/${spaceId}/theme`,
        ) as SpaceTheme
        if (stopped) return
        if (t.primary_color)    apply('--sh-primary', t.primary_color)
        if (t.accent_color)     apply('--sh-accent',  t.accent_color)
        if (t.background_tint)  apply('--sh-bg-space-tint', t.background_tint)
        if (t.font_family)      apply('--sh-font-family', t.font_family)
        if (t.post_layout && POST_LAYOUT_GAP[t.post_layout]) {
          apply('--sh-post-layout-gap', POST_LAYOUT_GAP[t.post_layout])
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
